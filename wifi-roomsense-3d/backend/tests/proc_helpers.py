"""Synthetic CSI frames for processing tests (SIMULATION / SYNTHETIC only).

These frames come from a deliberately simple made-up model. Each subcarrier
has a static, frequency-selective amplitude with small receiver noise, and
"motion" multiplies it by a slow random modulation. They exercise code paths
only. Detector results on this data say nothing about sensing accuracy on
real hardware.

The acquisition package's own generator is not imported on purpose. The
processing tests must not depend on another module's implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from roomsense.acquisition.base import FrameEvent
from roomsense.config import AppConfig, DetectionConfig, ProcessingConfig
from roomsense.csi_layouts import resolve_layout
from roomsense.schemas import CsiFrame, DeviceIdentity, InputFormat, QualityFlag, SourceMode

LAYOUT = resolve_layout(
    family="classic", total_values=128, ltf_config="lltf_only", secondary_channel=0, sig_mode=1, cwb=0, stbc=0
)
assert LAYOUT is not None
LAYOUT_ID = LAYOUT.layout_id

BASE_MONO_NS = 1_000_000_000_000  # arbitrary monotonic origin (1000 s)
BASE_UNIX_NS = 1_790_000_000_000_000_000  # arbitrary wall-clock origin
SESSION = "sess-test"
LINK = "tx1->rx1"
RX = "rx1"
TX = "tx1"
TX_MAC = "1a:00:00:00:00:01"


def t_ns(t_s: float) -> int:
    return BASE_MONO_NS + int(round(t_s * 1e9))


def raw_from_complex(csi: np.ndarray) -> tuple[int, ...]:
    """Interleave (imag, real) int8 values in buffer order, as ESP-IDF does."""
    re = np.clip(np.round(csi.real), -128, 127).astype(int)
    im = np.clip(np.round(csi.imag), -128, 127).astype(int)
    out = np.empty(2 * csi.size, dtype=int)
    out[0::2] = im
    out[1::2] = re
    return tuple(int(v) for v in out)


def make_frame(
    raw: Sequence[int],
    *,
    t_s: float | None,
    counter: int | None = None,
    link_id: str = LINK,
    session_id: str = SESSION,
    source_mode: SourceMode = SourceMode.SIMULATION,
    layout_id: str | None = LAYOUT_ID,
    first_word_invalid: bool = False,
    rssi: int | None = -55,
    channel: int | None = 6,
    chip: str | None = "esp32s3",
    firmware_version: str | None = "synthetic-test",
    transmitter_mac: str | None = TX_MAC,
    device_timestamp_us: int | None = None,
    extra_flags: tuple[str, ...] = (),
    unix: bool = True,
) -> CsiFrame:
    tx, rx = link_id.split("->")
    host_mono = None if t_s is None else t_ns(t_s)
    host_unix = None if (t_s is None or not unix) else BASE_UNIX_NS + int(round(t_s * 1e9))
    return CsiFrame(
        source_mode=source_mode,
        session_id=session_id,
        receiver_id=rx,
        transmitter_id=tx,
        link_id=link_id,
        input_format=InputFormat.SYNTHETIC_V1,
        device=DeviceIdentity(receiver_id=rx, chip=chip, firmware_name="proc-test-generator",
                              firmware_version=firmware_version, identity_source="synthetic"),
        frame_counter=counter,
        frame_counter_unwrapped=counter,
        transmitter_counter=None,
        device_timestamp_us=device_timestamp_us,
        device_timestamp_unwrapped_us=device_timestamp_us,
        host_arrival_monotonic_ns=host_mono,
        host_arrival_unix_ns=host_unix,
        transmitter_mac=transmitter_mac,
        channel=channel,
        secondary_channel=0,
        bandwidth_mhz=20,
        sig_mode=1,
        bb_format=None,
        mcs=None,
        rate=None,
        stbc=0,
        rssi_dbm=rssi,
        noise_floor_dbm=None,
        agc_gain=None,
        fft_gain=None,
        sig_len=None,
        rx_state=0,
        antenna=None,
        first_word_invalid=first_word_invalid,
        csi_len=len(raw),
        raw_csi=tuple(int(v) for v in raw),
        values_are_gain_compensated=False,
        layout_id=layout_id,
        valid_subcarriers=None,
        quality_flags=(QualityFlag.SYNTHETIC.value,) + tuple(extra_flags),
    )


@dataclass
class Scenario:
    """Piecewise description of a synthetic stream (times in seconds)."""

    duration_s: float
    rate_hz: float = 25.0
    start_s: float = 0.0
    motion: list[tuple[float, float]] = field(default_factory=list)  # (t0, t1) intervals
    motion_depth: float = 0.25  # relative amplitude modulation during motion
    noise: float = 0.6  # int8 LSB noise on I and Q
    loss_fraction: float = 0.0  # random frame drops (counters keep counting)
    gaps: list[tuple[float, float]] = field(default_factory=list)  # no frames at all
    profile_phase: float = 0.0  # static profile shape; change it to simulate drift
    profile_change_at: float | None = None  # switch to profile_phase2 after this time
    profile_phase2: float = np.pi
    jitter_s: float = 0.002
    seed: int = 1
    link_id: str = LINK
    session_id: str = SESSION
    source_mode: SourceMode = SourceMode.SIMULATION
    channel: int = 6
    first_counter: int = 0


def _static_channel(phase: float) -> np.ndarray:
    """Frequency-selective static channel per buffer position (made up)."""
    k = LAYOUT.k_indices().astype(np.float64)
    amp = 32.0 + 14.0 * np.cos(2 * np.pi * k / 23.0 + phase) + 6.0 * np.sin(2 * np.pi * k / 7.0 + 0.5 * phase)
    ph = 0.37 * k
    return amp * np.exp(1j * ph)


def generate(sc: Scenario) -> list[CsiFrame]:
    """Deterministic list of frames for ``sc`` (every frame SYNTHETIC)."""
    rng = np.random.default_rng(sc.seed)
    n_pos = LAYOUT.n_positions
    h1 = _static_channel(sc.profile_phase)
    h2 = _static_channel(sc.profile_phase2)
    # Motion: slow per-subcarrier modulation, a few components in 0.3..2 Hz.
    freqs = rng.uniform(0.3, 2.0, size=(3, n_pos))
    phases = rng.uniform(0, 2 * np.pi, size=(3, n_pos))
    frames: list[CsiFrame] = []
    n = int(np.floor(sc.duration_s * sc.rate_hz))
    counter = sc.first_counter
    for i in range(n):
        t = sc.start_s + i / sc.rate_hz
        counter += 1
        if any(a <= t < b for a, b in sc.gaps):
            continue
        if sc.loss_fraction and rng.random() < sc.loss_fraction:
            continue
        h = h2 if (sc.profile_change_at is not None and t >= sc.profile_change_at) else h1
        gain = np.ones(n_pos)
        if any(a <= t < b for a, b in sc.motion):
            mod = np.sum(np.sin(2 * np.pi * freqs * t + phases), axis=0) / np.sqrt(3 / 2)
            gain = 1.0 + sc.motion_depth * mod / 2.0
        csi = h * gain + sc.noise * (rng.standard_normal(n_pos) + 1j * rng.standard_normal(n_pos))
        t_actual = t + (rng.uniform(-sc.jitter_s, sc.jitter_s) if sc.jitter_s else 0.0)
        frames.append(
            make_frame(raw_from_complex(csi), t_s=t_actual, counter=counter, link_id=sc.link_id,
                       session_id=sc.session_id, source_mode=sc.source_mode, channel=sc.channel)
        )
    return frames


def events(frames: Iterable[CsiFrame]) -> list[FrameEvent]:
    return [FrameEvent(f) for f in frames]


def fast_cfg(**det_overrides: object) -> AppConfig:
    """AppConfig with a short baseline so tests stay quick."""
    det = dict(
        baseline_min_duration_s=10.0,
        baseline_min_windows=10,
        min_motion_hold_s=1.0,
        min_quiet_hold_s=2.0,
        drift_hold_s=4.0,
        clear_stale_after_s=5.0,
    )
    det.update(det_overrides)
    return AppConfig(processing=ProcessingConfig(), detection=DetectionConfig(**det))


def feed(engine, frames: Sequence[CsiFrame], *, step_every_s: float = 0.1) -> list:
    """Feed frames in time order and step the engine on the data clock."""
    results = []
    next_step = None
    for f in frames:
        engine.on_event(FrameEvent(f))
        now = f.host_arrival_monotonic_ns
        if next_step is None or now >= next_step:
            results.extend(engine.step(now))
            next_step = now + int(step_every_s * 1e9)
    return results


def feature_stream(frames: Sequence[CsiFrame], pcfg: ProcessingConfig | None = None, *,
                   expected_rate_hz: float | None = 25.0, hop_s: float | None = None, link_id: str = LINK):
    """Run frames through amplitude -> window -> quality -> features without
    the engine. Returns a list of ``(window_or_rejection, quality, fv_or_None)``."""
    from roomsense.processing.amplitude import frame_to_amplitude
    from roomsense.processing.features import extract_features
    from roomsense.processing.quality import assess_quality
    from roomsense.processing.windows import Window, build_window

    pcfg = pcfg or ProcessingConfig()
    hop_ns = int((hop_s or pcfg.hop_s) * 1e9)
    samples = [s for s in (frame_to_amplitude(f) for f in frames) if s is not None]
    out = []
    if not samples:
        return out
    end = samples[0].t_ns
    last = samples[-1].t_ns
    i = 0
    while end <= last:
        while i + 1 < len(samples) and samples[i + 1].t_ns <= end:
            i += 1
        w = build_window(samples[: i + 1], end_ns=samples[i].t_ns, cfg=pcfg, link_id=link_id)
        q = assess_quality(w, expected_rate_hz=expected_rate_hz, cfg=pcfg)
        fv = extract_features(w, pcfg) if isinstance(w, Window) else None
        out.append((w, q, fv))
        end += hop_ns
    return out


# --------------------------------------------------------------------------
# Hand-built detector inputs (exact scores; no signal processing involved)
# --------------------------------------------------------------------------

N_POS = LAYOUT.n_positions
VALID_POS = LAYOUT.valid_position_mask()
BASE_PROFILE = np.where(VALID_POS, np.abs(_static_channel(0.0)), np.nan)
CHANGED_PROFILE = np.where(VALID_POS, np.abs(_static_channel(np.pi)), np.nan)


def make_fv(score: float, *, t_s: float, profile: np.ndarray | None = None, layout_id: str = LAYOUT_ID,
            link_id: str = LINK, median: float = 1.0, scale: float = 0.1, spread: float = 0.0):
    """A FeatureVector whose robust z against ``make_baseline()`` is ``score``
    for both features (per-subcarrier z spread by ``spread``)."""
    from roomsense.processing.features import FEATURE_NAMES, FeatureVector

    v = median + score * scale
    per = np.where(VALID_POS, v + spread * scale * np.linspace(-1, 1, N_POS), np.nan)
    prof = BASE_PROFILE if profile is None else profile
    return FeatureVector(
        link_id=link_id,
        t_end_ns=t_ns(t_s),
        names=FEATURE_NAMES,
        values=np.array([v, v]),
        per_subcarrier={n: per.copy() for n in FEATURE_NAMES},
        profile=prof.copy(),
        k=LAYOUT.k_indices(),
        t_start_ns=t_ns(t_s - 2.0),
        valid=VALID_POS.copy(),
        n_frames=50,
        n_valid_subcarriers=int(VALID_POS.sum()),
        layout_id=layout_id,
        t_unix_start_ns=None,
        t_unix_end_ns=None,
    )


def make_baseline(config_version: str, *, link_id: str = LINK, layout_id: str = LAYOUT_ID,
                  link_signature: str | None = "sig-a", source_mode: str | None = "SIMULATION",
                  median: float = 1.0, scale: float = 0.1):
    from roomsense.processing.baseline import Baseline
    from roomsense.processing.features import FEATURE_NAMES

    return Baseline(
        link_id=link_id,
        calibration_id="cal-test",
        feature_median={n: median for n in FEATURE_NAMES},
        feature_scale={n: scale for n in FEATURE_NAMES},
        per_subcarrier_median={n: np.where(VALID_POS, median, np.nan) for n in FEATURE_NAMES},
        profile=BASE_PROFILE.copy(),
        k=LAYOUT.k_indices(),
        n_windows=100,
        duration_s=60.0,
        layout_id=layout_id,
        hardware_signature="hw-all",
        config_version=config_version,
        per_subcarrier_scale={n: np.where(VALID_POS, scale, np.nan) for n in FEATURE_NAMES},
        link_signature=link_signature,
        source_mode=source_mode,
    )


def make_quality(level: str = "GOOD", frames: int = 50, rejected: int = 0):
    from roomsense.schemas import QualityLevel, QualityReport

    return QualityReport(level=QualityLevel(level), frames_in_window=frames, rejected_frames=rejected,
                         packet_rate_hz=25.0, expected_rate_hz=25.0)


def make_prov(*, age: float | None = 0.05, mode: SourceMode = SourceMode.SIMULATION, config_version: str = "cfg-x"):
    from roomsense.schemas import Provenance

    return Provenance(source_mode=mode, session_id=SESSION, link_ids=[LINK], window_start_unix_ns=None,
                      window_end_unix_ns=None, window_frame_count=50, config_version=config_version,
                      computed_at_unix_ns=BASE_UNIX_NS, measurement_age_s=age)
