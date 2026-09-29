"""Per-link processing engine: frames -> windows -> features -> decisions.

Registered as a consumer on the acquisition manager (``on_event``). Frames
may arrive on any thread. A processing loop calls :meth:`ProcessingEngine.step`
periodically, and API threads read ``latest`` / ``history`` /
``signal_snapshot``. One re-entrant lock serialises all state access.

Timing
------
* A window is computed for a link when its newest accepted sample is at least
  ``hop_s`` newer than the previous window end. The window ends at that
  sample. Windows are therefore driven by the measurement timeline, and
  replaying a recording gives the same windows no matter how often ``step``
  runs.
* Staleness uses ``now_ns`` (host monotonic clock) against the arrival time of
  the newest accepted frame. When no fresh window is due and the data is
  stale, the detector's ``tick`` yields ``SENSOR_OFFLINE`` at most once per
  ``hop_s``.

Identity and calibration validity
---------------------------------
Each link has an identity tuple
``(link_id, chip, firmware_name, firmware_version, channel, secondary_channel,
layout_id, transmitter_mac)`` taken from its newest accepted frames. A field
missing from a frame keeps its last known value: "not reported" is not a
change. Any reported value that differs from a known one counts as a change.
The link's buffers and hysteresis are then reset and its baseline is
invalidated (``LAYOUT_CHANGED`` or ``HARDWARE_SIGNATURE_CHANGED``). The engine
never re-applies an old baseline on its own.

Memory is bounded everywhere: per-link deques have fixed ``maxlen``, the number
of links is capped, and baseline recorders cap their window count.
"""

from __future__ import annotations

import dataclasses
import math
import threading
import time
import uuid
import warnings
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from ..acquisition.base import (
    CONNECTED,
    DISCONNECTED,
    HELLO,
    RECONNECTING,
    EndOfStream,
    FrameEvent,
    LinkEvent,
    SourceEvent,
)
from ..config import AppConfig
from ..schemas import ActivityResult, ActivityState, CsiFrame, Provenance, SourceMode, canonical_hash
from .amplitude import AmplitudeSample, FrameRejection, cached_layout, convert_frame, ensure_phase_disabled
from .baseline import Baseline, BaselineRecorder
from .detector import MotionDetector
from .features import FeatureVector, extract_features
from .quality import assess_quality
from .windows import Window, WindowRejection, build_window

__all__ = [
    "HISTORY_SECONDS",
    "DISPLAY_INTERVAL_S",
    "MAX_SNAPSHOT_POINTS",
    "MAX_SNAPSHOT_SUBCARRIERS",
    "BUFFER_RATE_FACTOR",
    "MAX_LINKS",
    "ProcessingEngine",
]

HISTORY_SECONDS = 600.0  # results / features / display amplitude kept per link
DISPLAY_INTERVAL_S = 0.2  # amplitude display history decimation
MAX_SNAPSHOT_POINTS = 200
MAX_SNAPSHOT_SUBCARRIERS = 16
BUFFER_RATE_FACTOR = 4.0  # raw buffer holds window_s at 4x the expected rate
MIN_BUFFER_FRAMES = 64
MAX_LINKS = 64
MAX_GAP_RECORDS = 1024
_IDENTITY_FIELDS = (
    "link_id",
    "chip",
    "firmware_name",
    "firmware_version",
    "channel",
    "secondary_channel",
    "layout_id",
    "transmitter_mac",
)


@dataclass
class _WalkStats:
    windows: int = 0  # decision windows (NO_MOTION_DETECTED / MOTION_DETECTED)
    motion_windows: int = 0
    undecided_windows: int = 0  # UNKNOWN / CALIBRATING
    offline_windows: int = 0
    max_score: float | None = None


@dataclass
class _Link:
    link_id: str
    detector: MotionDetector
    buffer_len: int
    expected_rate_hz: float
    history_len: int
    display_len: int
    samples: deque[AmplitudeSample] = field(init=False)
    rejected_times: deque[int] = field(init=False)
    results: deque[tuple[int, ActivityResult]] = field(init=False)
    features: deque[FeatureVector] = field(init=False)
    display: deque[tuple[int, np.ndarray]] = field(init=False)
    frame_gaps: deque[tuple[int, int]] = field(init=False)
    identity: tuple[Any, ...] | None = None
    signature: str | None = None
    signature_complete: bool = False
    layout_id: str | None = None
    time_base: str | None = None
    source_mode: SourceMode | None = None
    session_id: str | None = None
    last_accepted_ns: int | None = None
    last_any_ns: int | None = None
    last_window_end_ns: int | None = None
    last_emit_ns: int | None = None
    offline_emitted: bool = False  # last emitted result came from the stale/offline path
    last_display_ns: int | None = None
    unix_offset_ns: int | None = None
    offline_reason: str | None = None
    calibration_id: str | None = None
    recorder: BaselineRecorder | None = None
    calib_frames: int = 0  # frames accepted while the recorder was active
    latest: ActivityResult | None = None
    latest_fv: FeatureVector | None = None
    frames_total: int = 0
    frames_accepted: int = 0
    frames_rejected: int = 0
    rejections: Counter[str] = field(default_factory=Counter)
    link_events: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self.samples = deque(maxlen=self.buffer_len)
        self.rejected_times = deque(maxlen=self.buffer_len)
        self.results = deque(maxlen=self.history_len)
        self.features = deque(maxlen=self.history_len)
        self.display = deque(maxlen=self.display_len)
        self.frame_gaps = deque(maxlen=MAX_GAP_RECORDS)

    def reset_data(self) -> None:
        """Drop buffered measurements (not results history or calibration)."""
        self.samples.clear()
        self.rejected_times.clear()
        self.display.clear()
        self.last_window_end_ns = None
        self.last_display_ns = None
        self.latest_fv = None
        self.layout_id = None
        self.time_base = None
        self.detector.reset_state()


def _nan_list(a: np.ndarray) -> list[float | None]:
    return [float(v) if np.isfinite(v) else None for v in np.asarray(a, dtype=np.float64)]


def _downsample_segments(
    t: np.ndarray, v: np.ndarray, seg: np.ndarray, max_points: int
) -> tuple[list[float], list[list[float | None] | None]]:
    """Downsample rows ``v`` (n x m) at times ``t`` without bridging segments.

    Returns time points and rows, with a ``None`` row between segments (the
    gap marker). The output never has more than ``max_points`` entries,
    markers included. If there are too many segments to fit, only the most
    recent ones are kept.
    """
    if t.size == 0:
        return [], []
    bounds = np.flatnonzero(np.diff(seg) != 0) + 1
    segments = np.split(np.arange(t.size), bounds)
    max_segments = max(1, (max_points + 1) // 2)
    if len(segments) > max_segments:
        segments = segments[-max_segments:]
    budget = max_points - (len(segments) - 1)
    counts = np.array([s.size for s in segments])
    total = int(counts.sum())
    if total <= budget:
        alloc = counts.copy()
    else:
        alloc = np.maximum(1, np.floor(budget * counts / total).astype(int))
        while int(alloc.sum()) > budget:
            alloc[int(np.argmax(alloc))] -= 1
    out_t: list[float] = []
    out_v: list[list[float | None] | None] = []
    for i, (idx, n_out) in enumerate(zip(segments, alloc)):
        if i > 0:
            out_t.append((out_t[-1] + float(t[idx[0]])) / 2.0)
            out_v.append(None)
        chunks = np.array_split(idx, int(n_out)) if idx.size > n_out else [np.array([j]) for j in idx]
        for c in chunks:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                row = np.nanmean(v[c], axis=0) if c.size > 1 else v[c[0]]
            out_t.append(float(np.mean(t[c])))
            out_v.append(_nan_list(row))
    return out_t, out_v


class ProcessingEngine:
    """See the module docstring. All public methods are thread-safe."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        clock_unix_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        ensure_phase_disabled(cfg.processing)
        self.cfg = cfg
        self._pcfg = cfg.processing
        self._dcfg = cfg.detection
        self._config_version = cfg.config_version()
        self._clock_ns = clock_ns
        self._clock_unix_ns = clock_unix_ns
        self._lock = threading.RLock()
        self._hop_ns = int(round(self._pcfg.hop_s * 1e9))
        self._window_ns = int(round(self._pcfg.window_s * 1e9))
        # Consecutive points further apart than this are drawn with a gap.
        self._plot_gap_ns = int(round(max(2 * self._pcfg.hop_s, self._pcfg.max_gap_s) * 1e9))
        self._history_len = int(math.ceil(HISTORY_SECONDS / self._pcfg.hop_s)) + 16
        self._display_len = int(math.ceil(HISTORY_SECONDS / DISPLAY_INTERVAL_S)) + 16
        self._links: dict[str, _Link] = {}
        self._session_id: str | None = None
        self._source_mode: SourceMode | None = None
        self._foreign_frames = 0
        self._dropped_new_links = 0
        self._calibrating: set[str] = set()
        self._walk: dict[str, _WalkStats] | None = None
        self._last_now_ns: int | None = None

    # ----------------------------------------------------------------- helpers
    def _buffer_len(self, rate_hz: float) -> int:
        return max(MIN_BUFFER_FRAMES, int(math.ceil(self._pcfg.window_s * rate_hz * BUFFER_RATE_FACTOR)))

    def _link(self, link_id: str) -> _Link | None:
        link = self._links.get(link_id)
        if link is not None:
            return link
        if len(self._links) >= MAX_LINKS:
            self._dropped_new_links += 1
            return None
        rate = self.cfg.acquisition.expected_rate_hz
        det = MotionDetector(link_id, self._dcfg, self._config_version,
                             stale_after_s=self.cfg.acquisition.stale_after_s)
        link = _Link(link_id=link_id, detector=det, buffer_len=self._buffer_len(rate), expected_rate_hz=rate,
                     history_len=self._history_len, display_len=self._display_len)
        link.session_id, link.source_mode = self._session_id, self._source_mode
        self._links[link_id] = link
        return link

    def _set_expected_rate(self, link: _Link, rate_hz: float) -> None:
        link.expected_rate_hz = rate_hz
        need = self._buffer_len(rate_hz)
        if need > link.buffer_len:
            link.buffer_len = need
            link.samples = deque(link.samples, maxlen=need)
            link.rejected_times = deque(link.rejected_times, maxlen=need)

    def _invalidate(self, link: _Link, reason: str) -> None:
        if link.detector.baseline is not None:
            link.detector.invalidate_baseline(reason)
            link.calibration_id = None
        if link.recorder is not None:
            link.recorder.note_identity_change(reason)

    def _provenance(self, link: _Link, window: Window | WindowRejection | None, age_s: float | None) -> Provenance:
        assert link.source_mode is not None and link.session_id is not None
        has_baseline = link.detector.baseline is not None
        if isinstance(window, Window):
            w_start, w_end, n = window.t_unix_start_ns, window.t_unix_end_ns, window.n_frames
        elif isinstance(window, WindowRejection):
            w_start, w_end, n = None, None, window.n_frames
        else:
            w_start, w_end, n = None, None, 0
        return Provenance(
            source_mode=link.source_mode,
            session_id=link.session_id,
            link_ids=[link.link_id],
            window_start_unix_ns=w_start,
            window_end_unix_ns=w_end,
            window_frame_count=n,
            config_version=self._config_version,
            calibration_id=link.calibration_id if has_baseline else None,
            computed_at_unix_ns=self._clock_unix_ns(),
            measurement_age_s=age_s,
        )

    @staticmethod
    def _sample_time(link: _Link, now_ns: int) -> int:
        """Express a host-monotonic instant on the link's sample time base.

        Identical for host-timed links. For the device-clock fallback it
        extrapolates from the newest sample, so history and plots never mix
        the two clocks.
        """
        if link.samples and link.last_accepted_ns is not None:
            return link.samples[-1].t_ns + (now_ns - link.last_accepted_ns)
        return now_ns

    def _record(self, link: _Link, res: ActivityResult, t_ref_ns: int, now_ns: int) -> None:
        link.results.append((t_ref_ns, res))
        link.latest = res
        link.last_emit_ns = now_ns
        if self._walk is not None and link.link_id in self._walk:
            ws = self._walk[link.link_id]
            state = ActivityState(res.state)
            if state in (ActivityState.MOTION_DETECTED, ActivityState.NO_MOTION_DETECTED):
                ws.windows += 1
                if state == ActivityState.MOTION_DETECTED:
                    ws.motion_windows += 1
            elif state == ActivityState.SENSOR_OFFLINE:
                ws.offline_windows += 1
            else:
                ws.undecided_windows += 1
            if res.activity_score is not None:
                ws.max_score = res.activity_score if ws.max_score is None else max(ws.max_score, res.activity_score)

    # ----------------------------------------------------------------- events
    def on_event(self, ev: SourceEvent) -> None:
        with self._lock:
            if isinstance(ev, FrameEvent):
                self._on_frame(ev.frame)
            elif isinstance(ev, LinkEvent):
                self._on_link_event(ev)
            elif isinstance(ev, EndOfStream):
                for link in self._links.values():
                    link.offline_reason = f"END_OF_STREAM: {ev.reason}"

    def _on_link_event(self, ev: LinkEvent) -> None:
        link = self._link(ev.link_id)
        if link is None:
            return
        link.link_events[ev.kind] += 1
        if ev.kind in (DISCONNECTED, RECONNECTING):
            link.offline_reason = f"{ev.kind}: {ev.detail}" if ev.detail else ev.kind
        elif ev.kind == CONNECTED:
            # Connected is not "data flowing": staleness still decides until frames arrive.
            link.offline_reason = None
        elif ev.kind == HELLO:
            rate = ev.data.get("rate_hz") if isinstance(ev.data, dict) else None
            if isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 < rate <= 1000:
                self._set_expected_rate(link, float(rate))

    def _on_frame(self, frame: CsiFrame) -> None:
        if self._session_id is not None and (
            frame.session_id != self._session_id or frame.source_mode != self._source_mode
        ):
            # Left over from a previous source: never mixed into this session.
            self._foreign_frames += 1
            return
        link = self._link(frame.link_id)
        if link is None:
            return
        now = self._clock_ns()
        link.frames_total += 1
        arrival = frame.host_arrival_monotonic_ns if frame.host_arrival_monotonic_ns is not None else now
        link.last_any_ns = arrival

        if link.session_id is not None and (
            frame.session_id != link.session_id or frame.source_mode != link.source_mode
        ):
            self._invalidate(link, f"SOURCE_CHANGED: session/source changed on {link.link_id}")
            link.reset_data()
            link.identity = None
        link.session_id, link.source_mode = frame.session_id, frame.source_mode

        out = convert_frame(frame, self._pcfg)
        if isinstance(out, FrameRejection):
            link.frames_rejected += 1
            link.rejections[out.reason] += 1
            link.rejected_times.append(arrival)
            return
        sample = out
        self._update_identity(link, frame, sample)
        if link.time_base is not None and sample.time_base != link.time_base:
            link.reset_data()
        link.layout_id, link.time_base = sample.layout_id, sample.time_base
        if sample.t_unix_ns is not None:
            link.unix_offset_ns = sample.t_unix_ns - sample.t_ns
        else:
            # Map the sample clock to wall time via our own receipt time (display only).
            link.unix_offset_ns = self._clock_unix_ns() - (sample.t_ns + (now - arrival))
        if link.samples and sample.t_ns - link.samples[-1].t_ns > self._plot_gap_ns:
            link.frame_gaps.append((link.samples[-1].t_ns, sample.t_ns))
        link.samples.append(sample)
        link.frames_accepted += 1
        if link.recorder is not None:
            link.calib_frames += 1
        link.last_accepted_ns = arrival
        link.offline_reason = None
        if link.last_display_ns is None or sample.t_ns - link.last_display_ns >= DISPLAY_INTERVAL_S * 1e9:
            link.display.append((sample.t_ns, sample.amp))
            link.last_display_ns = sample.t_ns

    def _update_identity(self, link: _Link, frame: CsiFrame, sample: AmplitudeSample) -> None:
        reported = (
            link.link_id,
            frame.device.chip,
            frame.device.firmware_name,
            frame.device.firmware_version,
            frame.channel,
            frame.secondary_channel,
            sample.layout_id,
            frame.transmitter_mac,
        )
        old = link.identity
        if old is None:
            merged = reported
        else:
            merged = tuple(r if r is not None else o for r, o in zip(reported, old))
            changed = [
                name for name, o, r in zip(_IDENTITY_FIELDS, old, reported)
                if o is not None and r is not None and o != r
            ]
            if changed:
                if "layout_id" in changed:
                    reason = f"LAYOUT_CHANGED: CSI layout changed from {old[6]} to {reported[6]}"
                else:
                    reason = f"HARDWARE_SIGNATURE_CHANGED: {', '.join(changed)} changed on {link.link_id}"
                self._invalidate(link, reason)
                link.reset_data()
                merged = reported
        link.identity = merged
        link.signature = canonical_hash(list(merged))
        link.signature_complete = all(v is not None for v in merged)
        link.detector.set_hardware_signature(link.signature, complete=link.signature_complete)

    # ----------------------------------------------------------------- stepping
    def step(self, now_ns: int | None = None) -> list[ActivityResult]:
        """Compute due windows and staleness for every link; return new results."""
        with self._lock:
            now = self._clock_ns() if now_ns is None else int(now_ns)
            self._last_now_ns = now
            out: list[ActivityResult] = []
            for link_id in sorted(self._links):
                res = self._step_link(self._links[link_id], now)
                if res is not None:
                    out.append(res)
            return out

    def _step_link(self, link: _Link, now: int) -> ActivityResult | None:
        if link.source_mode is None or link.session_id is None:
            return None  # cannot attribute provenance honestly yet
        age = None if link.last_accepted_ns is None else max(0.0, (now - link.last_accepted_ns) / 1e9)
        stale = age is None or age > self.cfg.acquisition.stale_after_s
        if link.offline_reason is not None or stale or not link.samples:
            # The transition to offline is reported at once; repeats at most once per hop.
            if link.offline_emitted and link.last_emit_ns is not None and now - link.last_emit_ns < self._hop_ns:
                return None
            reason = link.offline_reason
            if reason is None and link.last_any_ns is not None and link.frames_rejected:
                any_age = (now - link.last_any_ns) / 1e9
                if any_age <= self.cfg.acquisition.stale_after_s:
                    top = ", ".join(f"{k} x{v}" for k, v in link.rejections.most_common(3))
                    reason = f"FRAMES_REJECTED: frames arrive but none are usable ({top})"
            res = link.detector.tick(now, age, self._provenance(link, None, age), offline_reason=reason)
            if res is not None:
                self._record(link, res, self._sample_time(link, now), now)
                link.offline_emitted = True
            return res

        latest_t = link.samples[-1].t_ns
        if link.last_window_end_ns is not None and latest_t - link.last_window_end_ns < self._hop_ns:
            return None
        link.last_window_end_ns = latest_t
        window = build_window(link.samples, end_ns=latest_t, cfg=self._pcfg, link_id=link.link_id)
        # Rejected frames carry only an arrival time, so count them on the
        # arrival clock over the same span as the window.
        arr_end = link.last_accepted_ns if link.last_accepted_ns is not None else latest_t
        n_rej = sum(1 for t in link.rejected_times if arr_end - self._window_ns <= t <= arr_end)
        quality = assess_quality(window, expected_rate_hz=link.expected_rate_hz, cfg=self._pcfg,
                                 rejected_frames=n_rej)
        fv: FeatureVector | None = None
        if isinstance(window, Window):
            fv = extract_features(window, self._pcfg)
            link.features.append(fv)
            link.latest_fv = fv
            if link.recorder is not None:
                link.recorder.add(fv, quality)
        res = link.detector.update(fv, quality, self._provenance(link, window, age), now_ns=latest_t)
        if isinstance(window, WindowRejection):
            res = res.model_copy(update={"reasons": list(res.reasons) + [f"WINDOW_REJECTED: {window.reason}: "
                                                                          f"{window.detail}"]})
        self._record(link, res, latest_t, now)
        link.offline_emitted = False
        return res

    # ----------------------------------------------------------------- readers
    def latest(self) -> dict[str, ActivityResult]:
        with self._lock:
            return {lid: link.latest for lid, link in self._links.items() if link.latest is not None}

    def link_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._links)

    def history(self, link_id: str, seconds: float) -> list[ActivityResult]:
        with self._lock:
            link = self._links.get(link_id)
            if link is None or not link.results:
                return []
            ref = link.results[-1][0]
            lo = ref - int(max(0.0, seconds) * 1e9)
            return [r for t, r in link.results if t >= lo]

    def feature_history(self, link_id: str, seconds: float) -> list[FeatureVector]:
        with self._lock:
            link = self._links.get(link_id)
            if link is None or not link.features:
                return []
            lo = link.features[-1].t_end_ns - int(max(0.0, seconds) * 1e9)
            return [fv for fv in link.features if fv.t_end_ns >= lo]

    def link_stats(self) -> dict[str, dict[str, Any]]:
        """Per-link counters for diagnostics (frames, rejections by reason)."""
        with self._lock:
            return {
                lid: {
                    "frames_total": link.frames_total,
                    "frames_accepted": link.frames_accepted,
                    "frames_rejected": link.frames_rejected,
                    "rejections": dict(link.rejections),
                    "link_events": dict(link.link_events),
                    "layout_id": link.layout_id,
                    "expected_rate_hz": link.expected_rate_hz,
                    "offline_reason": link.offline_reason,
                    "signature": link.signature,
                    "signature_complete": link.signature_complete,
                }
                for lid, link in self._links.items()
            }

    def engine_stats(self) -> dict[str, Any]:
        """Frames dropped before reaching any link (other session / link cap)."""
        with self._lock:
            return {
                "session_id": self._session_id,
                "source_mode": None if self._source_mode is None else self._source_mode.value,
                "foreign_session_frames": self._foreign_frames,
                "dropped_new_links": self._dropped_new_links,
                "links": len(self._links),
            }

    def drift_status(self, link_id: str | None = None) -> dict[str, dict[str, Any]]:
        with self._lock:
            ids = [link_id] if link_id is not None else sorted(self._links)
            return {lid: self._links[lid].detector.drift_status() for lid in ids if lid in self._links}

    def baseline_status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                lid: {
                    "has_baseline": link.detector.baseline is not None,
                    "calibration_id": link.calibration_id if link.detector.baseline is not None else None,
                    "invalid_reason": link.detector.invalid_reason,
                    "calibrating": link.detector.calibrating,
                }
                for lid, link in self._links.items()
            }

    def hardware_signature(self) -> str | None:
        """Hash of every link's identity tuple, or ``None`` before any frame."""
        with self._lock:
            idents = sorted(
                (list(link.identity) for link in self._links.values() if link.identity is not None),
                key=lambda x: str(x[0]),
            )
            return canonical_hash(idents) if idents else None

    # ----------------------------------------------------------------- snapshot
    def signal_snapshot(self, link_id: str, seconds: float) -> dict[str, Any]:
        """Plot data for one link in the documented ``SignalSnapshot`` shape.

        Timestamps are Unix **milliseconds**. A ``null`` value, or a ``null``
        row in ``amplitude.v``, marks a gap that plots must not bridge.
        ``amplitude.v`` is indexed ``[time][subcarrier]`` and aligned with
        ``amplitude.t`` and ``amplitude.k``. At most ``MAX_SNAPSHOT_POINTS``
        time points (gap markers included) and ``MAX_SNAPSHOT_SUBCARRIERS``
        evenly spaced valid subcarriers are returned, sorted by subcarrier
        index.
        """
        seconds = float(min(max(seconds, 0.0), HISTORY_SECONDS))
        with self._lock:
            link = self._links.get(link_id)
            mode = (link.source_mode if link is not None and link.source_mode is not None else self._source_mode)
            snap: dict[str, Any] = {
                "link_id": link_id,
                "source_mode": None if mode is None else mode.value,
                "seconds": seconds,
                "score": {"t": [], "v": [], "state": []},
                "enter_threshold": self._dcfg.enter_threshold,
                "exit_threshold": self._dcfg.exit_threshold,
                "rate_hz": {"t": [], "v": []},
                "rssi_dbm": {"t": [], "v": []},
                "amplitude": {"t": [], "k": [], "v": []},
                "latest_profile": {"t": None, "k": [], "amp": []},
                "gaps": [],
            }
            if link is None:
                return snap
            refs = [x for x in (None if self._last_now_ns is None else self._sample_time(link, self._last_now_ns),
                                link.samples[-1].t_ns if link.samples else None,
                                link.results[-1][0] if link.results else None) if x is not None]
            if not refs:
                return snap
            ref = max(refs)
            lo = ref - int(seconds * 1e9)
            offset = link.unix_offset_ns
            if offset is None:
                offset = self._clock_unix_ns() - self._clock_ns()

            def ms(t_ns: float) -> float:
                return round((t_ns + offset) / 1e6, 3)

            # Score / rate / RSSI share the results time axis.
            t_s: list[float] = []
            score_v: list[float | None] = []
            states: list[str | None] = []
            rate_v: list[float | None] = []
            rssi_v: list[float | None] = []
            prev: int | None = None
            for t_ref, res in link.results:
                if t_ref < lo:
                    continue
                if prev is not None and t_ref - prev > self._plot_gap_ns:
                    t_s.append(ms((prev + t_ref) / 2))
                    score_v.append(None)
                    states.append(None)
                    rate_v.append(None)
                    rssi_v.append(None)
                t_s.append(ms(t_ref))
                score_v.append(res.activity_score)
                states.append(ActivityState(res.state).value)
                rate_v.append(res.quality.packet_rate_hz)
                rssi_v.append(res.quality.rssi_dbm_median)
                prev = t_ref
            snap["score"] = {"t": t_s, "v": score_v, "state": states}
            snap["rate_hz"] = {"t": list(t_s), "v": rate_v}
            snap["rssi_dbm"] = {"t": list(t_s), "v": rssi_v}

            snap["amplitude"] = self._amplitude_series(link, lo, ms)

            fv = link.latest_fv
            if fv is not None:
                order = np.argsort(fv.k, kind="stable")
                t_prof = fv.t_unix_end_ns / 1e6 if fv.t_unix_end_ns is not None else ms(fv.t_end_ns)
                snap["latest_profile"] = {
                    "t": round(float(t_prof), 3),
                    "k": [int(v) for v in fv.k[order]],
                    "amp": _nan_list(fv.profile[order]),
                }

            gaps = [{"start": ms(a), "end": ms(b)} for a, b in link.frame_gaps if b >= lo]
            last_t = link.samples[-1].t_ns if link.samples else None
            if last_t is not None and ref - last_t > self._plot_gap_ns:
                gaps.append({"start": ms(max(last_t, lo)), "end": ms(ref)})
            snap["gaps"] = gaps
            return snap

    def _amplitude_series(self, link: _Link, lo: int, ms: Callable[[float], float]) -> dict[str, Any]:
        pts = [(t, a) for t, a in link.display if t >= lo]
        if not pts:
            return {"t": [], "k": [], "v": []}
        k_all: np.ndarray
        if link.latest_fv is not None:
            k_all, valid = np.asarray(link.latest_fv.k), np.asarray(link.latest_fv.valid, dtype=bool)
        else:
            layout = cached_layout(link.layout_id)
            if layout is None:
                return {"t": [], "k": [], "v": []}
            k_all, valid = layout.k_indices(), layout.valid_position_mask()
        if valid.shape != pts[-1][1].shape:
            return {"t": [], "k": [], "v": []}
        pos = np.flatnonzero(valid)
        if pos.size == 0:
            return {"t": [], "k": [], "v": []}
        pos = pos[np.argsort(k_all[pos], kind="stable")]
        if pos.size > MAX_SNAPSHOT_SUBCARRIERS:
            pick = np.unique(np.round(np.linspace(0, pos.size - 1, MAX_SNAPSHOT_SUBCARRIERS)).astype(int))
            pos = pos[pick]
        pts = [(t, a) for t, a in pts if a.shape == valid.shape]
        t_arr = np.array([t for t, _ in pts], dtype=np.float64)
        v_arr = np.vstack([a[pos] for _, a in pts]).astype(np.float64)
        gap_starts = np.array(sorted(a for a, _ in link.frame_gaps), dtype=np.float64)
        seg = np.searchsorted(gap_starts, t_arr, side="left") if gap_starts.size else np.zeros(t_arr.size, dtype=int)
        t_out, v_out = _downsample_segments(t_arr, v_arr, seg, MAX_SNAPSHOT_POINTS)
        width = int(pos.size)
        return {
            "t": [ms(t) for t in t_out],
            "k": [int(v) for v in k_all[pos]],
            "v": [row if row is not None else [None] * width for row in v_out],
        }

    # ----------------------------------------------------------------- calibration
    def start_baseline(self, link_ids: list[str] | None = None) -> None:
        """Start recording a quiet baseline on the given (default: all) links."""
        with self._lock:
            ids = sorted(self._links) if link_ids is None else list(dict.fromkeys(link_ids))
            if not ids:
                raise ValueError("no links to calibrate: start a source and wait for frames first")
            self._cancel_baseline_locked()
            for lid in ids:
                link = self._link(lid)
                if link is None:
                    raise ValueError(f"too many links; cannot calibrate {lid!r}")
                rec = BaselineRecorder(lid, self._dcfg)
                rec.set_source_mode(None if link.source_mode is None else link.source_mode.value)
                link.recorder = rec
                link.calib_frames = 0
                link.detector.set_calibrating(True)
            self._calibrating = set(ids)

    def calibration_progress(self) -> dict[str, dict[str, Any]]:
        """Per-link progress of the running quiet-baseline recording."""
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for lid in sorted(self._calibrating):
                link = self._links.get(lid)
                if link is not None and link.recorder is not None:
                    out[lid] = {**link.recorder.progress(), "frames_accepted": link.calib_frames}
            return out

    def stop_baseline(self) -> dict[str, tuple[Baseline | None, list[str]]]:
        """Finish recording. Returns candidates; does NOT apply them."""
        with self._lock:
            cal_id = f"cal-{uuid.uuid4().hex[:16]}"
            hw = self.hardware_signature() or "unavailable"
            out: dict[str, tuple[Baseline | None, list[str]]] = {}
            for lid in sorted(self._calibrating):
                link = self._links.get(lid)
                if link is None or link.recorder is None:
                    out[lid] = (None, ["NO_USABLE_WINDOWS: link state was reset during calibration"])
                    continue
                b, reasons = link.recorder.finalize(
                    calibration_id=cal_id,
                    hardware_signature=hw,
                    config_version=self._config_version,
                    link_signature=link.signature,
                )
                if b is not None:
                    b = dataclasses.replace(b, summary={**b.summary, "frames_accepted": link.calib_frames})
                out[lid] = (b, reasons)
                link.recorder = None
                link.detector.set_calibrating(False)
            self._calibrating = set()
            return out

    def _cancel_baseline_locked(self) -> None:
        for lid in self._calibrating:
            link = self._links.get(lid)
            if link is not None:
                link.recorder = None
                link.detector.set_calibrating(False)
        self._calibrating = set()

    def cancel_baseline(self) -> None:
        with self._lock:
            self._cancel_baseline_locked()

    def is_calibrating(self) -> bool:
        with self._lock:
            return bool(self._calibrating)

    def set_baselines(self, baselines: dict[str, Baseline], calibration_id: str | None) -> None:
        """Apply baselines (replacing all current ones). Links not in the dict
        end up without a baseline."""
        with self._lock:
            for lid, b in baselines.items():
                if b.link_id != lid:
                    raise ValueError(f"baseline for {b.link_id!r} supplied under key {lid!r}")
            for lid, link in self._links.items():
                if lid not in baselines:
                    link.detector.set_baseline(None)
                    link.calibration_id = None
            for lid, b in baselines.items():
                link = self._link(lid)
                if link is None:
                    continue
                link.detector.set_baseline(b)
                link.calibration_id = calibration_id if calibration_id is not None else b.calibration_id

    def invalidate_baselines(self, reason: str, link_ids: list[str] | None = None) -> None:
        """Explicitly invalidate baselines (e.g. room geometry changed)."""
        with self._lock:
            ids = sorted(self._links) if link_ids is None else link_ids
            for lid in ids:
                link = self._links.get(lid)
                if link is not None:
                    self._invalidate(link, reason)

    # ----------------------------------------------------------------- walk test
    def start_walk_test(self, link_ids: list[str] | None = None) -> None:
        with self._lock:
            ids = sorted(self._links) if link_ids is None else list(dict.fromkeys(link_ids))
            self._walk = {lid: _WalkStats() for lid in ids}

    def stop_walk_test(self) -> dict[str, dict[str, Any]]:
        """Per-link report. ``detected`` is True only if MOTION_DETECTED was
        actually reported during the test."""
        with self._lock:
            walk, self._walk = self._walk, None
            if walk is None:
                return {}
            report: dict[str, dict[str, Any]] = {}
            for lid, ws in walk.items():
                reasons: list[str] = []
                if ws.windows == 0:
                    reasons.append("NO_DECISION_WINDOWS: no baseline, low quality or offline during the walk test")
                report[lid] = {
                    "max_score": ws.max_score,
                    "motion_window_fraction": (ws.motion_windows / ws.windows) if ws.windows else None,
                    "windows": ws.windows,
                    "motion_windows": ws.motion_windows,
                    "undecided_windows": ws.undecided_windows,
                    "offline_windows": ws.offline_windows,
                    "detected": ws.motion_windows > 0,
                    "reasons": reasons,
                }
            return report

    # ----------------------------------------------------------------- reset
    def reset(self, session_id: str, source_mode: SourceMode) -> None:
        """Forget everything (buffers, history, baselines, calibration) for a
        new source. Baselines must be re-applied explicitly."""
        with self._lock:
            self._links.clear()
            self._session_id = session_id
            self._source_mode = SourceMode(source_mode)
            self._foreign_frames = 0
            self._dropped_new_links = 0
            self._calibrating = set()
            self._walk = None
            self._last_now_ns = None
