"""Static-channel drift monitor.

The quiet baseline describes one static multipath configuration. Its
per-subcarrier mean amplitude profile has a characteristic shape (frequency
selective fading). Several things change that shape for a long time: moving
the sensors, moving furniture, a door left open, or a person standing still.
Motion features compared against the old baseline then stop being meaningful.

The monitor computes the Pearson correlation between the current window's
profile and the baseline profile over the subcarriers that are valid in both.
If the correlation stays below ``drift_min_profile_correlation`` for
``drift_hold_s``, drift is *suspected*. The detector then reports ``UNKNOWN``
and never quietly adapts the baseline. A window whose correlation cannot be
computed (fewer than 3 common subcarriers, or a flat profile) counts as
"below". That is the conservative choice: a profile that cannot be compared
cannot vouch for the calibration. The suspicion clears as soon as a window
correlates above the threshold again.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["MIN_COMMON_SUBCARRIERS", "profile_correlation", "DriftMonitor"]

MIN_COMMON_SUBCARRIERS = 3


def profile_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation over commonly finite entries; NaN if undefined."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        return float("nan")
    m = np.isfinite(a) & np.isfinite(b)
    if int(m.sum()) < MIN_COMMON_SUBCARRIERS:
        return float("nan")
    x = a[m] - a[m].mean()
    y = b[m] - b[m].mean()
    den = float(np.sqrt(np.dot(x, x) * np.dot(y, y)))
    if den <= 0.0 or not np.isfinite(den):
        return float("nan")
    return float(np.clip(np.dot(x, y) / den, -1.0, 1.0))


class DriftMonitor:
    """Hold-time rule on profile correlation. Not thread-safe by itself; the
    owning detector/engine serialises access."""

    def __init__(self, min_correlation: float, hold_s: float) -> None:
        self.min_correlation = float(min_correlation)
        self.hold_ns = int(round(hold_s * 1e9))
        self.reset()

    def reset(self) -> None:
        self._below_since_ns: int | None = None
        self._last_corr: float | None = None
        self._last_t_ns: int | None = None
        self._suspected = False

    @property
    def suspected(self) -> bool:
        return self._suspected

    @property
    def last_correlation(self) -> float | None:
        return self._last_corr

    def update(self, corr: float, t_ns: int) -> bool:
        """Feed one window's correlation; returns whether drift is suspected."""
        self._last_t_ns = t_ns
        self._last_corr = float(corr) if np.isfinite(corr) else None
        below = (not np.isfinite(corr)) or corr < self.min_correlation
        if not below:
            self._below_since_ns = None
            self._suspected = False
            return False
        if self._below_since_ns is None:
            self._below_since_ns = t_ns
        self._suspected = (t_ns - self._below_since_ns) >= self.hold_ns
        return self._suspected

    def below_for_s(self) -> float | None:
        if self._below_since_ns is None or self._last_t_ns is None:
            return None
        return (self._last_t_ns - self._below_since_ns) / 1e9

    def status(self) -> dict[str, Any]:
        return {
            "suspected": self._suspected,
            "profile_correlation": self._last_corr,
            "min_profile_correlation": self.min_correlation,
            "below_threshold_for_s": self.below_for_s(),
            "hold_s": self.hold_ns / 1e9,
        }
