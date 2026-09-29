"""Amplitude features for the motion heuristic (capability B).

Per valid subcarrier, over the window's time axis:

* ``amp_cv``: coefficient of variation, ``std / mean``. A moving body changes
  the multipath mix, which makes the amplitude fluctuate. A static scene only
  shows receiver noise.
* ``amp_tdiff``: ``mean |x[i+1] - x[i]| / mean(x)``, using only consecutive
  pairs whose spacing is at most ``max_gap_s``. The value is not divided by dt.
  With irregular packet timing it therefore depends a little on packet rate,
  which is one reason packet rate is part of the quality grade.

The window scalars are the **median across valid subcarriers**, so a few noisy
or faded subcarriers cannot dominate. ``profile`` is the time-mean amplitude
per subcarrier (``NaN`` where invalid). The drift monitor uses it. It is not a
motion feature.

An optional Hampel filter (per subcarrier, over time) first replaces isolated
spikes with the local median. ``hampel_window`` is the half-width in samples
(window length ``2*hampel_window + 1``), and 0 disables the filter.

These features describe signal variation. On their own they say nothing about
where a person is or whether one is present.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ..config import ProcessingConfig
from .amplitude import ensure_phase_disabled
from .windows import Window

__all__ = [
    "FEATURE_NAMES",
    "HAMPEL_REL_SCALE_FLOOR",
    "FeatureVector",
    "hampel_filter",
    "extract_features",
]

FEATURE_NAMES: tuple[str, ...] = ("amp_cv_median", "amp_tdiff_median")

# Hampel scale floor as a fraction of the local median. Without it, a
# quantised, almost-constant int8 amplitude has MAD == 0 and every 1-LSB
# flicker would count as an outlier.
HAMPEL_REL_SCALE_FLOOR = 0.02
_MAD_TO_SIGMA = 1.4826


def _nan_to_none(a: np.ndarray) -> list[float | None]:
    return [float(v) if np.isfinite(v) else None for v in np.asarray(a, dtype=np.float64)]


@dataclass(frozen=True)
class FeatureVector:
    """Features of one window. Arrays are read-only and aligned with ``k``."""

    link_id: str
    t_end_ns: int
    names: tuple[str, ...]
    values: np.ndarray
    per_subcarrier: dict[str, np.ndarray]
    profile: np.ndarray
    k: np.ndarray
    t_start_ns: int = 0
    valid: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    n_frames: int = 0
    n_valid_subcarriers: int = 0
    layout_id: str = ""
    t_unix_start_ns: int | None = None
    t_unix_end_ns: int | None = None
    n_outliers_replaced: int = 0

    def value(self, name: str) -> float:
        return float(self.values[self.names.index(name)])

    def is_finite(self) -> bool:
        return bool(self.values.size) and bool(np.all(np.isfinite(self.values)))

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary (NaN -> None). Per-subcarrier arrays are included."""
        return {
            "link_id": self.link_id,
            "t_start_ns": self.t_start_ns,
            "t_end_ns": self.t_end_ns,
            "t_unix_start_ns": self.t_unix_start_ns,
            "t_unix_end_ns": self.t_unix_end_ns,
            "layout_id": self.layout_id,
            "names": list(self.names),
            "values": _nan_to_none(self.values),
            "n_frames": self.n_frames,
            "n_valid_subcarriers": self.n_valid_subcarriers,
            "n_outliers_replaced": self.n_outliers_replaced,
            "k": [int(v) for v in self.k],
            "profile": _nan_to_none(self.profile),
            "per_subcarrier": {n: _nan_to_none(v) for n, v in self.per_subcarrier.items()},
        }


def _nanmedian_last_axis(w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """NaN-ignoring median along the last axis, plus the finite count.

    Sort-based. ``np.nanmedian`` over many short rows goes through a
    masked-array path that is an order of magnitude slower.
    """
    s = np.sort(w, axis=-1)  # NaNs sort to the end
    n = np.sum(np.isfinite(w), axis=-1)
    last = w.shape[-1] - 1
    lo = np.clip((n - 1) // 2, 0, last)[..., None]
    hi = np.clip(n // 2, 0, last)[..., None]
    med = 0.5 * (np.take_along_axis(s, lo, axis=-1) + np.take_along_axis(s, hi, axis=-1))[..., 0]
    med[n == 0] = np.nan
    return med, n


def hampel_filter(x: np.ndarray, half_window: int, n_sigmas: float) -> tuple[np.ndarray, int]:
    """Replace outliers along axis 0 by the local median. NaNs stay NaN.

    Returns ``(filtered_copy, n_replaced)``. A point is an outlier when it
    differs from the median of its ``2*half_window+1`` neighbourhood by more
    than ``n_sigmas`` robust standard deviations. The robust deviation is
    ``1.4826 * MAD``, floored at ``HAMPEL_REL_SCALE_FLOOR * |median|``.
    """
    x = np.asarray(x, dtype=np.float64)
    if half_window <= 0 or x.ndim != 2 or x.shape[0] < 3:
        return x.copy(), 0
    pad = np.full((half_window, x.shape[1]), np.nan)
    xp = np.vstack([pad, x, pad])
    win = sliding_window_view(xp, 2 * half_window + 1, axis=0)  # (n, S, w)
    med, n_ok = _nanmedian_last_axis(win)
    mad, _ = _nanmedian_last_axis(np.abs(win - med[..., None]))
    scale = np.maximum(_MAD_TO_SIGMA * mad, HAMPEL_REL_SCALE_FLOOR * np.abs(med))
    # At least 3 finite neighbours are needed to call anything an outlier.
    outlier = np.isfinite(x) & np.isfinite(med) & (n_ok >= 3) & (np.abs(x - med) > n_sigmas * scale)
    y = x.copy()
    y[outlier] = med[outlier]
    return y, int(outlier.sum())


def _per_subcarrier_features(t: np.ndarray, a: np.ndarray, valid: np.ndarray, max_gap_s: float
                             ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n_pos = a.shape[1]
    cv = np.full(n_pos, np.nan)
    tdiff = np.full(n_pos, np.nan)
    profile = np.full(n_pos, np.nan)
    if a.shape[0] == 0:
        return cv, tdiff, profile
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean = np.nanmean(a, axis=0)
        std = np.nanstd(a, axis=0)
    ok_mean = valid & np.isfinite(mean) & (mean > 0)
    cv[ok_mean] = std[ok_mean] / mean[ok_mean]
    profile[valid & np.isfinite(mean)] = mean[valid & np.isfinite(mean)]

    if a.shape[0] >= 2:
        d = np.abs(np.diff(a, axis=0))
        pair_ok = (np.diff(t) <= max_gap_s)[:, None] & np.isfinite(d)
        n_pairs = pair_ok.sum(axis=0)
        sum_d = np.where(pair_ok, d, 0.0).sum(axis=0)
        ok = ok_mean & (n_pairs > 0)
        tdiff[ok] = (sum_d[ok] / n_pairs[ok]) / mean[ok]
    return cv, tdiff, profile


def extract_features(window: Window, cfg: ProcessingConfig) -> FeatureVector:
    """Compute the window's feature vector (see the module docstring)."""
    ensure_phase_disabled(cfg)
    a = np.asarray(window.amp, dtype=np.float64)
    n_replaced = 0
    if cfg.hampel_window > 0:
        a, n_replaced = hampel_filter(a, cfg.hampel_window, cfg.hampel_sigmas)
    valid = np.asarray(window.valid, dtype=bool)
    cv, tdiff, profile = _per_subcarrier_features(np.asarray(window.t, dtype=np.float64), a, valid, cfg.max_gap_s)

    usable = valid & np.isfinite(cv) & np.isfinite(tdiff)
    if usable.any():
        values = np.array([np.median(cv[usable]), np.median(tdiff[usable])], dtype=np.float64)
    else:
        values = np.full(len(FEATURE_NAMES), np.nan)

    per_sc = {"amp_cv_median": cv, "amp_tdiff_median": tdiff}
    k = np.asarray(window.k, dtype=np.int32).copy()
    for arr in (values, cv, tdiff, profile, k, usable):
        arr.setflags(write=False)
    return FeatureVector(
        link_id=window.link_id,
        t_end_ns=window.t_end_ns,
        names=FEATURE_NAMES,
        values=values,
        per_subcarrier=per_sc,
        profile=profile,
        k=k,
        t_start_ns=window.t_start_ns,
        valid=usable,
        n_frames=window.n_frames,
        n_valid_subcarriers=int(usable.sum()),
        layout_id=window.layout_id,
        t_unix_start_ns=window.t_unix_start_ns,
        t_unix_end_ns=window.t_unix_end_ns,
        n_outliers_replaced=n_replaced,
    )
