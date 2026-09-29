"""Per-window, multi-link feature dataset for zone estimation (capability C).

Labelled sessions (whole recordings, or in-memory frame streams for software
tests) are pushed **offline and deterministically** through the same
processing functions the live pipeline uses:

``convert_frame`` -> ``build_window`` -> ``assess_quality`` -> ``extract_features``

(:mod:`roomsense.processing`). The low-level functions are used instead of a
full :class:`~roomsense.processing.pipeline.ProcessingEngine` because the
engine schedules each link's windows on that link's own newest sample. Here
every link is windowed at the same instants on a fixed grid, which makes the
cross-link alignment explicit and the output independent of frame
interleaving. The engine *is* used for one thing: the hardware signature, so
that a model is bound to exactly the value the runtime will report.

Windowing (per session)
-----------------------
* Time base: host arrival (monotonic) time, the only clock shared by every
  link on one host. Samples that fell back to a device clock are counted and
  not used (device clocks of different receivers are unrelated).
* Grid: ``T_n = t_first + trim + window_s + n * hop_s``, where ``t_first`` is
  the session's first accepted sample. A grid point is processed once data
  at least ``ORDERING_SLACK_S`` newer has arrived, so small cross-link
  interleaving differences do not matter.
* For every required link: ``build_window(end_ns=T)``. The grid point is
  excluded if any link has no window, a rejected window, quality ``BAD`` or
  ``UNAVAILABLE``, unusable features, or if the links' window ends are not
  within ``acquisition.alignment_tolerance_s``
  (:func:`~roomsense.acquisition.alignment.align_link_windows`).
* Edges: windows must lie inside ``[t_first + trim, t_last - trim]``.

Features
--------
Per link, in this order (all ``log10`` of a floor-clamped positive value,
because variation features span orders of magnitude):

* the window scalars of :data:`roomsense.processing.features.FEATURE_NAMES`
  (median across usable subcarriers), and
* fixed quantiles (10/25/75/90 %) of the per-subcarrier ``amp_cv`` and
  ``amp_tdiff`` vectors over usable subcarriers.

Quantiles make the input length independent of how many subcarriers a
layout has. Links are concatenated in a fixed ``link_order`` that is stored
with the model. RSSI and receiver coordinates are deliberately **not**
features, and nothing here computes a position.

Everything is bounded: per-link sample buffers are time-pruned and
length-capped, and a session may produce at most ``MAX_GRID_POINTS_PER_SESSION``
grid points (more raises :class:`DatasetError` instead of truncating silently).
"""

from __future__ import annotations

import contextlib
import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Iterator, Mapping, Sequence

import numpy as np

from ...acquisition.alignment import align_link_windows
from ...acquisition.base import FrameEvent
from ...config import AppConfig, ProcessingConfig
from ...processing.amplitude import TIME_BASE_HOST, AmplitudeSample, FrameRejection, convert_frame
from ...processing.features import FEATURE_NAMES, FeatureVector, extract_features
from ...processing.pipeline import ProcessingEngine
from ...processing.quality import assess_quality
from ...processing.windows import Window, WindowRejection, build_window
from ...schemas import CsiFrame, InputFormat, QualityFlag, QualityLevel, SourceMode

__all__ = [
    "FEATURE_SET_VERSION",
    "SUBCARRIER_QUANTILES",
    "PER_SUBCARRIER_KEYS",
    "LOG_FLOOR",
    "MIN_USABLE_SUBCARRIERS",
    "ORDERING_SLACK_S",
    "MAX_GRID_POINTS_PER_SESSION",
    "SPLIT_TRAIN",
    "SPLIT_VALIDATION",
    "SPLIT_TEST",
    "SPLITS",
    "EXCL_LINK_MISSING",
    "EXCL_WINDOW_REJECTED",
    "EXCL_LOW_QUALITY",
    "EXCL_FEATURES_UNUSABLE",
    "EXCL_NOT_ALIGNED",
    "EXCL_TRIMMED",
    "DatasetError",
    "SessionSpec",
    "SessionSummary",
    "ZoneDataset",
    "link_feature_names",
    "feature_names_for",
    "link_feature_values",
    "feature_row",
    "receiver_of",
    "frame_is_synthetic",
    "build_dataset",
]

FEATURE_SET_VERSION = "zone-features-v1"
SUBCARRIER_QUANTILES: tuple[float, ...] = (0.10, 0.25, 0.75, 0.90)
# FeatureVector.per_subcarrier keys summarised by quantiles, with short names.
PER_SUBCARRIER_KEYS: tuple[tuple[str, str], ...] = (("amp_cv_median", "amp_cv"), ("amp_tdiff_median", "amp_tdiff"))
LOG_FLOOR = 1e-6
MIN_USABLE_SUBCARRIERS = 4
ORDERING_SLACK_S = 1.0
MAX_GRID_POINTS_PER_SESSION = 200_000
# Per-link sample buffer cap (frames). Time pruning keeps far fewer; the cap
# only bounds memory if a link delivers at an absurd rate.
MAX_BUFFERED_SAMPLES = 20_000

SPLIT_TRAIN = "train"
SPLIT_VALIDATION = "validation"
SPLIT_TEST = "test"
SPLITS: tuple[str, ...] = (SPLIT_TRAIN, SPLIT_VALIDATION, SPLIT_TEST)

EXCL_LINK_MISSING = "LINK_MISSING"
EXCL_WINDOW_REJECTED = "WINDOW_REJECTED"
EXCL_LOW_QUALITY = "LOW_QUALITY"
EXCL_FEATURES_UNUSABLE = "FEATURES_UNUSABLE"
EXCL_NOT_ALIGNED = "NOT_ALIGNED"
EXCL_TRIMMED = "TRIMMED_SESSION_EDGE"

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DatasetError(ValueError):
    """A dataset cannot be built honestly from the given sessions."""


# ---------------------------------------------------------------------------
# Feature layout (shared with the runtime predictor)
# ---------------------------------------------------------------------------


def link_feature_names() -> tuple[str, ...]:
    """Names of the per-link feature block, in order."""
    names = [f"log10_{n}" for n in FEATURE_NAMES]
    for _, short in PER_SUBCARRIER_KEYS:
        names += [f"log10_{short}_q{int(round(q * 100)):02d}" for q in SUBCARRIER_QUANTILES]
    return tuple(names)


def feature_names_for(link_order: Sequence[str]) -> tuple[str, ...]:
    """Full model input names: ``<link_id>|<feature>`` in link order."""
    block = link_feature_names()
    return tuple(f"{lid}|{name}" for lid in link_order for name in block)


def _log(x: np.ndarray) -> np.ndarray:
    return np.log10(np.maximum(np.asarray(x, dtype=np.float64), LOG_FLOOR))


def link_feature_values(fv: FeatureVector) -> tuple[np.ndarray | None, str | None]:
    """The per-link feature block of one window, or ``(None, reason)``."""
    if tuple(fv.names) != tuple(FEATURE_NAMES):
        return None, (f"FEATURE_LAYOUT_CHANGED: window features {list(fv.names)} differ from "
                      f"{list(FEATURE_NAMES)}")
    if not fv.is_finite():
        return None, f"{EXCL_FEATURES_UNUSABLE}: non-finite window scalars"
    usable = np.asarray(fv.valid, dtype=bool)
    if int(usable.sum()) < MIN_USABLE_SUBCARRIERS:
        return None, f"{EXCL_FEATURES_UNUSABLE}: {int(usable.sum())} usable subcarriers (< {MIN_USABLE_SUBCARRIERS})"
    parts = [_log(fv.values)]
    for key, _ in PER_SUBCARRIER_KEYS:
        arr = fv.per_subcarrier.get(key)
        if arr is None:
            return None, f"FEATURE_LAYOUT_CHANGED: per-subcarrier feature {key!r} missing"
        arr = np.asarray(arr, dtype=np.float64)
        if arr.shape != usable.shape:
            return None, f"{EXCL_FEATURES_UNUSABLE}: per-subcarrier {key!r} shape {arr.shape} != {usable.shape}"
        vals = arr[usable]
        if not np.all(np.isfinite(vals)):
            return None, f"{EXCL_FEATURES_UNUSABLE}: non-finite per-subcarrier {key!r}"
        parts.append(_log(np.quantile(vals, SUBCARRIER_QUANTILES)))
    row = np.concatenate(parts)
    if row.shape != (len(link_feature_names()),) or not np.all(np.isfinite(row)):
        return None, f"{EXCL_FEATURES_UNUSABLE}: non-finite feature block"
    return row, None


def feature_row(features_by_link: Mapping[str, FeatureVector], link_order: Sequence[str]
                ) -> tuple[np.ndarray | None, list[str]]:
    """Model input row for one instant, or ``(None, reasons)``.

    Every link in ``link_order`` must be present with usable features. The
    runtime predictor uses exactly this function, so training and inference
    share one feature layout.
    """
    parts: list[np.ndarray] = []
    reasons: list[str] = []
    for lid in link_order:
        fv = features_by_link.get(lid)
        if fv is None:
            reasons.append(f"{EXCL_LINK_MISSING}: no feature window for required link {lid}")
            continue
        vals, why = link_feature_values(fv)
        if vals is None:
            reasons.append(f"{why} (link {lid})")
            continue
        parts.append(vals)
    if reasons:
        return None, reasons
    return np.concatenate(parts) if parts else np.zeros(0), []


def receiver_of(link_id: str) -> str:
    """Receiver id of a ``<tx>-><rx>`` link id (see ``ReceiverConfig.link_id``)."""
    return link_id.split("->", 1)[1] if "->" in link_id else link_id


def frame_is_synthetic(frame: CsiFrame) -> bool:
    """True for any frame that did not come from real hardware."""
    return (
        QualityFlag.SYNTHETIC.value in frame.quality_flags
        or frame.input_format == InputFormat.SYNTHETIC_V1
        or SourceMode(frame.source_mode) == SourceMode.SIMULATION
        or frame.device.identity_source == "synthetic"
    )


# ---------------------------------------------------------------------------
# Session description and summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSpec:
    """One labelled session: a whole recording or an in-memory frame stream.

    ``label`` is a zone id from the room geometry, ``"EMPTY"`` or
    ``"OUTSIDE_TARGET_ROOM"``. ``synthetic=True`` marks the session as
    simulated. It can only add the mark: frames are inspected too, and any
    simulated frame makes the session synthetic whatever this says.
    ``split`` is optional; either every session names one or none does.
    ``start_unix_ns`` orders sessions in time; when omitted it is taken from
    the first frame (original wall-clock arrival) or the recording header.
    """

    label: str
    recording_id: str | None = None
    frames: Iterable[CsiFrame] | Callable[[], Iterable[CsiFrame]] | None = None
    session_key: str | None = None
    start_unix_ns: int | None = None
    synthetic: bool = False
    split: str | None = None
    expected_rate_hz: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise DatasetError("session label must be a non-empty string")
        if (self.recording_id is None) == (self.frames is None):
            raise DatasetError("a session needs exactly one of recording_id or frames")
        if self.recording_id is not None and not _KEY_RE.fullmatch(self.recording_id):
            raise DatasetError("recording_id must match ^[A-Za-z0-9_-]{1,64}$")
        if self.session_key is not None and not _KEY_RE.fullmatch(self.session_key):
            raise DatasetError("session_key must match ^[A-Za-z0-9_-]{1,64}$")
        if self.recording_id is None and self.session_key is None:
            raise DatasetError("an in-memory session needs a session_key")
        if self.split is not None and self.split not in SPLITS:
            raise DatasetError(f"split must be one of {list(SPLITS)} or None, got {self.split!r}")
        if self.expected_rate_hz is not None and not (0 < float(self.expected_rate_hz) <= 1000):
            raise DatasetError("expected_rate_hz must be in (0, 1000]")

    @property
    def key(self) -> str:
        return self.session_key if self.session_key is not None else str(self.recording_id)


@dataclass
class SessionSummary:
    """What happened to one session while building the dataset."""

    session_key: str
    label: str
    requested_split: str | None
    recording_id: str | None
    synthetic: bool
    synthetic_reasons: list[str]
    start_unix_ns: int | None
    end_unix_ns: int | None
    source_modes: list[str]
    hardware_signature: str | None
    link_ids_seen: list[str]
    layout_ids: list[str]
    frames_total: int = 0
    frames_rejected: int = 0
    frames_device_time_base: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    grid_points: int = 0
    windows_kept: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    duration_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: (dict(v) if isinstance(v, Counter) else v) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class ZoneDataset:
    """Feature matrix plus everything needed to split it by session.

    ``window_end_ns`` is the grid instant on the session's host-monotonic
    data timeline (only comparable within one session).
    """

    X: np.ndarray
    y: np.ndarray
    session_ids: np.ndarray
    window_end_ns: np.ndarray
    feature_names: tuple[str, ...]
    link_order: tuple[str, ...]
    sessions: tuple[SessionSummary, ...]
    config_version: str
    feature_set_version: str = FEATURE_SET_VERSION

    def session(self, key: str) -> SessionSummary:
        for s in self.sessions:
            if s.session_key == key:
                return s
        raise KeyError(key)

    @property
    def synthetic_session_keys(self) -> list[str]:
        return [s.session_key for s in self.sessions if s.synthetic]

    @property
    def hardware_signatures(self) -> list[str | None]:
        return sorted({s.hardware_signature for s in self.sessions}, key=lambda v: (v is None, str(v)))

    @property
    def receivers(self) -> list[str]:
        return sorted({receiver_of(lid) for lid in self.link_order})

    def summary(self) -> dict[str, Any]:
        return {
            "n_windows": int(self.X.shape[0]),
            "n_features": int(self.X.shape[1]) if self.X.ndim == 2 else 0,
            "feature_set_version": self.feature_set_version,
            "link_order": list(self.link_order),
            "receivers": self.receivers,
            "config_version": self.config_version,
            "sessions": [s.to_dict() for s in self.sessions],
        }


# ---------------------------------------------------------------------------
# Offline windowing of one session
# ---------------------------------------------------------------------------


@dataclass
class _GridPoint:
    t_ns: int
    parts: dict[str, np.ndarray]
    ends: dict[str, int]
    reasons: dict[str, str]


class _LinkBuffer:
    def __init__(self) -> None:
        self.samples: deque[AmplitudeSample] = deque(maxlen=MAX_BUFFERED_SAMPLES)
        self.rejected: deque[int] = deque(maxlen=MAX_BUFFERED_SAMPLES)

    def prune(self, keep_from_ns: int) -> None:
        while self.samples and self.samples[0].t_ns < keep_from_ns:
            self.samples.popleft()
        while self.rejected and self.rejected[0] < keep_from_ns:
            self.rejected.popleft()


class _SessionWindower:
    """Fixed-grid windowing of all links of one session (see module docs)."""

    def __init__(self, pcfg: ProcessingConfig, *, expected_rate_hz: float, trim_s: float) -> None:
        self.pcfg = pcfg
        self.rate = expected_rate_hz
        self.window_ns = int(round(pcfg.window_s * 1e9))
        self.hop_ns = int(round(pcfg.hop_s * 1e9))
        self.trim_ns = int(round(trim_s * 1e9))
        self.slack_ns = int(round(ORDERING_SLACK_S * 1e9))
        self.links: dict[str, _LinkBuffer] = {}
        self.t_first: int | None = None
        self.t_last: int | None = None
        self.next_t: int | None = None
        self.points: list[_GridPoint] = []

    def _buf(self, link_id: str) -> _LinkBuffer:
        buf = self.links.get(link_id)
        if buf is None:
            buf = self.links[link_id] = _LinkBuffer()
        return buf

    def add_sample(self, link_id: str, sample: AmplitudeSample) -> None:
        self._buf(link_id).samples.append(sample)
        if self.t_first is None:
            self.t_first = sample.t_ns
            self.next_t = sample.t_ns + self.trim_ns + self.window_ns
        self.t_last = sample.t_ns if self.t_last is None else max(self.t_last, sample.t_ns)
        self._advance(final=False)

    def add_rejected(self, link_id: str, arrival_ns: int) -> None:
        self._buf(link_id).rejected.append(arrival_ns)

    def _advance(self, *, final: bool) -> None:
        if self.next_t is None or self.t_last is None:
            return
        limit = self.t_last if final else self.t_last - self.slack_ns
        while self.next_t <= limit:
            if len(self.points) >= MAX_GRID_POINTS_PER_SESSION:
                raise DatasetError(
                    f"session produces more than {MAX_GRID_POINTS_PER_SESSION} windows; split it into shorter "
                    "recordings (nothing is truncated silently)"
                )
            self.points.append(self._evaluate(self.next_t))
            self.next_t += self.hop_ns
            keep_from = self.next_t - 2 * self.window_ns
            for buf in self.links.values():
                buf.prune(keep_from)

    def _evaluate(self, t_ns: int) -> _GridPoint:
        point = _GridPoint(t_ns=t_ns, parts={}, ends={}, reasons={})
        for lid in sorted(self.links):
            buf = self.links[lid]
            window = build_window(buf.samples, end_ns=t_ns, cfg=self.pcfg, link_id=lid)
            n_rej = sum(1 for t in buf.rejected if t_ns - self.window_ns <= t <= t_ns)
            quality = assess_quality(window, expected_rate_hz=self.rate, cfg=self.pcfg, rejected_frames=n_rej)
            if isinstance(window, WindowRejection):
                point.reasons[lid] = f"{EXCL_WINDOW_REJECTED}: {window.reason}"
                continue
            assert isinstance(window, Window)
            level = QualityLevel(quality.level)
            if level in (QualityLevel.BAD, QualityLevel.UNAVAILABLE):
                point.reasons[lid] = f"{EXCL_LOW_QUALITY}: {level.value} ({', '.join(quality.flags)})"
                continue
            fv = extract_features(window, self.pcfg)
            vals, why = link_feature_values(fv)
            if vals is None:
                point.reasons[lid] = str(why)
                continue
            point.parts[lid] = vals
            point.ends[lid] = window.t_end_ns
        return point

    def finish(self) -> list[_GridPoint]:
        self._advance(final=True)
        return self.points


# ---------------------------------------------------------------------------
# Building the dataset
# ---------------------------------------------------------------------------


@dataclass
class _Processed:
    spec: SessionSpec
    summary: SessionSummary
    points: list[_GridPoint]
    t_last: int | None


def _frame_unix(frame: CsiFrame) -> int | None:
    # A replayed frame keeps its original wall-clock arrival separately.
    if frame.recorded_host_arrival_unix_ns is not None:
        return int(frame.recorded_host_arrival_unix_ns)
    return None if frame.host_arrival_unix_ns is None else int(frame.host_arrival_unix_ns)


@contextlib.contextmanager
def _open_frames(spec: SessionSpec, data_dir: Path | None, db: Any | None
                 ) -> Iterator[tuple[Iterable[CsiFrame], dict[str, Any]]]:
    """Yield the session's frames and what the recording metadata says."""
    meta: dict[str, Any] = {"synthetic_reasons": [], "start_unix_ns": None}
    if spec.recording_id is None:
        src = spec.frames() if callable(spec.frames) else spec.frames
        assert src is not None
        yield iter(src), meta
        return
    # Imported lazily: the storage layer is only needed for recordings.
    from ...acquisition.replay_source import original_is_synthetic
    from ...recording_format import read_header
    from ...storage.recordings import iter_recording, recording_file_for_read

    if data_dir is None:
        raise DatasetError("data_dir is required to read recordings")
    try:
        path = recording_file_for_read(Path(data_dir), spec.recording_id)
    except (ValueError, FileNotFoundError) as exc:
        raise DatasetError(f"recording {spec.recording_id}: {exc}") from exc
    header = read_header(path)
    for key in ("source_mode", "original_source_mode"):
        if header.get(key) == SourceMode.SIMULATION.value:
            meta["synthetic_reasons"].append(f"recording header {key} is SIMULATION")
    if header.get("synthetic") is True:
        meta["synthetic_reasons"].append("recording header marks the data synthetic")
    if original_is_synthetic(path):
        meta["synthetic_reasons"].append("recording holds simulated frames")
    if db is not None:
        info = db.get_recording(spec.recording_id)
        if info is not None and info.synthetic:
            meta["synthetic_reasons"].append("recording metadata marks the data synthetic")
    created = header.get("created_at_unix_ns")
    if isinstance(created, int) and not isinstance(created, bool):
        meta["start_unix_ns"] = created
    frames = iter_recording(path)
    try:
        yield frames, meta
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()


def _process_session(spec: SessionSpec, cfg: AppConfig, *, trim_s: float, data_dir: Path | None, db: Any | None
                     ) -> _Processed:
    pcfg = cfg.processing
    rate = float(spec.expected_rate_hz) if spec.expected_rate_hz is not None else cfg.acquisition.expected_rate_hz
    windower = _SessionWindower(pcfg, expected_rate_hz=rate, trim_s=trim_s)
    # Only used for hardware_signature(): the exact value the runtime reports.
    signature_engine = ProcessingEngine(cfg, clock_ns=lambda: 0, clock_unix_ns=lambda: 0)
    synthetic_reasons: list[str] = ["declared synthetic by the caller"] if spec.synthetic else []
    modes: set[str] = set()
    links_seen: set[str] = set()
    layouts: set[str] = set()
    rejections: Counter[str] = Counter()
    first_unix: int | None = None
    last_unix: int | None = None
    n_total = n_rej = n_dev = 0
    synthetic_frame_seen = False

    with _open_frames(spec, data_dir, db) as (frames, meta):
        synthetic_reasons += meta["synthetic_reasons"]
        for frame in frames:
            if not isinstance(frame, CsiFrame):
                raise DatasetError(f"session {spec.key}: expected CsiFrame objects, got {type(frame).__name__}")
            n_total += 1
            modes.add(SourceMode(frame.source_mode).value)
            links_seen.add(frame.link_id)
            if not synthetic_frame_seen and frame_is_synthetic(frame):
                synthetic_frame_seen = True
                synthetic_reasons.append("session contains simulated (SYNTHETIC) frames")
            unix = _frame_unix(frame)
            if unix is not None:
                first_unix = unix if first_unix is None else min(first_unix, unix)
                last_unix = unix if last_unix is None else max(last_unix, unix)
            signature_engine.on_event(FrameEvent(frame))
            out = convert_frame(frame, pcfg)
            if isinstance(out, FrameRejection):
                n_rej += 1
                rejections[out.reason] += 1
                if frame.host_arrival_monotonic_ns is not None:
                    windower.add_rejected(frame.link_id, int(frame.host_arrival_monotonic_ns))
                continue
            if out.time_base != TIME_BASE_HOST:
                n_dev += 1
                continue
            layouts.add(out.layout_id)
            windower.add_sample(frame.link_id, out)
        start_meta = meta["start_unix_ns"]

    points = windower.finish()
    start = spec.start_unix_ns if spec.start_unix_ns is not None else (
        first_unix if first_unix is not None else start_meta)
    duration = None
    if windower.t_first is not None and windower.t_last is not None:
        duration = (windower.t_last - windower.t_first) / 1e9
    summary = SessionSummary(
        session_key=spec.key,
        label=spec.label,
        requested_split=spec.split,
        recording_id=spec.recording_id,
        synthetic=bool(synthetic_reasons),
        synthetic_reasons=synthetic_reasons,
        start_unix_ns=None if start is None else int(start),
        end_unix_ns=last_unix,
        source_modes=sorted(modes),
        hardware_signature=signature_engine.hardware_signature(),
        link_ids_seen=sorted(links_seen),
        layout_ids=sorted(layouts),
        frames_total=n_total,
        frames_rejected=n_rej,
        frames_device_time_base=n_dev,
        rejection_reasons=dict(rejections),
        grid_points=len(points),
        duration_s=duration,
    )
    return _Processed(spec=spec, summary=summary, points=points, t_last=windower.t_last)


def build_dataset(
    specs: Sequence[SessionSpec],
    cfg: AppConfig,
    *,
    trim_session_edges_s: float,
    link_order: Sequence[str] | None = None,
    allowed_labels: Collection[str] | None = None,
    data_dir: Path | None = None,
    db: Any | None = None,
) -> ZoneDataset:
    """Build the per-window dataset from labelled sessions.

    ``link_order`` defaults to the sorted link ids, which must then be the
    same in every session. ``allowed_labels`` (zone ids plus the two
    non-zone labels) is checked when given. ``db`` (a
    :class:`roomsense.storage.db.Database`) is optional and only used to
    read the recordings' synthetic flag.
    """
    if not specs:
        raise DatasetError("no sessions given")
    if not (math.isfinite(trim_session_edges_s) and trim_session_edges_s >= 0):
        raise DatasetError("trim_session_edges_s must be a finite number >= 0")
    keys = [s.key for s in specs]
    dup = sorted(k for k, n in Counter(keys).items() if n > 1)
    if dup:
        raise DatasetError(f"duplicate session(s) {dup}: one session may appear only once (no leakage)")
    rec_ids = [s.recording_id for s in specs if s.recording_id is not None]
    dup_rec = sorted(k for k, n in Counter(rec_ids).items() if n > 1)
    if dup_rec:
        raise DatasetError(f"recording(s) {dup_rec} used by more than one session")
    if allowed_labels is not None:
        bad = sorted({s.label for s in specs} - set(allowed_labels))
        if bad:
            raise DatasetError(f"unknown label(s) {bad}; allowed: {sorted(allowed_labels)}")

    processed = [_process_session(s, cfg, trim_s=trim_session_edges_s, data_dir=data_dir, db=db) for s in specs]

    if link_order is None:
        link_sets = {tuple(p.summary.link_ids_seen) for p in processed}
        if len(link_sets) != 1:
            raise DatasetError(
                "sessions have different link sets "
                f"{sorted(list(ls) for ls in link_sets)}; pass link_order explicitly or record every session "
                "with the same receivers"
            )
        order = tuple(next(iter(link_sets)))
    else:
        order = tuple(dict.fromkeys(link_order))
    if not order:
        raise DatasetError("no links: link_order is empty")

    tol = cfg.acquisition.alignment_tolerance_s
    trim_ns = int(round(trim_session_edges_s * 1e9))
    block = len(link_feature_names())
    rows: list[np.ndarray] = []
    labels: list[str] = []
    sess: list[str] = []
    ends: list[int] = []
    for p in processed:
        excluded: Counter[str] = Counter()
        kept = 0
        for pt in p.points:
            if p.t_last is not None and pt.t_ns > p.t_last - trim_ns:
                excluded[EXCL_TRIMMED] += 1
                continue
            reason = None
            for lid in order:
                if lid not in pt.parts:
                    reason = pt.reasons.get(lid, f"{EXCL_LINK_MISSING}: no data for {lid}")
                    break
            if reason is None:
                ok, why = align_link_windows({lid: pt.ends[lid] for lid in order}, tol)
                if not ok:
                    reason = f"{EXCL_NOT_ALIGNED}: {why}"
            if reason is not None:
                excluded[reason.split(":", 1)[0]] += 1
                continue
            rows.append(np.concatenate([pt.parts[lid] for lid in order]))
            labels.append(p.spec.label)
            sess.append(p.spec.key)
            ends.append(pt.t_ns)
            kept += 1
        p.summary.windows_kept = kept
        p.summary.excluded = dict(excluded)
        p.points = []  # release memory early

    n_features = block * len(order)
    X = np.vstack(rows) if rows else np.zeros((0, n_features), dtype=np.float64)
    return ZoneDataset(
        X=X.astype(np.float64, copy=False),
        y=np.asarray(labels, dtype=str) if labels else np.zeros(0, dtype=str),
        session_ids=np.asarray(sess, dtype=str) if sess else np.zeros(0, dtype=str),
        window_end_ns=np.asarray(ends, dtype=np.int64),
        feature_names=feature_names_for(order),
        link_order=order,
        sessions=tuple(p.summary for p in processed),
        config_version=cfg.config_version(),
    )
