"""Session-level training, validation-only tuning and a single test evaluation.

Order of operations (each step only sees what it is allowed to see):

1. **Split by session, never by window.** Either every session names its
   split, or splits are made automatically, chronologically per class: the
   earliest sessions train, the next validate, the latest test. A session's
   windows therefore always stay in one split.
2. **Fit on TRAIN only:** ``StandardScaler`` (+ optional ``SelectKBest``) +
   ``LogisticRegression(class_weight="balanced")`` in one scikit-learn
   ``Pipeline``, so nothing is ever fitted on validation or test windows.
3. **Tune the abstention threshold on VALIDATION only**, with the grid and
   rule from the criteria file, then freeze it.
4. **Evaluate once on TEST** with the frozen pipeline and threshold. The
   test rows are sliced only after steps 2-3 are complete.
5. Compare every measured value with the predefined criteria. The model is
   enabled only if all of them pass and no synthetic session was used.

Nothing here lowers a threshold, retries with other settings after looking
at test results, or turns a failed criterion into a pass.
"""

from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_selection import SelectKBest, f_classif
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ...config import AppConfig
from ...schemas import GeometryProvenance, RoomGeometry
from .criteria import NON_ZONE_LABELS, ZoneCriteria, default_criteria_path, load_criteria
from .dataset import (
    FEATURE_SET_VERSION,
    SPLIT_TEST,
    SPLIT_TRAIN,
    SPLIT_VALIDATION,
    SPLITS,
    SessionSpec,
    SessionSummary,
    ZoneDataset,
    build_dataset,
)
from .evaluate import (
    REPORT_VERSION,
    SCORES_NOTE,
    abstention_sweep,
    choose_threshold,
    enabled_decision,
    evaluate_criteria,
    evaluate_predictions,
    json_safe,
    split_counts,
)
from .registry import ModelBinding, ZoneModelRegistry

__all__ = [
    "MAX_ITER",
    "NEVER_ANSWER_THRESHOLD",
    "TrainingRefused",
    "SplitPlan",
    "TrainedZoneModel",
    "room_classes",
    "plan_splits",
    "build_pipeline",
    "train_and_evaluate",
    "run_training",
]

MAX_ITER = 10_000
# Used when no grid threshold is acceptable on validation. Model scores lie in
# [0, 1], so this threshold makes the model abstain on every window.
NEVER_ANSWER_THRESHOLD = 2.0
# Automatic splits give validation and test at least this share of a class's
# sessions (subject to the criteria minimums).
AUTO_HOLDOUT_FRACTION = 0.2


class TrainingRefused(Exception):
    """Training cannot proceed honestly (not a criteria failure)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SplitPlan:
    assignment: dict[str, str]  # session_key -> split
    method: str  # "user" or "chronological_per_class"

    def keys(self, split: str) -> list[str]:
        return sorted(k for k, s in self.assignment.items() if s == split)


@dataclass
class TrainedZoneModel:
    pipeline: Pipeline
    classes: tuple[str, ...]
    zone_ids: tuple[str, ...]
    threshold: float
    link_order: tuple[str, ...]
    feature_names: tuple[str, ...]
    feature_set_version: str
    hardware_signature: str | None
    room_config_hash: str
    config_version: str
    criteria_version: str
    synthetic_data_used: bool
    enabled: bool
    report: dict[str, Any]
    split_masks: dict[str, np.ndarray] = field(default_factory=dict, repr=False)


def room_classes(room: RoomGeometry) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(target zone ids in room order, all classes in fixed report order)."""
    zones = tuple(z.id for z in room.zones if z.kind == "TARGET_ROOM_ZONE")
    clash = sorted(set(zones) & set(NON_ZONE_LABELS))
    if clash:
        raise TrainingRefused("INVALID_ZONE_IDS", f"zone ids {clash} collide with the non-zone class names")
    return zones, zones + NON_ZONE_LABELS


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


def plan_splits(sessions: Sequence[SessionSummary], classes: Sequence[str], criteria: ZoneCriteria) -> SplitPlan:
    """User-given splits, or automatic chronological splits per class."""
    requested = [s.requested_split for s in sessions]
    if any(r is not None for r in requested):
        if any(r is None for r in requested):
            raise TrainingRefused("PARTIAL_SPLITS", "either every session names its split or none does")
        return SplitPlan({s.session_key: str(s.requested_split) for s in sessions}, "user")

    unknown_start = sorted(s.session_key for s in sessions if s.start_unix_ns is None)
    if unknown_start:
        raise TrainingRefused("SESSION_START_UNKNOWN",
                              f"cannot order sessions in time (no start time): {unknown_start}")
    need = (criteria.min_sessions_per_class_train + criteria.min_sessions_per_class_validation
            + criteria.min_sessions_per_class_test)
    assignment: dict[str, str] = {}
    short: dict[str, int] = {}
    for c in classes:
        own = sorted((s for s in sessions if s.label == c), key=lambda s: (s.start_unix_ns, s.session_key))
        n = len(own)
        if n < need:
            short[c] = n
            continue
        n_test = max(criteria.min_sessions_per_class_test, int(math.floor(AUTO_HOLDOUT_FRACTION * n)))
        n_val = max(criteria.min_sessions_per_class_validation, int(math.floor(AUTO_HOLDOUT_FRACTION * n)))
        if n - n_test - n_val < criteria.min_sessions_per_class_train:
            n_test, n_val = criteria.min_sessions_per_class_test, criteria.min_sessions_per_class_validation
        n_train = n - n_test - n_val
        for i, s in enumerate(own):
            assignment[s.session_key] = (SPLIT_TRAIN if i < n_train
                                         else SPLIT_VALIDATION if i < n_train + n_val else SPLIT_TEST)
    if short:
        raise TrainingRefused(
            "INSUFFICIENT_SESSIONS",
            f"automatic splits need at least {need} sessions per class "
            f"({criteria.min_sessions_per_class_train} train + {criteria.min_sessions_per_class_validation} "
            f"validation + {criteria.min_sessions_per_class_test} test); have {short}",
        )
    return SplitPlan(assignment, "chronological_per_class")


def _test_after(sessions: Sequence[SessionSummary], plan: SplitPlan) -> bool | None:
    """True if every test session starts after every train/validation session
    has ended (its end, or its start when the end is unknown)."""
    by_key = {s.session_key: s for s in sessions}
    test = [by_key[k] for k in plan.keys(SPLIT_TEST)]
    earlier = [by_key[k] for k in plan.keys(SPLIT_TRAIN) + plan.keys(SPLIT_VALIDATION)]
    if not test or not earlier:
        return None
    if any(s.start_unix_ns is None for s in test + earlier):
        return None
    first_test = min(int(s.start_unix_ns) for s in test)  # type: ignore[arg-type]
    last_end = max(int(s.end_unix_ns if s.end_unix_ns is not None else s.start_unix_ns)  # type: ignore[arg-type]
                   for s in earlier)
    return first_test > last_end


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def build_pipeline(n_features: int, *, select_k: int | None = None, random_state: int = 0) -> Pipeline:
    steps: list[tuple[str, Any]] = [("scaler", StandardScaler())]
    if select_k is not None:
        if select_k < 1:
            raise ValueError("select_k must be >= 1")
        steps.append(("select", SelectKBest(f_classif, k=min(int(select_k), n_features))))
    steps.append(("classifier", LogisticRegression(class_weight="balanced", max_iter=MAX_ITER,
                                                   random_state=random_state)))
    return Pipeline(steps)


def _scores_in_order(pipeline: Pipeline, X: np.ndarray, classes: Sequence[str]) -> np.ndarray:
    """``predict_proba`` columns re-ordered to ``classes``."""
    if X.shape[0] == 0:
        return np.zeros((0, len(classes)))
    raw = pipeline.predict_proba(X)
    fitted = [str(c) for c in pipeline.classes_]
    out = np.zeros((X.shape[0], len(classes)))
    for j, c in enumerate(classes):
        if c in fitted:
            out[:, j] = raw[:, fitted.index(c)]
    return out


def train_and_evaluate(
    dataset: ZoneDataset,
    criteria: ZoneCriteria,
    *,
    room: RoomGeometry,
    min_receivers: int | None = None,
    plan: SplitPlan | None = None,
    select_k: int | None = None,
    random_state: int = 0,
    now_unix_ns: int | None = None,
) -> TrainedZoneModel:
    """Steps 1-5 of the module docstring on an already built dataset."""
    zone_ids, classes = room_classes(room)
    labels_seen = {s.label for s in dataset.sessions}
    unknown = sorted(labels_seen - set(classes))
    if unknown:
        raise TrainingRefused("UNKNOWN_LABEL", f"labels {unknown} are neither room zones nor {list(NON_ZONE_LABELS)}")

    plan = plan if plan is not None else plan_splits(dataset.sessions, classes, criteria)
    keys = {s.session_key for s in dataset.sessions}
    if set(plan.assignment) != keys or any(v not in SPLITS for v in plan.assignment.values()):
        raise TrainingRefused("INVALID_SPLITS", "every session must be assigned to exactly one of train/validation/test")

    split_of = np.asarray([plan.assignment[k] for k in dataset.session_ids.tolist()], dtype=object)
    masks = {sp: split_of == sp for sp in SPLITS}
    train_m, val_m = masks[SPLIT_TRAIN], masks[SPLIT_VALIDATION]
    y = dataset.y.astype(object)
    train_classes = sorted(set(y[train_m].tolist()))
    if len(train_classes) < 2:
        raise TrainingRefused("INSUFFICIENT_TRAINING_DATA", f"training windows cover {train_classes}; need >= 2 classes")
    missing_train = [c for c in classes if c not in train_classes]
    if missing_train:
        raise TrainingRefused("INSUFFICIENT_TRAINING_DATA", f"no training windows for classes {missing_train}")
    if not val_m.any():
        raise TrainingRefused("NO_VALIDATION_WINDOWS", "the validation sessions produced no usable windows")
    if not masks[SPLIT_TEST].any():
        raise TrainingRefused("NO_TEST_WINDOWS", "the test sessions produced no usable windows")

    # --- 2. fit on TRAIN only ------------------------------------------------
    X = dataset.X
    pipeline = build_pipeline(X.shape[1], select_k=select_k, random_state=random_state)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        pipeline.fit(X[train_m], y[train_m])
    converged = not any(issubclass(w.category, ConvergenceWarning) for w in caught)

    # --- 3. tune the abstention threshold on VALIDATION only ----------------
    val_scores = _scores_in_order(pipeline, X[val_m], classes)
    sweep = abstention_sweep(val_scores, y[val_m], classes, criteria.threshold_grid)
    threshold, rule = choose_threshold(sweep, criteria.max_validation_abstention_rate)
    if threshold is None:
        # No grid value is acceptable: freeze a "never answer" threshold so
        # the model cannot pass anything by accident.
        threshold = NEVER_ANSWER_THRESHOLD
    val_metrics = evaluate_predictions(y[val_m], val_scores, classes, zone_ids, threshold)

    # --- 4. evaluate ONCE on TEST with everything frozen --------------------
    test_m = masks[SPLIT_TEST]
    meta = {s.session_key: {"label": s.label, "start_unix_ns": s.start_unix_ns} for s in dataset.sessions}
    test_scores = _scores_in_order(pipeline, X[test_m], classes)
    test_metrics = evaluate_predictions(y[test_m], test_scores, classes, zone_ids, threshold,
                                        session_ids=dataset.session_ids[test_m], session_meta=meta)

    # --- 5. criteria ----------------------------------------------------------
    counts = split_counts(dataset.y, dataset.session_ids, plan.assignment, classes, SPLITS)
    sessions_per_split: dict[str, dict[str, int]] = {sp: {c: 0 for c in classes} for sp in SPLITS}
    for s in dataset.sessions:
        if s.label in sessions_per_split[plan.assignment[s.session_key]]:
            # Sessions only count if they contributed at least one window.
            if s.windows_kept > 0:
                sessions_per_split[plan.assignment[s.session_key]][s.label] += 1
    need_rx = max(criteria.min_receivers, int(min_receivers or 0))
    synthetic = dataset.synthetic_session_keys
    checks = evaluate_criteria(
        criteria,
        zone_ids=zone_ids,
        classes=classes,
        sessions_per_split=sessions_per_split,
        test_windows_per_class=counts[SPLIT_TEST]["windows"],
        test_after_train_and_validation=_test_after(dataset.sessions, plan),
        synthetic_sessions=synthetic,
        receivers=dataset.receivers,
        min_receivers=need_rx,
        test=test_metrics,
        hardware_signatures=dataset.hardware_signatures,
        room_user_provided=GeometryProvenance(room.provenance) == GeometryProvenance.USER_PROVIDED,
    )
    enabled, reasons = enabled_decision(checks)
    enabled = enabled and not synthetic

    sigs = dataset.hardware_signatures
    hw = sigs[0] if len(sigs) == 1 else None
    # Leakage check, recorded in the report: sessions and windows per split.
    split_sets = {sp: set(plan.keys(sp)) for sp in SPLITS}
    overlaps = {f"{a}&{b}": sorted(split_sets[a] & split_sets[b])
                for i, a in enumerate(SPLITS) for b in SPLITS[i + 1:]}
    classifier = pipeline.named_steps["classifier"]
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "created_at_unix_ns": int(now_unix_ns if now_unix_ns is not None else time.time_ns()),
        "criteria_version": criteria.criteria_version,
        "criteria_path": criteria.source_path,
        "criteria_values": criteria.to_dict(),
        "criteria": checks,
        "enabled": enabled,
        "enabled_reasons": reasons,
        "synthetic_data_used": bool(synthetic),
        "synthetic_sessions": synthetic,
        "scope": {
            "max_participants": criteria.max_participants,
            "description": criteria.scope_description,
            "note": "Session labels are declared by the operator; the number of participants is not measured.",
        },
        "scores_note": SCORES_NOTE,
        "display_note": "Zone centres are display anchors, not measured positions.",
        "binding": {
            "hardware_signature": hw,
            "hardware_signatures_seen": sigs,
            "room_config_hash": room.config_hash(),
            "room_provenance": GeometryProvenance(room.provenance).value,
            "config_version": dataset.config_version,
            "link_order": list(dataset.link_order),
            "receivers": dataset.receivers,
            "feature_names": list(dataset.feature_names),
            "feature_set_version": dataset.feature_set_version,
            "classes": list(classes),
            "zone_ids": list(zone_ids),
        },
        "splits": {
            "method": plan.method,
            "assignment": dict(sorted(plan.assignment.items())),
            "leakage_check": {
                "unit": "session",
                "overlapping_sessions": overlaps,
                "no_session_in_two_splits": all(not v for v in overlaps.values()),
            },
        },
        "counts": counts,
        "sessions_per_split": sessions_per_split,
        "model": {
            "pipeline": [name for name, _ in pipeline.steps],
            "classifier": "LogisticRegression(class_weight='balanced')",
            "max_iter": MAX_ITER,
            "n_iter": json_safe(getattr(classifier, "n_iter_", None)),
            "converged": converged,
            "select_k": select_k,
            "fitted_on": SPLIT_TRAIN,
            "n_train_windows": int(train_m.sum()),
        },
        "threshold_tuning": {
            "split": SPLIT_VALIDATION,
            "n_windows": int(val_m.sum()),
            "grid": list(criteria.threshold_grid),
            "max_validation_abstention_rate": criteria.max_validation_abstention_rate,
            "rule": rule,
            "sweep": sweep,
            "chosen_threshold": None if threshold == NEVER_ANSWER_THRESHOLD else threshold,
            "frozen": True,
        },
        "validation": val_metrics,
        "test": test_metrics,
        "test_evaluations": 1,
        "dataset": dataset.summary(),
    }
    return TrainedZoneModel(
        pipeline=pipeline,
        classes=classes,
        zone_ids=zone_ids,
        threshold=threshold,
        link_order=dataset.link_order,
        feature_names=dataset.feature_names,
        feature_set_version=dataset.feature_set_version,
        hardware_signature=hw,
        room_config_hash=room.config_hash(),
        config_version=dataset.config_version,
        criteria_version=criteria.criteria_version,
        synthetic_data_used=bool(synthetic),
        enabled=enabled,
        report=json_safe(report),
        split_masks=masks,
    )


def run_training(
    specs: Sequence[SessionSpec],
    cfg: AppConfig,
    room: RoomGeometry,
    *,
    data_dir: Path | None = None,
    db: Any | None = None,
    criteria_path: Path | None = None,
    registry: ZoneModelRegistry | None = None,
    link_order: Sequence[str] | None = None,
    select_k: int | None = None,
) -> tuple[TrainedZoneModel, ModelBinding | None]:
    """Load criteria, build the dataset, train, evaluate and (optionally) save.

    A model that fails its criteria is still saved (disabled) when a
    registry is given, so its report can be inspected.
    """
    criteria = load_criteria(criteria_path if criteria_path is not None else default_criteria_path(cfg))
    zone_ids, classes = room_classes(room)
    dataset = build_dataset(
        specs, cfg,
        trim_session_edges_s=criteria.trim_session_edges_s,
        link_order=link_order,
        allowed_labels=classes,
        data_dir=data_dir if data_dir is not None else cfg.storage.resolved_data_dir(),
        db=db,
    )
    trained = train_and_evaluate(dataset, criteria, room=room, min_receivers=cfg.zone.min_receivers,
                                 select_k=select_k)
    binding = None
    if registry is not None:
        binding = registry.save(
            trained.pipeline,
            hardware_signature=trained.hardware_signature,
            room_config_hash=trained.room_config_hash,
            config_version=trained.config_version,
            criteria_version=trained.criteria_version,
            link_order=trained.link_order,
            feature_names=trained.feature_names,
            feature_set_version=trained.feature_set_version or FEATURE_SET_VERSION,
            threshold=trained.threshold,
            classes=trained.classes,
            zone_ids=trained.zone_ids,
            synthetic_data_used=trained.synthetic_data_used,
            report=trained.report,
        )
    return trained, binding

