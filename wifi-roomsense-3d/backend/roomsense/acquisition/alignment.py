"""Host/device clock bookkeeping and coarse multi-receiver window alignment.

What this module does NOT do, stated plainly: host receipt times do NOT
create RF phase synchronisation between receivers. Each ESP32 has its own
free-running oscillator, carrier-frequency offset and sampling offset, and USB
serial adds milliseconds of variable latency. No angle-of-arrival or
time-of-flight positioning is possible from these timestamps, and nothing in
RoomSense attempts it.

What it does:

* :class:`ClockModel` fits ``host_monotonic_ns ≈ a + b * device_us * 1000``
  for one receiver, using a robust (Theil-Sen) line on a decimated, bounded
  history. ``drift_ppm = (b - 1) * 1e6`` describes how fast the board's
  microsecond counter runs relative to the host clock. ``offset_ms`` is the
  accumulated offset since the first sample (drift integrated over the
  tracked period), and ``residual_ms`` is a robust spread of host receipt
  latency around the fit. These are diagnostics for the UI and for gap
  detection, not inputs to any localisation.
* :func:`align_link_windows` checks that analysis windows from different
  receivers end close enough in host time to be combined for coarse,
  window-level features (e.g. "which links saw motion in the same second").
"""

from __future__ import annotations

from collections import deque

import numpy as np
from scipy import stats

__all__ = ["ClockModel", "align_link_windows"]


class ClockModel:
    """Robust linear model of host receipt time vs device timestamp.

    Feed *unwrapped* device timestamps (see :class:`~.rollover.TimestampUnwrapper`).
    A decrease in the device timestamp means a new clock epoch (reboot or
    reset), so the history is discarded and the model starts again.

    Memory is bounded: at most ``max_points`` samples are kept, and samples
    closer than ``min_spacing_s`` in host time are skipped so the history
    covers minutes rather than seconds (drift needs a long baseline).
    """

    def __init__(
        self,
        max_points: int = 600,
        *,
        min_spacing_s: float = 0.5,
        min_points: int = 10,
        min_span_s: float = 5.0,
        fit_points: int = 200,
    ) -> None:
        if max_points < 3 or min_points < 3 or fit_points < 3:
            raise ValueError("ClockModel needs at least 3 points")
        self._pts: deque[tuple[int, int]] = deque(maxlen=max_points)
        self._min_spacing_ns = int(min_spacing_s * 1e9)
        self._min_points = min_points
        self._min_span_ns = int(min_span_s * 1e9)
        self._fit_points = fit_points
        self._first: tuple[int, int] | None = None
        self._last_device_us: int | None = None
        self._dirty = True
        self._fit: tuple[float, float, float, float] | None = None  # slope, offset_ms, residual_ms, span
        self.resets = 0

    def reset(self) -> None:
        self._pts.clear()
        self._first = None
        self._last_device_us = None
        self._dirty = True
        self._fit = None
        self.resets += 1

    def update(self, device_ts_us: int, host_monotonic_ns: int) -> None:
        if self._last_device_us is not None and device_ts_us < self._last_device_us:
            self.reset()
        self._last_device_us = device_ts_us
        if self._pts and host_monotonic_ns - self._pts[-1][1] < self._min_spacing_ns:
            return
        if self._first is None:
            self._first = (device_ts_us, host_monotonic_ns)
        self._pts.append((device_ts_us, host_monotonic_ns))
        self._dirty = True

    @property
    def n_points(self) -> int:
        return len(self._pts)

    def _compute(self) -> tuple[float, float, float, float] | None:
        if not self._dirty:
            return self._fit
        self._dirty = False
        self._fit = None
        if len(self._pts) < self._min_points or self._first is None:
            return None
        arr = np.asarray(self._pts, dtype=np.int64)
        # Work relative to the first retained sample: the absolute epochs of
        # the two clocks are unrelated, and small numbers keep float64 exact.
        x = (arr[:, 0] - arr[0, 0]).astype(np.float64) * 1000.0  # device, ns
        y = (arr[:, 1] - arr[0, 1]).astype(np.float64)  # host, ns
        span = float(x[-1] - x[0])
        if span < self._min_span_ns:
            return None
        if len(x) > self._fit_points:
            idx = np.linspace(0, len(x) - 1, self._fit_points).round().astype(np.int64)
            xs, ys = x[idx], y[idx]
        else:
            xs, ys = x, y
        slope, intercept, _, _ = stats.theilslopes(ys, xs)
        resid = y - (intercept + slope * x)
        mad = float(np.median(np.abs(resid - np.median(resid))))
        residual_ms = 1.4826 * mad / 1e6
        # Offset accumulated since the first ever sample of this epoch.
        first_dev_ns = (arr[-1, 0] - self._first[0]) * 1000.0
        offset_ms = float((slope - 1.0) * first_dev_ns / 1e6)
        self._fit = (float(slope), offset_ms, residual_ms, span)
        return self._fit

    def drift_ppm(self) -> float | None:
        fit = self._compute()
        return None if fit is None else (fit[0] - 1.0) * 1e6

    def offset_ms(self) -> float | None:
        fit = self._compute()
        return None if fit is None else fit[1]

    def residual_ms(self) -> float | None:
        fit = self._compute()
        return None if fit is None else fit[2]


def align_link_windows(window_ends_ns: dict[str, int], tolerance_s: float) -> tuple[bool, str]:
    """Decide whether per-link windows can be combined.

    Returns ``(ok, reason)``. Windows are combinable only if every link has a
    window and all end times lie within ``tolerance_s`` of each other. This
    is coarse, window-level alignment on host receipt time only.
    """
    if tolerance_s <= 0:
        raise ValueError("tolerance_s must be > 0")
    if not window_ends_ns:
        return False, "no link windows to align"
    missing = sorted(k for k, v in window_ends_ns.items() if v is None)
    if missing:
        return False, f"no window for link(s): {', '.join(missing)}"
    if len(window_ends_ns) == 1:
        return True, "single link; nothing to align"
    lo_link = min(window_ends_ns, key=lambda k: window_ends_ns[k])
    hi_link = max(window_ends_ns, key=lambda k: window_ends_ns[k])
    spread_ns = window_ends_ns[hi_link] - window_ends_ns[lo_link]
    spread_ms = spread_ns / 1e6
    tol_ms = tolerance_s * 1e3
    if spread_ns <= tolerance_s * 1e9:
        return True, f"window ends within {spread_ms:.1f} ms (tolerance {tol_ms:.0f} ms)"
    return False, (
        f"window ends differ by {spread_ms:.1f} ms between {lo_link} and {hi_link} "
        f"(tolerance {tol_ms:.0f} ms)"
    )
