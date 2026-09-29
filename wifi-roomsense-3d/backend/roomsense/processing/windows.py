"""Gap-aware analysis windows over amplitude samples.

A window never bridges a long gap. Samples in ``[end - window_s, end]`` are
split wherever consecutive arrivals are more than ``max_gap_s`` apart and only
the most recent contiguous segment is analysed. Older segments are discarded,
never stitched to the newest one, and nothing is synthesised across a long gap.

Optional resampling (``ProcessingConfig.resample_hz``) happens only inside the
chosen segment. By construction every inter-arrival interval there is at most
``max_gap_s``, and a grid point is only filled when the two measured points
around it (for that subcarrier) are at most ``max_gap_s`` apart. The default
is no resampling. Features are written for irregular sampling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..config import ProcessingConfig
from .amplitude import AmplitudeSample, cached_layout

__all__ = [
    "Window",
    "WindowRejection",
    "NO_DATA",
    "INSUFFICIENT_FRAMES",
    "INSUFFICIENT_CONTIGUOUS_SPAN",
    "LAYOUT_CHANGED",
    "TIME_BASE_CHANGED",
    "MIN_SPAN_FRACTION",
    "SUBCARRIER_VALID_FRACTION",
    "build_window",
]

NO_DATA = "NO_DATA"
INSUFFICIENT_FRAMES = "INSUFFICIENT_FRAMES"
INSUFFICIENT_CONTIGUOUS_SPAN = "INSUFFICIENT_CONTIGUOUS_SPAN"
LAYOUT_CHANGED = "LAYOUT_CHANGED"
TIME_BASE_CHANGED = "TIME_BASE_CHANGED"

# The newest contiguous segment must cover at least this fraction of window_s.
MIN_SPAN_FRACTION = 0.5
# A subcarrier is analysed only if it is valid (and finite) in at least this
# fraction of the segment's frames.
SUBCARRIER_VALID_FRACTION = 0.9


@dataclass(frozen=True)
class Window:
    """One analysis window for one link (the newest contiguous segment).

    ``t`` is the time axis of ``amp`` in seconds relative to ``t_start_ns``
    (the raw arrival times unless ``resampled``). ``t_frames`` always holds the
    raw arrival times, so quality metrics are measured on real packets even
    when ``amp`` was resampled. ``gaps`` lists every inter-arrival gap longer
    than ``max_gap_s`` that overlaps the requested interval, as
    ``(start_ns, end_ns)`` on the sample time base (a hole that began before
    the interval is included when an earlier sample shows where it began).
    """

    link_id: str
    t_start_ns: int
    t_end_ns: int
    t: np.ndarray
    amp: np.ndarray  # (len(t), n_positions) float, NaN where not valid
    valid: np.ndarray  # (n_positions,) bool: analysed subcarriers
    counters: tuple[int | None, ...]
    rssi: tuple[int | None, ...]
    n_frames: int
    gaps: list[tuple[int, int]]
    k: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))
    occupied: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    layout_id: str = ""
    time_base: str = ""
    window_s: float = 0.0
    requested_end_ns: int = 0
    t_frames: np.ndarray = field(default_factory=lambda: np.zeros(0))
    t_unix_start_ns: int | None = None
    t_unix_end_ns: int | None = None
    counter_sources: tuple[str | None, ...] = ()
    frames_discarded_before_gap: int = 0
    resampled: bool = False
    flags: tuple[str, ...] = ()

    @property
    def span_s(self) -> float:
        return (self.t_end_ns - self.t_start_ns) / 1e9


@dataclass(frozen=True)
class WindowRejection:
    link_id: str
    reason: str
    detail: str
    n_frames: int = 0  # frames that were inside the requested interval
    gaps: list[tuple[int, int]] = field(default_factory=list)
    requested_end_ns: int = 0


def _resample(t: np.ndarray, amp: np.ndarray, valid: np.ndarray, rate_hz: float, max_gap_s: float
              ) -> tuple[np.ndarray, np.ndarray]:
    """Linear resampling inside one contiguous segment, amplitude only."""
    step = 1.0 / rate_hz
    grid = np.arange(0.0, float(t[-1]) + step * 1e-6, step)
    out = np.full((grid.size, amp.shape[1]), np.nan, dtype=np.float64)
    for j in np.flatnonzero(valid):
        col = amp[:, j]
        ok = np.isfinite(col)
        tf = t[ok]
        if tf.size < 2:
            continue
        yf = col[ok].astype(np.float64)
        idx = np.clip(np.searchsorted(tf, grid, side="left"), 1, tf.size - 1)
        left, right = tf[idx - 1], tf[idx]
        # Only fill a grid point when its two measured neighbours are close:
        # invalid frames inside the segment must not become a long bridge.
        fill = (grid >= tf[0]) & (grid <= tf[-1]) & ((right - left) <= max_gap_s)
        out[fill, j] = np.interp(grid[fill], tf, yf)
    return grid, out


def build_window(
    samples: Sequence[AmplitudeSample],
    *,
    end_ns: int,
    cfg: ProcessingConfig,
    link_id: str,
) -> Window | WindowRejection:
    """Build the newest gap-free window ending at ``end_ns`` (inclusive)."""
    window_ns = int(round(cfg.window_s * 1e9))
    max_gap_ns = int(round(cfg.max_gap_s * 1e9))
    start_ns = end_ns - window_ns

    sel = [s for s in samples if start_ns <= s.t_ns <= end_ns]
    if not sel:
        return WindowRejection(link_id, NO_DATA, f"no accepted frames in the last {cfg.window_s:g} s",
                               requested_end_ns=end_ns)
    # Stable sort: device timestamps can step backwards; host monotonic cannot.
    sel.sort(key=lambda s: s.t_ns)

    layouts = {s.layout_id for s in sel}
    if len(layouts) > 1:
        return WindowRejection(link_id, LAYOUT_CHANGED,
                               f"window mixes CSI layouts {sorted(layouts)}; never combined", len(sel),
                               requested_end_ns=end_ns)
    bases = {s.time_base for s in sel}
    if len(bases) > 1:
        return WindowRejection(link_id, TIME_BASE_CHANGED,
                               f"window mixes time bases {sorted(bases)}; never combined", len(sel),
                               requested_end_ns=end_ns)

    t_all = np.fromiter((s.t_ns for s in sel), dtype=np.int64, count=len(sel))
    dt_all = np.diff(t_all)
    gap_idx = np.flatnonzero(dt_all > max_gap_ns)
    gaps = [(int(t_all[i]), int(t_all[i + 1])) for i in gap_idx]
    # A hole that began before the requested start is still a hole in this
    # window, provided an earlier sample proves that data existed before it
    # (at start-up there is no such sample and nothing is claimed).
    earlier = [s.t_ns for s in samples if s.t_ns < start_ns and s.time_base == sel[0].time_base]
    if earlier:
        prev_t = max(earlier)
        if int(t_all[0]) - prev_t > max_gap_ns:
            gaps.insert(0, (int(prev_t), int(t_all[0])))
    seg_start = int(gap_idx[-1]) + 1 if gap_idx.size else 0
    seg = sel[seg_start:]
    n = len(seg)
    span_s = (seg[-1].t_ns - seg[0].t_ns) / 1e9

    if n < cfg.min_frames_per_window:
        detail = f"{n} frames in the newest contiguous segment; need {cfg.min_frames_per_window}"
        if gaps:
            detail += f" ({len(gaps)} gap(s) > {cfg.max_gap_s:g} s split the window)"
        return WindowRejection(link_id, INSUFFICIENT_FRAMES, detail, len(sel), gaps, end_ns)
    if span_s < MIN_SPAN_FRACTION * cfg.window_s:
        detail = (f"newest contiguous segment spans {span_s:.2f} s; need "
                  f"{MIN_SPAN_FRACTION * cfg.window_s:.2f} s ({MIN_SPAN_FRACTION:.0%} of window_s)")
        if gaps:
            detail += f" ({len(gaps)} gap(s) > {cfg.max_gap_s:g} s split the window)"
        return WindowRejection(link_id, INSUFFICIENT_CONTIGUOUS_SPAN, detail, len(sel), gaps, end_ns)

    t0 = seg[0].t_ns
    t_rel = (np.fromiter((s.t_ns for s in seg), dtype=np.int64, count=n) - t0) / 1e9
    amp = np.vstack([s.amp for s in seg]).astype(np.float64)
    frame_valid = np.vstack([s.valid for s in seg]) & np.isfinite(amp)
    amp[~frame_valid] = np.nan
    valid = frame_valid.mean(axis=0) >= SUBCARRIER_VALID_FRACTION
    amp[:, ~valid] = np.nan

    layout = cached_layout(seg[0].layout_id)
    occupied = layout.valid_position_mask() if layout is not None else valid.copy()

    t_axis = t_rel
    resampled = False
    if cfg.resample_hz is not None and n >= 2:
        t_axis, amp = _resample(t_rel, amp, valid, cfg.resample_hz, cfg.max_gap_s)
        resampled = True

    flags = sorted({f for s in seg for f in s.flags})
    for arr in (t_axis, amp, valid, t_rel):
        arr.setflags(write=False)
    return Window(
        link_id=link_id,
        t_start_ns=int(t0),
        t_end_ns=int(seg[-1].t_ns),
        t=t_axis,
        amp=amp,
        valid=valid,
        counters=tuple(s.counter for s in seg),
        rssi=tuple(s.rssi for s in seg),
        n_frames=n,
        gaps=gaps,
        k=seg[0].k,
        occupied=occupied,
        layout_id=seg[0].layout_id,
        time_base=seg[0].time_base,
        window_s=cfg.window_s,
        requested_end_ns=end_ns,
        t_frames=t_rel,
        t_unix_start_ns=seg[0].t_unix_ns,
        t_unix_end_ns=seg[-1].t_unix_ns,
        counter_sources=tuple(s.counter_source for s in seg),
        frames_discarded_before_gap=seg_start,
        resampled=resampled,
        flags=tuple(flags),
    )
