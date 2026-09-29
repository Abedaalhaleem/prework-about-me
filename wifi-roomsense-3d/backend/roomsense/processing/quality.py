"""Acquisition quality of one analysis window.

Everything here is measured from the frames in the window; nothing is
guessed. In particular ``loss_fraction`` is only computed from device
sequence counters. Without counters it stays ``None``, because arrival-rate
shortfall alone cannot tell over-the-air loss from a lower transmit rate.

Level thresholds (``rate`` means the measured packet rate over the window and
``expected`` the configured/announced transmit rate):

* ``UNAVAILABLE``: no window, a rejected window without any frames, or fewer
  than 2 frames (nothing to measure).
* ``BAD`` if any of:
  - the window was rejected although frames were present (too few frames or
    too short a contiguous span);
  - ``rate < 30 %`` of ``expected``;
  - valid subcarrier fraction ``< ProcessingConfig.min_valid_subcarrier_fraction``;
  - ``loss_fraction > 50 %``.
* ``DEGRADED`` if any of:
  - ``rate < 70 %`` of ``expected``;
  - ``loss_fraction > 20 %``;
  - a gap longer than ``max_gap_s`` inside the requested window;
  - more than 20 % of the frames that arrived in the window were rejected.
* ``GOOD`` otherwise.

If the expected rate is unknown, rate is not used for grading and the flag
``EXPECTED_RATE_UNKNOWN`` is set.
"""

from __future__ import annotations

import numpy as np

from ..config import ProcessingConfig
from ..schemas import QualityLevel, QualityReport
from .windows import Window, WindowRejection

__all__ = [
    "BAD_RATE_FRACTION",
    "DEGRADED_RATE_FRACTION",
    "BAD_LOSS_FRACTION",
    "DEGRADED_LOSS_FRACTION",
    "DEGRADED_REJECTED_FRACTION",
    "QUALITY_ORDER",
    "quality_at_least",
    "assess_quality",
    "loss_from_counters",
]

BAD_RATE_FRACTION = 0.30
DEGRADED_RATE_FRACTION = 0.70
BAD_LOSS_FRACTION = 0.50
DEGRADED_LOSS_FRACTION = 0.20
DEGRADED_REJECTED_FRACTION = 0.20

QUALITY_ORDER: dict[QualityLevel, int] = {
    QualityLevel.UNAVAILABLE: 0,
    QualityLevel.BAD: 1,
    QualityLevel.DEGRADED: 2,
    QualityLevel.GOOD: 3,
}

# Frame flags worth surfacing on the quality report (transparency, not grading).
_SURFACED_FRAME_FLAGS = frozenset(
    {
        "SYNTHETIC",
        "REPLAYED",
        "FIRST_WORD_INVALID",
        "UNDOCUMENTED_LAYOUT_ASSUMPTION",
        "GAIN_COMPENSATED_UPSTREAM",
        "SATURATED_VALUES",
        "COUNTER_RESET",
        "TIMESTAMP_NON_MONOTONIC",
        "FIRMWARE_QUEUE_DROPS",
        "DEVICE_TIME_BASE",
        "HOST_TIMESTAMP_UNAVAILABLE",
    }
)


def quality_at_least(level: QualityLevel | str, minimum: QualityLevel | str) -> bool:
    return QUALITY_ORDER[QualityLevel(level)] >= QUALITY_ORDER[QualityLevel(minimum)]


def loss_from_counters(counters: tuple[int | None, ...], sources: tuple[str | None, ...] = ()
                       ) -> tuple[float | None, list[str]]:
    """Fraction of sequence numbers missing between the first and last frame.

    Returns ``(None, flags)`` whenever the counters cannot support a
    measurement: any counter missing, mixed counter sources, or a counter that
    goes backwards or repeats (device reboot / wrap), which would make the
    count meaningless.
    """
    if any(c is None for c in counters):
        return None, ["NO_COUNTERS"]
    if len(counters) < 2:
        return None, []
    if sources and len({s for s in sources}) > 1:
        return None, ["MIXED_COUNTER_SOURCES"]
    c = np.asarray(counters, dtype=np.int64)
    d = np.diff(c)
    if np.any(d <= 0):
        return None, ["COUNTER_DISCONTINUITY"]
    expected = int(c[-1] - c[0]) + 1
    received = int(c.size)
    return max(0.0, 1.0 - received / expected), []


def assess_quality(
    window: Window | WindowRejection | None,
    *,
    expected_rate_hz: float | None,
    cfg: ProcessingConfig,
    rejected_frames: int = 0,
) -> QualityReport:
    """Grade one window. See the module docstring for the thresholds."""
    if window is None:
        return QualityReport(
            level=QualityLevel.UNAVAILABLE,
            expected_rate_hz=expected_rate_hz,
            rejected_frames=rejected_frames,
            flags=["NO_WINDOW"] + (["FRAMES_REJECTED"] if rejected_frames else []),
        )
    if isinstance(window, WindowRejection):
        max_gap = max(((b - a) / 1e9 for a, b in window.gaps), default=None)
        level = QualityLevel.BAD if window.n_frames > 0 else QualityLevel.UNAVAILABLE
        flags = ["WINDOW_REJECTED", window.reason]
        if rejected_frames:
            flags.append("FRAMES_REJECTED")
        return QualityReport(
            level=level,
            expected_rate_hz=expected_rate_hz,
            max_gap_s=max_gap,
            frames_in_window=window.n_frames,
            rejected_frames=rejected_frames,
            flags=flags,
        )

    n = window.n_frames
    flags: list[str] = [f for f in window.flags if f in _SURFACED_FRAME_FLAGS]
    t = np.asarray(window.t_frames, dtype=np.float64)
    dt = np.diff(t)
    span = float(t[-1] - t[0]) if n >= 2 else 0.0
    rate = (n - 1) / span if span > 0 else None
    jitter_ms = float(np.std(dt) * 1000.0) if dt.size >= 2 else None
    gap_candidates = [float(dt.max())] if dt.size else []
    gap_candidates += [(b - a) / 1e9 for a, b in window.gaps]
    max_gap = max(gap_candidates) if gap_candidates else None

    loss, loss_flags = loss_from_counters(window.counters, window.counter_sources)
    flags += loss_flags

    rssi_vals = [r for r in window.rssi if r is not None]
    rssi_med = float(np.median(rssi_vals)) if rssi_vals else None

    occupied = np.asarray(window.occupied, dtype=bool)
    denom = int(occupied.sum()) if occupied.size == window.valid.size and occupied.any() else window.valid.size
    num = int(np.sum(window.valid & occupied)) if occupied.size == window.valid.size else int(window.valid.sum())
    valid_frac = num / denom if denom else 0.0

    if n < 2:
        level = QualityLevel.UNAVAILABLE
        flags.append("TOO_FEW_FRAMES")
    else:
        bad: list[str] = []
        degraded: list[str] = []
        if expected_rate_hz is None:
            flags.append("EXPECTED_RATE_UNKNOWN")
        elif rate is not None:
            if rate < BAD_RATE_FRACTION * expected_rate_hz:
                bad.append("VERY_LOW_PACKET_RATE")
            elif rate < DEGRADED_RATE_FRACTION * expected_rate_hz:
                degraded.append("LOW_PACKET_RATE")
        if valid_frac < cfg.min_valid_subcarrier_fraction:
            bad.append("LOW_VALID_SUBCARRIER_FRACTION")
        if loss is not None:
            if loss > BAD_LOSS_FRACTION:
                bad.append("VERY_HIGH_PACKET_LOSS")
            elif loss > DEGRADED_LOSS_FRACTION:
                degraded.append("HIGH_PACKET_LOSS")
        if max_gap is not None and max_gap > cfg.max_gap_s:
            degraded.append("GAP_IN_WINDOW")
        arrived = n + rejected_frames
        if rejected_frames and arrived and rejected_frames / arrived > DEGRADED_REJECTED_FRACTION:
            degraded.append("MANY_REJECTED_FRAMES")
        if window.resampled:
            flags.append("RESAMPLED_WITHIN_SHORT_GAPS")
        flags += bad + degraded
        level = QualityLevel.BAD if bad else (QualityLevel.DEGRADED if degraded else QualityLevel.GOOD)

    return QualityReport(
        level=level,
        packet_rate_hz=rate,
        expected_rate_hz=expected_rate_hz,
        loss_fraction=loss,
        max_gap_s=max_gap,
        timing_jitter_ms=jitter_ms,
        rssi_dbm_median=rssi_med,
        valid_subcarrier_fraction=valid_frac,
        frames_in_window=n,
        rejected_frames=rejected_frames,
        flags=flags,
    )
