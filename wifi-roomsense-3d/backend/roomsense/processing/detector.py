"""Baseline-relative motion heuristic with hysteresis (capability B).

``activity_score`` is the largest robust z-score over the window features:
``(feature - baseline_median) / baseline_scale``. It is a **unitless
heuristic**: how unusual the amplitude fluctuation is compared with the quiet
baseline. It is **not a probability** of presence or motion, and
``calibrated_probability`` is always ``None``. The uncertainty field reports
how much the per-subcarrier z-scores spread (``subcarrier_iqr``). It is not a
confidence interval on presence.

Decision rules, in priority order:

1. calibrating -> ``CALIBRATING``
2. stale data (newest frame older than ``stale_after_s``) or no frames ->
   ``SENSOR_OFFLINE``. A latched ``MOTION_DETECTED`` is cleared once the data
   is older than ``clear_stale_after_s``.
3. no baseline -> ``UNKNOWN`` (``NO_BASELINE``). An invalidated baseline gives
   ``UNKNOWN`` (``CALIBRATION_INVALID``).
4. baseline layout / hardware signature / config version / source mismatch ->
   ``UNKNOWN`` (``CALIBRATION_INVALID``)
5. quality below ``min_quality_for_decision`` or no usable features ->
   ``UNKNOWN`` (``LOW_QUALITY``)
6. drift suspected -> ``UNKNOWN`` (``STATIC_CHANNEL_CHANGED``)
7. hysteresis: enter ``MOTION_DETECTED`` when score >= ``enter_threshold``.
   Leave it only after the score has been below ``exit_threshold`` for
   ``min_quiet_hold_s`` without a break, and only after at least
   ``min_motion_hold_s`` in motion.

Low quality or stale data therefore never turns into ``NO_MOTION_DETECTED``.
A motion latch survives a short run of undecidable windows. After
``clear_stale_after_s`` with no decision it is dropped.

The detector never writes to its baseline. Activity is never absorbed into the
empty-room reference. Recalibration is always an explicit user action.

Hold timers use the ``now_ns`` passed to :meth:`MotionDetector.update`. The
engine passes the window end time, which is the measurement timeline.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..config import DetectionConfig
from ..schemas import (
    ActivityResult,
    ActivityState,
    Provenance,
    QualityLevel,
    QualityReport,
    ScoreUncertainty,
    SourceMode,
)
from .baseline import Baseline
from .drift import DriftMonitor, profile_correlation
from .features import FeatureVector
from .quality import quality_at_least

__all__ = [
    "SCORE_NOTE",
    "robust_z",
    "MotionDetector",
]

SCORE_NOTE = (
    "SCORE_IS_HEURISTIC: activity_score is a unitless robust deviation from the quiet baseline, "
    "not a probability of presence or motion"
)


def robust_z(fv: FeatureVector, baseline: Baseline) -> dict[str, float]:
    """Per-feature robust z-score of ``fv`` against ``baseline``."""
    out: dict[str, float] = {}
    for name in fv.names:
        out[name] = (fv.value(name) - baseline.feature_median[name]) / baseline.feature_scale[name]
    return out


def _subcarrier_uncertainty(fv: FeatureVector, baseline: Baseline) -> ScoreUncertainty | None:
    """IQR (25th..75th percentile) of per-subcarrier max-over-features z."""
    if not baseline.per_subcarrier_scale or fv.k.shape != baseline.k.shape:
        return None
    zs = []
    for name in fv.names:
        cur = fv.per_subcarrier.get(name)
        med = baseline.per_subcarrier_median.get(name)
        scale = baseline.per_subcarrier_scale.get(name)
        if cur is None or med is None or scale is None:
            return None
        zs.append((np.asarray(cur) - med) / scale)
    z = np.vstack(zs)
    ok = np.all(np.isfinite(z), axis=0) & np.asarray(fv.valid, dtype=bool)
    if not ok.any():
        return None
    zmax = np.max(z[:, ok], axis=0)
    lo, hi = np.percentile(zmax, [25.0, 75.0])
    return ScoreUncertainty(
        method="subcarrier_iqr", low=float(lo), high=float(hi), n_subcarriers=int(ok.sum()), n_frames=fv.n_frames
    )


class MotionDetector:
    """Per-link state machine. Not thread-safe by itself; the engine holds a
    lock around every call."""

    def __init__(self, link_id: str, cfg: DetectionConfig, config_version: str, *, stale_after_s: float = 2.0) -> None:
        self.link_id = link_id
        self.cfg = cfg
        self.config_version = config_version
        self.stale_after_s = float(stale_after_s)
        self._baseline: Baseline | None = None
        self._invalid_reason: str | None = None
        self._hardware_signature: str | None = None
        self._signature_complete = True
        self._calibrating = False
        self._drift = DriftMonitor(cfg.drift_min_profile_correlation, cfg.drift_hold_s)
        self.reset_state()

    # ------------------------------------------------------------------ control
    def reset_state(self) -> None:
        """Forget hysteresis and drift state (not the baseline)."""
        self._motion = False
        self._motion_since_ns: int | None = None
        self._quiet_since_ns: int | None = None
        self._last_decision_ns: int | None = None
        self._drift.reset()

    @property
    def baseline(self) -> Baseline | None:
        return self._baseline

    @property
    def invalid_reason(self) -> str | None:
        return self._invalid_reason

    @property
    def calibrating(self) -> bool:
        return self._calibrating

    @property
    def motion_latched(self) -> bool:
        return self._motion

    def set_baseline(self, baseline: Baseline | None) -> None:
        if baseline is not None and baseline.link_id != self.link_id:
            raise ValueError(f"baseline for {baseline.link_id!r} given to detector of {self.link_id!r}")
        self._baseline = baseline
        self._invalid_reason = None
        self.reset_state()

    def invalidate_baseline(self, reason: str) -> None:
        """Drop the baseline and remember why (reported as CALIBRATION_INVALID).

        A detector without a baseline keeps reporting NO_BASELINE: there is
        nothing to invalidate.
        """
        if self._baseline is None:
            return
        self._invalid_reason = reason
        self._baseline = None
        self.reset_state()

    def set_hardware_signature(self, signature: str | None, *, complete: bool = True) -> None:
        """Current identity hash of this link (from the engine).

        ``complete=False`` means some identity fields have not been reported
        yet (e.g. before the first firmware hello), so a mismatch cannot yet
        be called a change.
        """
        self._hardware_signature = signature
        self._signature_complete = bool(complete)

    def set_calibrating(self, on: bool) -> None:
        self._calibrating = bool(on)
        if on:
            self.reset_state()

    def drift_status(self) -> dict[str, Any]:
        return {"link_id": self.link_id, "has_baseline": self._baseline is not None, **self._drift.status()}

    # ------------------------------------------------------------------ helpers
    def _result(
        self,
        state: ActivityState,
        quality: QualityReport,
        provenance: Provenance,
        reasons: list[str],
        score: float | None = None,
        uncertainty: ScoreUncertainty | None = None,
    ) -> ActivityResult:
        return ActivityResult(
            link_id=self.link_id,
            state=state,
            activity_score=score,
            enter_threshold=self.cfg.enter_threshold,
            exit_threshold=self.cfg.exit_threshold,
            calibrated_probability=None,
            uncertainty=uncertainty,
            quality=quality,
            reasons=reasons,
            provenance=provenance,
        )

    def _mismatch(self, fv: FeatureVector | None, provenance: Provenance) -> str | None:
        b = self._baseline
        assert b is not None
        if b.config_version != self.config_version:
            return f"CONFIG_CHANGED: baseline config {b.config_version} != current {self.config_version}"
        expected_sig = b.link_signature if b.link_signature is not None else b.hardware_signature
        if self._hardware_signature is not None and expected_sig != self._hardware_signature:
            if not self._signature_complete:
                return ("HARDWARE_SIGNATURE_UNVERIFIED: link identity is not fully reported yet, so it "
                        "cannot be matched to the calibrated hardware")
            return "HARDWARE_SIGNATURE_CHANGED: link identity differs from the one calibrated"
        if b.source_mode is not None:
            simulated_now = SourceMode(provenance.source_mode) == SourceMode.SIMULATION
            if (b.source_mode == SourceMode.SIMULATION.value) != simulated_now:
                return (f"SOURCE_MODE_MISMATCH: baseline recorded in {b.source_mode}, data is "
                        f"{SourceMode(provenance.source_mode).value}; simulated and measured data never mix")
        if fv is not None:
            if fv.layout_id != b.layout_id:
                return f"LAYOUT_CHANGED: baseline layout {b.layout_id} != current {fv.layout_id}"
            if fv.k.shape != b.k.shape or not np.array_equal(fv.k, b.k):
                return "LAYOUT_CHANGED: subcarrier indices differ from the baseline"
            if set(fv.names) != set(b.feature_median):
                return "FEATURE_SET_CHANGED: features differ from the baseline"
        return None

    def _maybe_expire_latch(self, now_ns: int, reasons: list[str]) -> None:
        """Drop a motion latch after clear_stale_after_s without any decision."""
        if not self._motion:
            return
        ref = self._last_decision_ns
        if ref is None or (now_ns - ref) / 1e9 > self.cfg.clear_stale_after_s:
            self._motion = False
            self._motion_since_ns = None
            reasons.append(
                f"MOTION_CLEARED: no decidable data for more than {self.cfg.clear_stale_after_s:g} s"
            )

    def _offline(self, now_ns: int, age_s: float | None, provenance: Provenance, quality: QualityReport,
                 reasons: list[str]) -> ActivityResult:
        if self._motion and (age_s is None or age_s > self.cfg.clear_stale_after_s):
            self._motion = False
            self._motion_since_ns = None
            reasons.append(
                f"MOTION_CLEARED: previous MOTION_DETECTED cleared; data older than {self.cfg.clear_stale_after_s:g} s"
            )
        # Quiet evidence cannot accumulate while no data arrives.
        self._quiet_since_ns = None
        if self._calibrating:
            return self._result(ActivityState.CALIBRATING, quality, provenance,
                                ["CALIBRATING: quiet-baseline recording in progress"] + reasons)
        return self._result(ActivityState.SENSOR_OFFLINE, quality, provenance, reasons)

    # ------------------------------------------------------------------ main API
    def update(
        self,
        fv: FeatureVector | None,
        quality: QualityReport,
        provenance: Provenance,
        now_ns: int,
    ) -> ActivityResult:
        """Decide the state for one new window (see the module docstring)."""
        if self._calibrating:
            return self._result(
                ActivityState.CALIBRATING, quality, provenance,
                ["CALIBRATING: quiet-baseline recording in progress; keep the room empty and still"],
            )

        age = provenance.measurement_age_s
        if age is not None and age > self.stale_after_s:
            return self._offline(now_ns, age, provenance, quality,
                                 [f"STALE_DATA: newest frame is {age:.1f} s old (limit {self.stale_after_s:g} s)"])
        if fv is None and quality.frames_in_window == 0 and quality.rejected_frames == 0:
            return self._offline(now_ns, None, provenance, quality, ["NO_FRAMES: no frames in the window"])

        if self._baseline is None:
            reasons: list[str] = []
            if self._invalid_reason is not None:
                reasons.append("CALIBRATION_INVALID: baseline invalidated; record a new quiet baseline")
                reasons.append(self._invalid_reason)
            else:
                reasons.append("NO_BASELINE: record a quiet baseline with the room empty")
            self._maybe_expire_latch(now_ns, reasons)
            return self._result(ActivityState.UNKNOWN, quality, provenance, reasons)

        mismatch = self._mismatch(fv, provenance)
        if mismatch is not None:
            reasons = ["CALIBRATION_INVALID: baseline does not match the current link", mismatch]
            self._maybe_expire_latch(now_ns, reasons)
            self._quiet_since_ns = None
            return self._result(ActivityState.UNKNOWN, quality, provenance, reasons)

        min_q = QualityLevel(self.cfg.min_quality_for_decision)
        if fv is None or not quality_at_least(quality.level, min_q) or not fv.is_finite():
            reasons = [f"LOW_QUALITY: quality {QualityLevel(quality.level).value} is below "
                       f"{min_q.value} or features are unavailable; no decision"]
            reasons += [f"QUALITY_FLAG: {f}" for f in quality.flags]
            if fv is not None and not fv.is_finite():
                reasons.append("NO_USABLE_SUBCARRIERS: features are not finite")
            self._maybe_expire_latch(now_ns, reasons)
            self._quiet_since_ns = None
            return self._result(ActivityState.UNKNOWN, quality, provenance, reasons)

        b = self._baseline
        z = robust_z(fv, b)
        score = float(max(z.values()))
        uncertainty = _subcarrier_uncertainty(fv, b)

        corr = profile_correlation(fv.profile, b.profile)
        if self._drift.update(corr, now_ns):
            corr_txt = "undefined" if not np.isfinite(corr) else f"{corr:.2f}"
            reasons = [
                "STATIC_CHANNEL_CHANGED: the static amplitude profile no longer matches the baseline "
                f"(correlation {corr_txt} < {self.cfg.drift_min_profile_correlation:g} for >= "
                f"{self.cfg.drift_hold_s:g} s). Possible sensor movement, environment change or a stationary "
                "person; recalibrate only if the room is empty",
            ]
            if self._motion:
                reasons.append("MOTION_CLEARED: calibration is suspect, previous motion state dropped")
            self._motion = False
            self._motion_since_ns = None
            self._quiet_since_ns = None
            self._last_decision_ns = now_ns
            return self._result(ActivityState.UNKNOWN, quality, provenance, reasons)

        self._last_decision_ns = now_ns
        cfg = self.cfg
        reasons = [SCORE_NOTE]
        if not self._motion:
            if score >= cfg.enter_threshold:
                self._motion = True
                self._motion_since_ns = now_ns
                self._quiet_since_ns = None
                reasons.append(f"ENTER: score {score:.2f} >= enter threshold {cfg.enter_threshold:g}")
                state = ActivityState.MOTION_DETECTED
            else:
                state = ActivityState.NO_MOTION_DETECTED
                reasons.append(f"BELOW_ENTER: score {score:.2f} < enter threshold {cfg.enter_threshold:g}")
        else:
            if score < cfg.exit_threshold:
                if self._quiet_since_ns is None:
                    self._quiet_since_ns = now_ns
                quiet_s = (now_ns - self._quiet_since_ns) / 1e9
                motion_s = (now_ns - (self._motion_since_ns or now_ns)) / 1e9
                if quiet_s >= cfg.min_quiet_hold_s and motion_s >= cfg.min_motion_hold_s:
                    self._motion = False
                    self._motion_since_ns = None
                    self._quiet_since_ns = None
                    state = ActivityState.NO_MOTION_DETECTED
                    reasons.append(f"EXIT: score below {cfg.exit_threshold:g} for {quiet_s:.1f} s")
                else:
                    state = ActivityState.MOTION_DETECTED
                    reasons.append(
                        f"HOLD: score below exit threshold for {quiet_s:.1f} s of {cfg.min_quiet_hold_s:g} s; "
                        f"in motion for {motion_s:.1f} s (min {cfg.min_motion_hold_s:g} s)"
                    )
            else:
                self._quiet_since_ns = None
                state = ActivityState.MOTION_DETECTED
                reasons.append(f"ABOVE_EXIT: score {score:.2f} >= exit threshold {cfg.exit_threshold:g}")
        return self._result(state, quality, provenance, reasons, score=score, uncertainty=uncertainty)

    def tick(
        self,
        now_ns: int,
        last_frame_age_s: float | None,
        provenance: Provenance,
        *,
        offline_reason: str | None = None,
    ) -> ActivityResult | None:
        """Staleness check when no fresh window exists; ``None`` if not stale.

        ``offline_reason`` (e.g. ``DISCONNECTED``) forces ``SENSOR_OFFLINE``
        regardless of the data age.
        """
        stale = (
            offline_reason is not None
            or last_frame_age_s is None
            or last_frame_age_s > self.stale_after_s
        )
        if not stale:
            return None
        reasons: list[str] = []
        if offline_reason is not None:
            reasons.append(offline_reason)
        if last_frame_age_s is None:
            reasons.append("NO_FRAMES: no usable frames received on this link")
        elif last_frame_age_s > self.stale_after_s:
            reasons.append(
                f"STALE_DATA: newest frame is {last_frame_age_s:.1f} s old (limit {self.stale_after_s:g} s)"
            )
        quality = QualityReport(level=QualityLevel.UNAVAILABLE, flags=["STALE_OR_OFFLINE"])
        return self._offline(now_ns, last_frame_age_s, provenance, quality, reasons)
