"""Runtime zone predictor (capability C): ``DISABLED`` / ``ABSTAIN`` / ``ESTIMATE``.

``DISABLED`` (the capability is off) when any of these holds:

* no model has passed the predefined criteria (``NO_ENABLED_MODEL``), or the
  pinned model's report does not enable it (``MODEL_NOT_ENABLED``);
* the criteria file changed since evaluation (``CRITERIA_CHANGED``) or cannot
  be read (``CRITERIA_UNAVAILABLE``);
* the hardware signature, room geometry or processing config differ from
  what the model was bound to (``HARDWARE_SIGNATURE_MISMATCH``,
  ``ROOM_CHANGED``, ``CONFIG_CHANGED``);
* the stored pipeline does not accept the bound input layout
  (``MODEL_INPUT_SHAPE_MISMATCH``) or its file fails verification
  (``MODEL_FILE_INVALID``);
* the source is a simulation (``SIMULATED_SOURCE``).

``ABSTAIN`` (enabled, but no estimate for this window) when a required link
is missing, stale, not aligned, of too low quality, or in state ``UNKNOWN`` /
``SENSOR_OFFLINE`` / ``CALIBRATING``; when the top model score is below the
frozen threshold; or when the top class is ``EMPTY`` or
``OUTSIDE_TARGET_ROOM`` (``NO_TARGET_ZONE_PREDICTED``).

``ESTIMATE`` otherwise: a zone id and label, named model scores (not
probabilities), and the zone polygon's centroid as a **display anchor**
(x/y only). A position is never computed from receiver coordinates or
signal strength.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ...acquisition.alignment import align_link_windows
from ...config import AppConfig
from ...processing.features import FeatureVector
from ...schemas import (
    ActivityResult,
    ActivityState,
    Provenance,
    QualityLevel,
    RoomGeometry,
    SourceMode,
    ZonePrediction,
    ZoneState,
)
from .criteria import (
    NON_ZONE_LABELS,
    CriteriaError,
    current_criteria_version,
    default_criteria_path,
    load_criteria,
)
from .dataset import feature_names_for, feature_row
from .registry import ModelBinding, RegistryError, ZoneModelRegistry

__all__ = [
    "NOT_READY_STATES",
    "STATUS_ENABLED",
    "ZonePredictor",
    "runtime_inputs",
]

# status() vocabulary matches CapabilityState: ENABLED or DISABLED.
STATUS_ENABLED = "ENABLED"
NOT_READY_STATES = frozenset({ActivityState.UNKNOWN, ActivityState.SENSOR_OFFLINE, ActivityState.CALIBRATING})
_LOW_QUALITY = frozenset({QualityLevel.BAD, QualityLevel.UNAVAILABLE})
_EXPERIMENTAL_NOTE = (
    "EXPERIMENTAL: coarse zone class from a model that passed its predefined criteria on held-out "
    "sessions; scores are not probabilities and the zone centre is a display anchor, not a position."
)


def runtime_inputs(engine: Any, link_order: Sequence[str] | None = None
                   ) -> tuple[dict[str, FeatureVector], dict[str, ActivityState], dict[str, QualityLevel]]:
    """Collect predictor inputs from a :class:`ProcessingEngine`.

    ``link_order`` defaults to every link the engine knows; the predictor
    picks the links its model needs. A link's newest feature vector is only
    used if it belongs to the same window as the link's newest activity
    result, so features and state never describe different moments.
    """
    if link_order is None:
        link_order = engine.link_ids()
    latest: Mapping[str, ActivityResult] = engine.latest()
    feats: dict[str, FeatureVector] = {}
    states: dict[str, ActivityState] = {}
    quality: dict[str, QualityLevel] = {}
    for lid, res in latest.items():
        states[lid] = ActivityState(res.state)
        quality[lid] = QualityLevel(res.quality.level)
    for lid in link_order:
        hist = engine.feature_history(lid, 0.0)
        res = latest.get(lid)
        if not hist or res is None:
            continue
        fv = hist[-1]
        # A result without a window end (rejected window, offline) or with a
        # different one means the newest features are from an older window.
        w_end = res.provenance.window_end_unix_ns
        if w_end is None or fv.t_unix_end_ns != w_end:
            continue
        feats[lid] = fv
    return feats, states, quality


class ZonePredictor:
    """Thread-safe. Call :meth:`refresh` after a model is trained or deleted."""

    def __init__(
        self,
        registry: ZoneModelRegistry,
        cfg: AppConfig,
        *,
        room: RoomGeometry | None = None,
        criteria_path: Path | None = None,
        model_id: str | None = None,
    ) -> None:
        self.registry = registry
        self.cfg = cfg
        self._criteria_path = criteria_path if criteria_path is not None else default_criteria_path(cfg)
        self._room = room
        self._pinned = model_id
        self._lock = threading.RLock()
        self._bindings: list[ModelBinding] = []
        self._errors: list[str] = []
        self._pipelines: dict[str, tuple[Any | None, list[str]]] = {}
        self.refresh()

    # --------------------------------------------------------------- setup
    def refresh(self) -> None:
        """Re-read model bindings (JSON only) from the registry."""
        with self._lock:
            self._bindings, self._errors = self.registry.list_bindings()
            self._pipelines.clear()

    def set_room(self, room: RoomGeometry | None) -> None:
        with self._lock:
            self._room = room

    def pin(self, model_id: str | None) -> None:
        """Use only ``model_id`` (``None``: newest matching enabled model)."""
        with self._lock:
            self._pinned = model_id

    # ----------------------------------------------------------- selection
    @staticmethod
    def _mismatches(b: ModelBinding, *, room_hash: str | None, hardware_signature: str | None,
                    config_version: str | None, criteria_version: str | None) -> list[str]:
        out: list[str] = []
        if criteria_version is None:
            out.append("CRITERIA_UNAVAILABLE: the zone criteria file cannot be loaded")
        elif criteria_version != b.criteria_version:
            out.append(f"CRITERIA_CHANGED: model {b.model_id} was evaluated under criteria {b.criteria_version}, "
                       f"current criteria are {criteria_version}; re-evaluate on new held-out sessions")
        if hardware_signature is None:
            out.append("HARDWARE_SIGNATURE_UNAVAILABLE: no hardware signature yet (no frames from the links)")
        elif hardware_signature != b.hardware_signature:
            out.append(f"HARDWARE_SIGNATURE_MISMATCH: model {b.model_id} is bound to hardware "
                       f"{b.hardware_signature}, current hardware is {hardware_signature}")
        if room_hash is None or room_hash != b.room_config_hash:
            out.append(f"ROOM_CHANGED: model {b.model_id} is bound to room {b.room_config_hash}, "
                       f"current room is {room_hash}")
        if config_version is None or config_version != b.config_version:
            out.append(f"CONFIG_CHANGED: model {b.model_id} is bound to processing config {b.config_version}, "
                       f"current config is {config_version}")
        return out

    def _select(self, **ctx: str | None) -> tuple[ModelBinding | None, list[str]]:
        if self._pinned is not None:
            pinned = [b for b in self._bindings if b.model_id == self._pinned]
            if not pinned:
                return None, [f"NO_ENABLED_MODEL: pinned model {self._pinned} not found"]
            b = pinned[0]
            if not b.enabled:
                return None, [f"MODEL_NOT_ENABLED: model {b.model_id} did not pass its enablement criteria",
                              *b.enabled_reasons]
            mism = self._mismatches(b, **ctx)
            return (None, mism) if mism else (b, [])
        enabled = [b for b in self._bindings if b.enabled]
        if not enabled:
            reasons = ["NO_ENABLED_MODEL: no zone model has passed the predefined enablement criteria "
                       "(configs/zone_enablement.toml) on held-out sessions"]
            if self._bindings:
                newest = self._bindings[0]
                reasons.append(f"MODEL_NOT_ENABLED: newest model {newest.model_id}: "
                               + "; ".join(newest.enabled_reasons[:3]))
            reasons += self._errors[:3]
            return None, reasons
        first_mismatch: list[str] | None = None
        for b in enabled:  # newest first
            mism = self._mismatches(b, **ctx)
            if not mism:
                return b, []
            if first_mismatch is None:
                first_mismatch = mism
        return None, list(first_mismatch or [])

    def _pipeline(self, b: ModelBinding) -> tuple[Any | None, list[str]]:
        cached = self._pipelines.get(b.model_id)
        if cached is not None:
            return cached
        problems: list[str] = []
        pipeline = None
        try:
            loaded = self.registry.load(b.model_id)
            pipeline = loaded.pipeline
            if loaded.binding.joblib_sha256 != b.joblib_sha256:
                problems.append(f"MODEL_FILE_INVALID: model {b.model_id} changed on disk; refresh first")
        except RegistryError as exc:
            problems.append(f"MODEL_FILE_INVALID: {exc.code}: {exc.detail}")
        except Exception as exc:  # unpickling a damaged file can raise anything
            problems.append(f"MODEL_FILE_INVALID: could not load model {b.model_id} ({type(exc).__name__})")
        if pipeline is not None and not problems:
            problems += self._shape_problems(b, pipeline)
        result = (None if problems else pipeline, problems)
        self._pipelines[b.model_id] = result
        return result

    @staticmethod
    def _shape_problems(b: ModelBinding, pipeline: Any) -> list[str]:
        code = "MODEL_INPUT_SHAPE_MISMATCH"
        expected_names = feature_names_for(b.link_order)
        if tuple(b.feature_names) != expected_names:
            return [f"{code}: model {b.model_id} expects features {len(b.feature_names)} "
                    f"({b.feature_set_version}) that this software no longer produces "
                    f"({len(expected_names)} for links {list(b.link_order)})"]
        n_in = getattr(pipeline, "n_features_in_", None)
        if n_in is None or int(n_in) != len(b.feature_names):
            return [f"{code}: model {b.model_id} takes {n_in} inputs but is bound to {len(b.feature_names)} features"]
        fitted = [str(c) for c in getattr(pipeline, "classes_", [])]
        if sorted(fitted) != sorted(b.classes):
            return [f"{code}: model {b.model_id} classes {fitted} differ from the bound classes {list(b.classes)}"]
        if not hasattr(pipeline, "predict_proba"):
            return [f"{code}: model {b.model_id} does not produce class scores"]
        return []

    # --------------------------------------------------------------- status
    def status(
        self,
        *,
        room_hash: str | None,
        hardware_signature: str | None,
        config_version: str | None,
        criteria_version: str | None = None,
    ) -> dict[str, Any]:
        """``{state, reasons, model_id, criteria_version, criteria, report}`` for ``/api/zone/status``.

        ``state`` is ``ENABLED`` when a model passed its criteria and still
        matches this context (individual windows may still ABSTAIN), else
        ``DISABLED``. ``criteria`` is the parsed criteria file (``None`` if it
        cannot be loaded); ``report`` is the selected (or newest) model's report.
        """
        try:
            criteria: dict[str, Any] | None = load_criteria(self._criteria_path).to_dict()
        except CriteriaError:
            criteria = None
        crit = criteria_version if criteria_version is not None else (
            None if criteria is None else criteria["criteria_version"])
        with self._lock:
            b, reasons = self._select(room_hash=room_hash, hardware_signature=hardware_signature,
                                      config_version=config_version, criteria_version=crit)
            if b is not None:
                _, problems = self._pipeline(b)
                if problems:
                    return {"state": ZoneState.DISABLED.value, "reasons": problems, "model_id": b.model_id,
                            "criteria_version": crit, "criteria": criteria, "report": b.report}
                return {"state": STATUS_ENABLED, "reasons": [_EXPERIMENTAL_NOTE], "model_id": b.model_id,
                        "criteria_version": crit, "criteria": criteria, "report": b.report}
            newest = self._bindings[0] if self._bindings else None
            return {"state": ZoneState.DISABLED.value, "reasons": reasons,
                    "model_id": None, "criteria_version": crit, "criteria": criteria,
                    "report": None if newest is None else newest.report}

    # -------------------------------------------------------------- predict
    def predict(
        self,
        features_by_link: Mapping[str, FeatureVector],
        link_states: Mapping[str, ActivityState],
        room_hash: str | None,
        hardware_signature: str | None,
        config_version: str | None,
        criteria_version: str | None,
        provenance: Provenance | None,
        *,
        room: RoomGeometry | None = None,
        link_quality: Mapping[str, QualityLevel] | None = None,
        now_ns: int | None = None,
    ) -> ZonePrediction:
        """One zone decision for the current windows (see module docstring).

        ``now_ns`` (host monotonic, same clock as ``FeatureVector.t_end_ns``)
        enables the staleness check; ``link_quality`` the quality check.
        ``criteria_version=None`` reads the criteria file's current version.
        """
        crit = criteria_version if criteria_version is not None else current_criteria_version(self._criteria_path)
        with self._lock:
            b, reasons = self._select(room_hash=room_hash, hardware_signature=hardware_signature,
                                      config_version=config_version, criteria_version=crit)
            if b is None:
                return ZonePrediction(state=ZoneState.DISABLED, reasons=reasons, criteria_version=crit)

            def disabled(why: list[str]) -> ZonePrediction:
                return ZonePrediction(state=ZoneState.DISABLED, reasons=why, model_id=b.model_id,
                                      criteria_version=crit)

            pipeline, problems = self._pipeline(b)
            if problems or pipeline is None:
                return disabled(problems)
            if provenance is not None and SourceMode(provenance.source_mode) == SourceMode.SIMULATION:
                return disabled(["SIMULATED_SOURCE: zone estimation never runs on simulated data"])
            use_room = room if room is not None else self._room
            if use_room is None or use_room.config_hash() != b.room_config_hash:
                return disabled(["ROOM_UNAVAILABLE: the predictor has no room geometry matching the model's room"])
            zones = {z.id: z for z in use_room.zones}
            missing_zones = [z for z in b.zone_ids if z not in zones]
            if missing_zones:
                return disabled([f"ROOM_CHANGED: zones {missing_zones} are not in the room geometry"])

            def abstain(why: list[str], scores: dict[str, float] | None = None) -> ZonePrediction:
                return ZonePrediction(state=ZoneState.ABSTAIN, reasons=why, model_id=b.model_id,
                                      criteria_version=crit, model_scores=scores or {},
                                      provenance=self._provenance(provenance, b))

            # --- per-link readiness ----------------------------------------
            why: list[str] = []
            stale_s = self.cfg.acquisition.stale_after_s
            for lid in b.link_order:
                fv = features_by_link.get(lid)
                state = link_states.get(lid)
                if fv is None:
                    why.append(f"LINK_MISSING: no current feature window for required link {lid}")
                if state is None:
                    why.append(f"LINK_STATE_UNKNOWN: no activity state for required link {lid}")
                elif ActivityState(state) in NOT_READY_STATES:
                    why.append(f"LINK_NOT_READY: required link {lid} is {ActivityState(state).value}")
                if link_quality is not None:
                    q = link_quality.get(lid)
                    if q is None or QualityLevel(q) in _LOW_QUALITY:
                        why.append(f"LOW_QUALITY: required link {lid} quality is "
                                   f"{'unknown' if q is None else QualityLevel(q).value}")
                if fv is not None and now_ns is not None and (now_ns - fv.t_end_ns) / 1e9 > stale_s:
                    why.append(f"LINK_STALE: newest window of {lid} is {(now_ns - fv.t_end_ns) / 1e9:.1f} s old")
            if provenance is None:
                why.append("NO_PROVENANCE: an estimate needs provenance")
            if why:
                return abstain(why)
            ok, detail = align_link_windows({lid: features_by_link[lid].t_end_ns for lid in b.link_order},
                                            self.cfg.acquisition.alignment_tolerance_s)
            if not ok:
                return abstain([f"LINKS_NOT_ALIGNED: {detail}"])

            # --- model ------------------------------------------------------
            row, row_why = feature_row(features_by_link, b.link_order)
            if row is None:
                if any(r.startswith("FEATURE_LAYOUT_CHANGED") for r in row_why):
                    return disabled([f"MODEL_INPUT_SHAPE_MISMATCH: {r}" for r in row_why])
                return abstain(row_why)
            if row.shape != (len(b.feature_names),):
                return disabled([f"MODEL_INPUT_SHAPE_MISMATCH: input row has {row.shape[0]} values, model is "
                                 f"bound to {len(b.feature_names)}"])
            try:
                raw = pipeline.predict_proba(row[None, :])[0]
            except ValueError as exc:
                return disabled([f"MODEL_INPUT_SHAPE_MISMATCH: {exc}"])
            fitted = [str(c) for c in pipeline.classes_]
            scores = {c: float(raw[fitted.index(c)]) for c in b.classes if c in fitted}
            top_cls = max(scores, key=lambda c: (scores[c], -b.classes.index(c)))
            top = scores[top_cls]
            if not np.isfinite(top) or top < b.threshold:
                return abstain([f"LOW_MODEL_SCORE: top score {top:.3f} ({top_cls}) is below the frozen "
                                f"threshold {b.threshold:g}"], scores)
            if top_cls in NON_ZONE_LABELS:
                return abstain([f"NO_TARGET_ZONE_PREDICTED: the model's top class is {top_cls}"], scores)
            zone = zones[top_cls]
            prov = self._provenance(provenance, b)
            assert prov is not None
            return ZonePrediction(
                state=ZoneState.ESTIMATE,
                zone_id=zone.id,
                zone_label=zone.label,
                model_scores=scores,
                display_anchor=zone.display_anchor(),
                reasons=[_EXPERIMENTAL_NOTE],
                model_id=b.model_id,
                criteria_version=crit,
                provenance=prov,
            )

    @staticmethod
    def _provenance(p: Provenance | None, b: ModelBinding) -> Provenance | None:
        if p is None:
            return None
        return p.model_copy(update={"model_version": b.model_id, "link_ids": list(b.link_order)})
