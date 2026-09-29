"""User-recorded quiet ("empty room") baseline.

The user starts a recording after confirming that the monitored room is empty
and that nobody is moving near the sensors. Windows are collected while the
recording runs. At the end the baseline is accepted or rejected with explicit
reasons.

Acceptance rules (all must hold):

* Only windows whose quality is at least ``DEGRADED`` with finite features are
  used, and at most 50 % of the offered windows may have been skipped for low
  quality (``LOW_QUALITY_DURING_CALIBRATION``).
* At least ``baseline_min_windows`` usable windows (``INSUFFICIENT_WINDOWS``),
  covering at least ``baseline_min_duration_s`` from the first window start to
  the last window end (``INSUFFICIENT_DURATION``).
* A single CSI layout and link identity throughout (``LAYOUT_CHANGED`` /
  ``HARDWARE_SIGNATURE_CHANGED``).
* Stability (``BASELINE_UNSTABLE``), checked against the provisional baseline
  built from the same windows:
  - more than 10 % of the windows deviate from it by at least
    ``enter_threshold`` robust z (either sign). This is stricter than the
    one-sided detector rule. Motion during calibration makes windows exceed
    the threshold, and a bimodal recording (quiet and moving halves) makes
    the quiet half deviate negatively; or
  - more than 10 % of the windows have a profile correlation with the median
    profile below ``drift_min_profile_correlation``; or
  - the mean profiles of the first and second halves correlate below
    ``drift_min_profile_correlation`` (the static channel changed during the
    recording, e.g. someone moved furniture or a sensor).

Robust statistics per feature: ``median`` and ``scale = 1.4826 * MAD``. The
scale is floored at ``SCALE_REL_FLOOR * |median|`` (and ``SCALE_ABS_FLOOR``),
so a perfectly flat synthetic baseline yields large but finite z-scores.

Limitation: if motion lasts for the *whole* recording, the robust statistics
describe motion and none of these rules can notice it. The walk test exists to
catch that case (a baseline that absorbed motion gives weak walk-test scores).
"""

from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config import DetectionConfig
from ..schemas import QualityLevel, QualityReport
from .drift import profile_correlation
from .features import FeatureVector
from .quality import quality_at_least

__all__ = [
    "BASELINE_FORMAT",
    "SCALE_REL_FLOOR",
    "SCALE_ABS_FLOOR",
    "MAX_UNSTABLE_FRACTION",
    "MAX_SKIPPED_FRACTION",
    "MAX_BASELINE_WINDOWS",
    "Baseline",
    "BaselineRecorder",
    "robust_center_scale",
]

BASELINE_FORMAT = "roomsense-baseline-v1"
SCALE_REL_FLOOR = 0.05
SCALE_ABS_FLOOR = 1e-6
MAX_UNSTABLE_FRACTION = 0.10
MAX_SKIPPED_FRACTION = 0.50
# Memory bound (~2.5 kB per stored window): one hour at the default 0.5 s hop
# is far more than any sensible quiet recording. Windows beyond the cap are
# counted in the summary but not stored.
MAX_BASELINE_WINDOWS = 7_200
_MAD_TO_SIGMA = 1.4826


def robust_center_scale(values: np.ndarray, axis: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """``(median, floored 1.4826*MAD)`` along ``axis`` ignoring NaN."""
    v = np.asarray(values, dtype=np.float64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(v, axis=axis)
        mad = np.nanmedian(np.abs(v - np.expand_dims(med, axis)), axis=axis)
    scale = np.maximum(np.maximum(_MAD_TO_SIGMA * mad, SCALE_REL_FLOOR * np.abs(med)), SCALE_ABS_FLOOR)
    return med, scale


def _ro(a: np.ndarray) -> np.ndarray:
    a = np.array(a, dtype=np.float64 if a.dtype.kind == "f" else a.dtype, copy=True)
    a.setflags(write=False)
    return a


def _list(a: np.ndarray) -> list[float | None]:
    return [float(v) if np.isfinite(v) else None for v in np.asarray(a, dtype=np.float64)]


def _arr(v: Any, n: int, name: str) -> np.ndarray:
    if not isinstance(v, list) or len(v) != n:
        raise ValueError(f"baseline field {name!r} must be a list of length {n}")
    out = np.array([np.nan if x is None else float(x) for x in v], dtype=np.float64)
    if np.any(np.isinf(out)):
        raise ValueError(f"baseline field {name!r} contains infinite values")
    return out


@dataclass(frozen=True)
class Baseline:
    """A frozen quiet baseline for one link. Arrays are read-only: nothing
    downstream may adapt it silently.

    ``hardware_signature`` is the engine-wide signature supplied at
    finalisation. ``link_signature``, when present, is the identity hash of
    this link alone and is what detectors compare against.
    """

    link_id: str
    calibration_id: str
    feature_median: dict[str, float]
    feature_scale: dict[str, float]
    per_subcarrier_median: dict[str, np.ndarray]
    profile: np.ndarray
    k: np.ndarray
    n_windows: int
    duration_s: float
    layout_id: str
    hardware_signature: str
    config_version: str
    per_subcarrier_scale: dict[str, np.ndarray] = field(default_factory=dict)
    link_signature: str | None = None
    source_mode: str | None = None
    created_at_unix_ns: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(self.feature_median)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation (NaN -> None)."""
        return {
            "format": BASELINE_FORMAT,
            "link_id": self.link_id,
            "calibration_id": self.calibration_id,
            "feature_median": {k: float(v) for k, v in self.feature_median.items()},
            "feature_scale": {k: float(v) for k, v in self.feature_scale.items()},
            "per_subcarrier_median": {k: _list(v) for k, v in self.per_subcarrier_median.items()},
            "per_subcarrier_scale": {k: _list(v) for k, v in self.per_subcarrier_scale.items()},
            "profile": _list(self.profile),
            "k": [int(v) for v in self.k],
            "n_windows": int(self.n_windows),
            "duration_s": float(self.duration_s),
            "layout_id": self.layout_id,
            "hardware_signature": self.hardware_signature,
            "link_signature": self.link_signature,
            "config_version": self.config_version,
            "source_mode": self.source_mode,
            "created_at_unix_ns": self.created_at_unix_ns,
            "summary": dict(self.summary),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Baseline":
        """Strict inverse of :meth:`to_dict`; raises ``ValueError`` on bad input."""
        if not isinstance(d, dict) or d.get("format") != BASELINE_FORMAT:
            raise ValueError(f"not a {BASELINE_FORMAT} object")
        try:
            k = np.asarray([int(v) for v in d["k"]], dtype=np.int32)
            n = int(k.size)
            fmed = {str(a): float(b) for a, b in d["feature_median"].items()}
            fscale = {str(a): float(b) for a, b in d["feature_scale"].items()}
            if set(fmed) != set(fscale) or not fmed:
                raise ValueError("feature_median and feature_scale must name the same features")
            if not all(math.isfinite(v) for v in fmed.values()) or not all(
                math.isfinite(v) and v > 0 for v in fscale.values()
            ):
                raise ValueError("feature statistics must be finite and scales positive")
            psm = {str(a): _ro(_arr(b, n, f"per_subcarrier_median.{a}")) for a, b in d["per_subcarrier_median"].items()}
            pss = {str(a): _ro(_arr(b, n, f"per_subcarrier_scale.{a}"))
                   for a, b in (d.get("per_subcarrier_scale") or {}).items()}
            profile = _ro(_arr(d["profile"], n, "profile"))
            k.setflags(write=False)
            created = d.get("created_at_unix_ns")
            return cls(
                link_id=str(d["link_id"]),
                calibration_id=str(d["calibration_id"]),
                feature_median=fmed,
                feature_scale=fscale,
                per_subcarrier_median=psm,
                profile=profile,
                k=k,
                n_windows=int(d["n_windows"]),
                duration_s=float(d["duration_s"]),
                layout_id=str(d["layout_id"]),
                hardware_signature=str(d["hardware_signature"]),
                config_version=str(d["config_version"]),
                per_subcarrier_scale=pss,
                link_signature=None if d.get("link_signature") is None else str(d["link_signature"]),
                source_mode=None if d.get("source_mode") is None else str(d["source_mode"]),
                created_at_unix_ns=None if created is None else int(created),
                summary=dict(d.get("summary") or {}),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"malformed baseline: {exc}") from exc


class BaselineRecorder:
    """Collects quiet-baseline windows for one link, then accepts or rejects."""

    def __init__(self, link_id: str, cfg: DetectionConfig) -> None:
        self.link_id = link_id
        self.cfg = cfg
        self._fvs: list[FeatureVector] = []
        self._skipped_quality = 0
        self._skipped_nonfinite = 0
        self._over_cap = 0
        self._layouts: set[str] = set()
        self._identity_changes: list[str] = []
        self._source_mode: str | None = None

    @property
    def n_windows(self) -> int:
        return len(self._fvs)

    @property
    def skipped_windows(self) -> int:
        return self._skipped_quality + self._skipped_nonfinite

    def progress(self) -> dict[str, Any]:
        """Live progress for the UI (no acceptance decision is implied)."""
        duration = (self._fvs[-1].t_end_ns - self._fvs[0].t_start_ns) / 1e9 if self._fvs else 0.0
        return {
            "windows_used": len(self._fvs),
            "windows_skipped": self.skipped_windows,
            "covered_s": duration,
            "min_windows": self.cfg.baseline_min_windows,
            "min_duration_s": self.cfg.baseline_min_duration_s,
        }

    def note_identity_change(self, reason: str) -> None:
        """Called by the engine when the link's identity changes mid-recording."""
        self._identity_changes.append(reason)

    def set_source_mode(self, mode: str | None) -> None:
        self._source_mode = mode

    def add(self, fv: FeatureVector, quality: QualityReport) -> None:
        if not quality_at_least(quality.level, QualityLevel.DEGRADED):
            self._skipped_quality += 1
            return
        if not fv.is_finite():
            self._skipped_nonfinite += 1
            return
        if len(self._fvs) >= MAX_BASELINE_WINDOWS:
            self._over_cap += 1
            return
        self._layouts.add(fv.layout_id)
        self._fvs.append(fv)

    def finalize(
        self,
        *,
        calibration_id: str,
        hardware_signature: str,
        config_version: str,
        link_signature: str | None = None,
    ) -> tuple[Baseline | None, list[str]]:
        """Return ``(Baseline, [])`` or ``(None, reasons)``."""
        cfg = self.cfg
        n = len(self._fvs)
        offered = n + self.skipped_windows
        reasons: list[str] = []
        if n == 0:
            return None, [f"NO_USABLE_WINDOWS: {offered} window(s) offered, none of usable quality"]
        if len(self._layouts) > 1:
            reasons.append(f"LAYOUT_CHANGED: CSI layout changed during calibration ({sorted(self._layouts)})")
        for r in self._identity_changes:
            reasons.append(r if r.startswith("HARDWARE_SIGNATURE_CHANGED") else f"HARDWARE_SIGNATURE_CHANGED: {r}")
        if offered and self.skipped_windows / offered > MAX_SKIPPED_FRACTION:
            reasons.append(
                f"LOW_QUALITY_DURING_CALIBRATION: {self.skipped_windows} of {offered} windows skipped "
                f"(limit {MAX_SKIPPED_FRACTION:.0%})"
            )
        duration_s = (self._fvs[-1].t_end_ns - self._fvs[0].t_start_ns) / 1e9
        if n < cfg.baseline_min_windows:
            reasons.append(f"INSUFFICIENT_WINDOWS: {n} usable windows; need {cfg.baseline_min_windows}")
        if duration_s < cfg.baseline_min_duration_s:
            reasons.append(f"INSUFFICIENT_DURATION: {duration_s:.1f} s; need {cfg.baseline_min_duration_s:g} s")

        names = self._fvs[0].names
        if any(fv.names != names for fv in self._fvs):
            reasons.append("FEATURE_SET_CHANGED: feature names changed during calibration")
            return None, reasons
        vals = np.vstack([fv.values for fv in self._fvs])  # (n, F)
        med, scale = robust_center_scale(vals, axis=0)

        # Stability 1: robust z of each window against the provisional baseline.
        z = (vals - med) / scale
        zmax_abs = np.max(np.abs(z), axis=1)
        unstable_frac = float(np.mean(zmax_abs >= cfg.enter_threshold))
        if unstable_frac > MAX_UNSTABLE_FRACTION:
            reasons.append(
                f"BASELINE_UNSTABLE: {unstable_frac:.0%} of windows deviate by >= {cfg.enter_threshold:g} "
                f"(robust z) from the provisional baseline (limit {MAX_UNSTABLE_FRACTION:.0%}); "
                "movement during calibration is likely"
            )

        # Stability 2: static profile consistency.
        k0 = self._fvs[0].k
        same_k = all(fv.k.shape == k0.shape and np.array_equal(fv.k, k0) for fv in self._fvs)
        corr_low_frac: float | None = None
        half_corr: float | None = None
        profiles = np.vstack([fv.profile for fv in self._fvs]) if same_k else None
        if profiles is None:
            reasons.append("LAYOUT_CHANGED: subcarrier index sets differ between windows")
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                med_profile = np.nanmedian(profiles, axis=0)
            corrs = np.array([profile_correlation(p, med_profile) for p in profiles])
            low = ~np.isfinite(corrs) | (corrs < cfg.drift_min_profile_correlation)
            corr_low_frac = float(np.mean(low))
            if corr_low_frac > MAX_UNSTABLE_FRACTION:
                reasons.append(
                    f"BASELINE_UNSTABLE: {corr_low_frac:.0%} of windows have a static profile correlation "
                    f"below {cfg.drift_min_profile_correlation:g} (limit {MAX_UNSTABLE_FRACTION:.0%})"
                )
            if n >= 4:
                h = n // 2
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    p1, p2 = np.nanmean(profiles[:h], axis=0), np.nanmean(profiles[h:], axis=0)
                half_corr = profile_correlation(p1, p2)
                if not np.isfinite(half_corr) or half_corr < cfg.drift_min_profile_correlation:
                    reasons.append(
                        "BASELINE_UNSTABLE: static profile changed between the first and second half of the "
                        f"recording (correlation {half_corr:.2f})"
                    )

        if reasons:
            return None, reasons

        assert profiles is not None
        per_sc = {name: np.vstack([fv.per_subcarrier[name] for fv in self._fvs]) for name in names}
        psm: dict[str, np.ndarray] = {}
        pss: dict[str, np.ndarray] = {}
        for name, mat in per_sc.items():
            m, s = robust_center_scale(mat, axis=0)
            psm[name], pss[name] = _ro(m), _ro(s)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mean_profile = np.nanmean(profiles, axis=0)
        k = np.asarray(k0, dtype=np.int32).copy()
        k.setflags(write=False)
        summary = {
            "windows_used": n,
            "windows_skipped_low_quality": self._skipped_quality,
            "windows_skipped_nonfinite": self._skipped_nonfinite,
            "windows_over_cap": self._over_cap,
            "unstable_window_fraction": unstable_frac,
            "low_profile_correlation_fraction": corr_low_frac,
            "half_profile_correlation": half_corr,
            "rules": "median/1.4826*MAD per feature; <=10% windows |z|>=enter; profile correlation checks",
        }
        return (
            Baseline(
                link_id=self.link_id,
                calibration_id=calibration_id,
                feature_median={nm: float(med[i]) for i, nm in enumerate(names)},
                feature_scale={nm: float(scale[i]) for i, nm in enumerate(names)},
                per_subcarrier_median=psm,
                profile=_ro(mean_profile),
                k=k,
                n_windows=n,
                duration_s=duration_s,
                layout_id=self._fvs[0].layout_id,
                hardware_signature=hardware_signature,
                config_version=config_version,
                per_subcarrier_scale=pss,
                link_signature=link_signature,
                source_mode=self._source_mode,
                created_at_unix_ns=time.time_ns(),
                summary=summary,
            ),
            [],
        )
