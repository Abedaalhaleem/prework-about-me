"""Metrics, criteria checks and the enablement decision for zone models.

Everything here is a pure function of arrays and counts, so every number in
a report can be recomputed from the report's own inputs.

Vocabulary
----------
* **model scores** are the classifier's per-class outputs
  (``predict_proba`` of a logistic regression). They are *not* claimed to
  be calibrated probabilities and are never labelled as such.
* A window is **abstained** when its top score is below the frozen
  threshold. Abstained windows are never counted as correct.
* **non-abstained** metrics are computed only over windows the model
  answered. The abstention rate is always reported next to them, so
  abstaining cannot hide errors unnoticed.
* The EMPTY / OUTSIDE "predicted as zone" rates are reported over all
  windows of the class *and* over its non-abstained windows. The criterion
  uses the larger of the two, so abstention cannot make them look better.

A model is enabled only if every criterion from the predefined criteria file
passes, **and** no synthetic session was used, **and** the eligibility
checks pass (single hardware signature, enough receivers, a user-provided
room). :func:`report_supports_enabled` re-checks a stored report before any
model is used.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from ...validation.metrics import wilson_interval
from .criteria import EMPTY_LABEL, NON_ZONE_LABELS, OUTSIDE_LABEL, ZoneCriteria

__all__ = [
    "ABSTAIN",
    "REPORT_VERSION",
    "CRITERIA_FILE_CHECKS",
    "ELIGIBILITY_CHECKS",
    "SCORES_NOTE",
    "decide",
    "balanced_accuracy",
    "abstention_sweep",
    "choose_threshold",
    "evaluate_predictions",
    "split_counts",
    "evaluate_criteria",
    "enabled_decision",
    "report_supports_enabled",
    "json_safe",
    "zone_ids_from_classes",
]

ABSTAIN = "ABSTAIN"
REPORT_VERSION = "roomsense-zone-report-v1"
SCORES_NOTE = (
    "model_scores are logistic-regression outputs used for ranking and abstention; they are not "
    "calibrated probabilities of presence or location."
)

# Every check that must appear, and pass, in an enabled report.
CRITERIA_FILE_CHECKS: tuple[str, ...] = (
    "min_zones",
    "required_non_zone_classes",
    "min_sessions_per_class_train",
    "min_sessions_per_class_validation",
    "min_sessions_per_class_test",
    "require_test_after_train_and_validation",
    "min_test_windows_per_class",
    "allow_synthetic_sessions",
    "min_receivers",
    "min_test_balanced_accuracy_non_abstained",
    "min_test_accuracy_wilson_lower_95",
    "max_test_abstention_rate",
    "max_empty_predicted_as_zone_rate",
    "max_outside_predicted_as_zone_rate",
    "min_per_zone_recall",
)
ELIGIBILITY_CHECKS: tuple[str, ...] = (
    "no_synthetic_sessions",
    "single_hardware_signature",
    "room_user_provided",
)


def json_safe(obj: Any) -> Any:
    """Plain-JSON copy: numpy scalars/arrays to Python, NaN/inf to None."""
    if isinstance(obj, Mapping):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [json_safe(v) for v in obj.tolist()]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if obj is None or isinstance(obj, str):
        return obj
    return str(obj)


# ---------------------------------------------------------------------------
# Decisions and basic metrics
# ---------------------------------------------------------------------------


def decide(scores: np.ndarray, classes: Sequence[str], threshold: float
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Top class, abstention mask and top score per row of ``scores``.

    ``scores`` columns follow ``classes``. A row abstains when its top score
    is strictly below ``threshold``.
    """
    s = np.asarray(scores, dtype=np.float64)
    if s.ndim != 2 or s.shape[1] != len(classes):
        raise ValueError(f"scores shape {s.shape} does not match {len(classes)} classes")
    if s.shape[0] == 0:
        return np.zeros(0, dtype=object), np.zeros(0, dtype=bool), np.zeros(0)
    idx = np.argmax(s, axis=1)
    top = s[np.arange(s.shape[0]), idx]
    pred = np.asarray([classes[i] for i in idx], dtype=object)
    return pred, top < threshold, top


def balanced_accuracy(y_true: Sequence[str], y_pred: Sequence[str], classes: Sequence[str]) -> float | None:
    """Mean per-class recall over the classes present in ``y_true``."""
    yt = np.asarray(y_true, dtype=object)
    yp = np.asarray(y_pred, dtype=object)
    recalls = []
    for c in classes:
        m = yt == c
        if m.any():
            recalls.append(float(np.mean(yp[m] == c)))
    return float(np.mean(recalls)) if recalls else None


def abstention_sweep(scores: np.ndarray, y_true: Sequence[str], classes: Sequence[str], grid: Sequence[float]
                     ) -> list[dict[str, Any]]:
    """Abstention rate and non-abstained balanced accuracy for every threshold."""
    yt = np.asarray(y_true, dtype=object)
    out: list[dict[str, Any]] = []
    for t in grid:
        pred, abst, _ = decide(scores, classes, float(t))
        n = int(yt.size)
        keep = ~abst
        out.append({
            "threshold": float(t),
            "windows": n,
            "abstained": int(abst.sum()),
            "abstention_rate": (float(abst.mean()) if n else None),
            "balanced_accuracy_non_abstained": balanced_accuracy(yt[keep], pred[keep], classes) if keep.any() else None,
        })
    return out


def choose_threshold(sweep: Sequence[Mapping[str, Any]], max_abstention_rate: float) -> tuple[float | None, str]:
    """The criteria file's rule: among thresholds whose abstention rate is at
    most ``max_abstention_rate``, take those with the highest non-abstained
    balanced accuracy, and of those the smallest threshold."""
    eligible = [
        s for s in sweep
        if s["abstention_rate"] is not None and s["abstention_rate"] <= max_abstention_rate + 1e-12
        and s["balanced_accuracy_non_abstained"] is not None
    ]
    if not eligible:
        return None, "NO_ELIGIBLE_THRESHOLD: no grid threshold keeps validation abstention within the limit"
    best = max(s["balanced_accuracy_non_abstained"] for s in eligible)
    ties = [s["threshold"] for s in eligible if s["balanced_accuracy_non_abstained"] >= best - 1e-12]
    t = min(ties)
    return float(t), (f"smallest threshold with validation abstention <= {max_abstention_rate:g} and maximal "
                      f"non-abstained balanced accuracy ({best:.4f})")


def _rate(num: int, den: int) -> float | None:
    return (num / den) if den else None


def evaluate_predictions(
    y_true: Sequence[str],
    scores: np.ndarray,
    classes: Sequence[str],
    zone_ids: Sequence[str],
    threshold: float,
    *,
    session_ids: Sequence[str] | None = None,
    session_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """All split-level metrics at a frozen threshold (see module docstring)."""
    classes = list(classes)
    zones = set(zone_ids)
    yt = np.asarray(y_true, dtype=object)
    pred, abst, top = decide(scores, classes, threshold)
    keep = ~abst
    n = int(yt.size)

    cols = classes + [ABSTAIN]
    matrix: list[list[int]] = []
    per_class: dict[str, Any] = {}
    for c in classes:
        m = yt == c
        row = [int(np.sum(m & keep & (pred == p))) for p in classes] + [int(np.sum(m & abst))]
        matrix.append(row)
        n_c, n_abst = int(m.sum()), int(np.sum(m & abst))
        n_ans = n_c - n_abst
        correct = int(np.sum(m & keep & (pred == c)))
        per_class[c] = {
            "windows": n_c,
            "abstained": n_abst,
            "abstention_rate": _rate(n_abst, n_c),
            "answered": n_ans,
            "correct": correct,
            "recall_non_abstained": _rate(correct, n_ans),
            "predicted_as": {p: int(np.sum(m & keep & (pred == p))) for p in classes if p != c
                             and int(np.sum(m & keep & (pred == p))) > 0},
        }

    per_zone_errors = {
        z: {
            "recall_non_abstained": per_class[z]["recall_non_abstained"] if z in per_class else None,
            "answered": per_class[z]["answered"] if z in per_class else 0,
            "errors": per_class[z]["predicted_as"] if z in per_class else {},
            "error_count": (per_class[z]["answered"] - per_class[z]["correct"]) if z in per_class else 0,
        }
        for z in zone_ids
    }

    def as_zone(label: str) -> dict[str, Any]:
        m = yt == label
        n_all = int(m.sum())
        n_ans = int(np.sum(m & keep))
        hits = int(np.sum(m & keep & np.isin(pred, list(zones))))
        over_all, over_ans = _rate(hits, n_all), _rate(hits, n_ans)
        defined = [r for r in (over_all, over_ans) if r is not None]
        return {
            "windows": n_all,
            "answered": n_ans,
            "predicted_as_zone": hits,
            "rate_over_all_windows": over_all,
            "rate_over_non_abstained": over_ans,
            "criterion_value": max(defined) if defined else None,
        }

    n_ans = int(keep.sum())
    n_correct = int(np.sum(keep & (pred == yt)))
    w = wilson_interval(n_correct, n_ans)
    out: dict[str, Any] = {
        "threshold": float(threshold),
        "windows": n,
        "abstained": int(abst.sum()),
        "abstention_rate": _rate(int(abst.sum()), n),
        "answered": n_ans,
        "correct": n_correct,
        "accuracy_non_abstained": _rate(n_correct, n_ans),
        "balanced_accuracy_non_abstained": balanced_accuracy(yt[keep], pred[keep], classes) if n_ans else None,
        "accuracy_wilson_95": {"low": w.ci_low, "high": w.ci_high, "successes": n_correct, "trials": n_ans,
                               "method": w.method, "reason": w.reason},
        "confusion_matrix": {"rows_true": classes, "columns_predicted": cols, "counts": matrix},
        "per_class": per_class,
        "per_zone_errors": per_zone_errors,
        "empty_predicted_as_zone": as_zone(EMPTY_LABEL),
        "outside_predicted_as_zone": as_zone(OUTSIDE_LABEL),
        "mean_top_score": float(np.mean(top)) if n else None,
        "scores_note": SCORES_NOTE,
    }

    if session_ids is not None:
        sid = np.asarray(session_ids, dtype=object)
        meta = session_meta or {}
        rows = []
        for key in sorted(set(sid.tolist())):
            m = sid == key
            n_s, a_s = int(m.sum()), int(np.sum(m & abst))
            c_s = int(np.sum(m & keep & (pred == yt)))
            info = meta.get(key, {})
            rows.append({
                "session_key": key,
                "label": info.get("label", str(yt[m][0]) if n_s else None),
                "start_unix_ns": info.get("start_unix_ns"),
                "windows": n_s,
                "abstained": a_s,
                "abstention_rate": _rate(a_s, n_s),
                "accuracy_non_abstained": _rate(c_s, n_s - a_s),
            })
        rows.sort(key=lambda r: (r["start_unix_ns"] is None, r["start_unix_ns"] or 0, r["session_key"]))
        out["per_session"] = rows
    return out


def split_counts(labels: Sequence[str], session_ids: Sequence[str], assignment: Mapping[str, str],
                 classes: Sequence[str], splits: Sequence[str]) -> dict[str, Any]:
    """Window and session counts per class and split, plus class balance."""
    y = np.asarray(labels, dtype=object)
    sid = np.asarray(session_ids, dtype=object)
    split_of = np.asarray([assignment.get(s, "") for s in sid.tolist()], dtype=object)
    out: dict[str, Any] = {}
    for sp in splits:
        m = split_of == sp
        windows = {c: int(np.sum(m & (y == c))) for c in classes}
        total = int(m.sum())
        sessions = {c: 0 for c in classes}
        for key, s in assignment.items():
            if s != sp:
                continue
            idx = np.flatnonzero(sid == key)
            if idx.size:
                lbl = str(y[idx[0]])
                if lbl in sessions:
                    sessions[lbl] += 1
        out[sp] = {
            "windows": windows,
            "total_windows": total,
            "sessions_with_windows": sessions,
            "class_balance": {c: (windows[c] / total if total else None) for c in classes},
        }
    return out


# ---------------------------------------------------------------------------
# Criteria and the enablement decision
# ---------------------------------------------------------------------------


def _check(name: str, source: str, comparator: str, threshold: Any, measured: Any, passed: bool | None,
           detail: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "source": source,
        "comparator": comparator,
        "threshold": threshold,
        "measured": measured,
        # Anything that could not be measured fails: "NOT MEASURED" is not a pass.
        "passed": bool(passed) if measured is not None and passed is not None else False,
        "detail": detail if measured is not None else ("NOT MEASURED" + (f": {detail}" if detail else "")),
    }


def _ge(v: float | None, t: float) -> bool | None:
    return None if v is None else v >= t - 1e-12


def _le(v: float | None, t: float) -> bool | None:
    return None if v is None else v <= t + 1e-12


def evaluate_criteria(
    criteria: ZoneCriteria,
    *,
    zone_ids: Sequence[str],
    classes: Sequence[str],
    sessions_per_split: Mapping[str, Mapping[str, int]],
    test_windows_per_class: Mapping[str, int],
    test_after_train_and_validation: bool | None,
    synthetic_sessions: Sequence[str],
    receivers: Sequence[str],
    min_receivers: int,
    test: Mapping[str, Any] | None,
    hardware_signatures: Sequence[str | None],
    room_user_provided: bool,
) -> list[dict[str, Any]]:
    """Every predefined criterion (and eligibility check) with its measured
    value and pass/fail."""
    src = "criteria_file"
    checks: list[dict[str, Any]] = []
    checks.append(_check("min_zones", src, ">=", criteria.min_zones, len(zone_ids),
                         len(zone_ids) >= criteria.min_zones, f"target zones: {list(zone_ids)}"))
    missing_nz = [c for c in criteria.required_non_zone_classes if c not in classes]
    checks.append(_check("required_non_zone_classes", src, "present", list(criteria.required_non_zone_classes),
                         [c for c in criteria.required_non_zone_classes if c in classes], not missing_nz,
                         f"missing: {missing_nz}" if missing_nz else "all present"))
    for split, key, minimum in (
        ("train", "min_sessions_per_class_train", criteria.min_sessions_per_class_train),
        ("validation", "min_sessions_per_class_validation", criteria.min_sessions_per_class_validation),
        ("test", "min_sessions_per_class_test", criteria.min_sessions_per_class_test),
    ):
        per = {c: int(sessions_per_split.get(split, {}).get(c, 0)) for c in classes}
        low = min(per.values()) if per else None
        checks.append(_check(key, src, ">=", minimum, low, _ge(low, minimum), f"sessions per class: {per}"))
    if criteria.require_test_after_train_and_validation:
        checks.append(_check(
            "require_test_after_train_and_validation", src, "==", True, test_after_train_and_validation,
            test_after_train_and_validation,
            "every test session starts after every training and validation session ended",
        ))
    else:
        checks.append(_check("require_test_after_train_and_validation", src, "==", False, "not required", True))
    tw = {c: int(test_windows_per_class.get(c, 0)) for c in classes}
    low_tw = min(tw.values()) if tw else None
    checks.append(_check("min_test_windows_per_class", src, ">=", criteria.min_test_windows_per_class, low_tw,
                         _ge(low_tw, criteria.min_test_windows_per_class), f"test windows per class: {tw}"))
    n_syn = len(synthetic_sessions)
    checks.append(_check("allow_synthetic_sessions", src, "==", criteria.allow_synthetic_sessions, n_syn,
                         criteria.allow_synthetic_sessions or n_syn == 0,
                         f"synthetic sessions used: {list(synthetic_sessions)}"))
    checks.append(_check("min_receivers", src, ">=", min_receivers, len(receivers),
                         len(receivers) >= min_receivers, f"receivers: {list(receivers)}"))

    t = test or {}
    ba = t.get("balanced_accuracy_non_abstained")
    checks.append(_check("min_test_balanced_accuracy_non_abstained", src, ">=",
                         criteria.min_test_balanced_accuracy_non_abstained, ba,
                         _ge(ba, criteria.min_test_balanced_accuracy_non_abstained)))
    wl = (t.get("accuracy_wilson_95") or {}).get("low")
    checks.append(_check("min_test_accuracy_wilson_lower_95", src, ">=", criteria.min_test_accuracy_wilson_lower_95,
                         wl, _ge(wl, criteria.min_test_accuracy_wilson_lower_95),
                         "Wilson 95 % lower bound of test accuracy on non-abstained windows"))
    ar = t.get("abstention_rate")
    checks.append(_check("max_test_abstention_rate", src, "<=", criteria.max_test_abstention_rate, ar,
                         _le(ar, criteria.max_test_abstention_rate)))
    er = (t.get("empty_predicted_as_zone") or {}).get("criterion_value")
    checks.append(_check("max_empty_predicted_as_zone_rate", src, "<=", criteria.max_empty_predicted_as_zone_rate,
                         er, _le(er, criteria.max_empty_predicted_as_zone_rate),
                         "larger of the rates over all and over non-abstained EMPTY test windows"))
    orr = (t.get("outside_predicted_as_zone") or {}).get("criterion_value")
    checks.append(_check("max_outside_predicted_as_zone_rate", src, "<=",
                         criteria.max_outside_predicted_as_zone_rate, orr,
                         _le(orr, criteria.max_outside_predicted_as_zone_rate),
                         "larger of the rates over all and over non-abstained OUTSIDE_TARGET_ROOM test windows"))
    per_zone = {z: ((t.get("per_zone_errors") or {}).get(z) or {}).get("recall_non_abstained") for z in zone_ids}
    unmeasured = [z for z, r in per_zone.items() if r is None]
    low_rec = None if (unmeasured or not per_zone) else min(per_zone.values())  # type: ignore[type-var]
    checks.append(_check("min_per_zone_recall", src, ">=", criteria.min_per_zone_recall, low_rec,
                         _ge(low_rec, criteria.min_per_zone_recall),
                         f"per-zone recall (non-abstained): {per_zone}"
                         + (f"; not measured for {unmeasured}" if unmeasured else "")))

    elig = "eligibility"
    # Synthetic data is never evidence, whatever the criteria file says.
    checks.append(_check("no_synthetic_sessions", elig, "==", 0, n_syn, n_syn == 0,
                         "simulated sessions can exercise the software but never enable a model"))
    sigs = list(hardware_signatures)
    one_sig = len(sigs) == 1 and sigs[0] is not None
    checks.append(_check("single_hardware_signature", elig, "==", 1, len(sigs), one_sig,
                         f"hardware signatures: {sigs}"))
    checks.append(_check("room_user_provided", elig, "==", True, room_user_provided, room_user_provided,
                         "zone ids and display anchors must come from a USER_PROVIDED room, not the EXAMPLE room"))
    return checks


def enabled_decision(checks: Sequence[Mapping[str, Any]]) -> tuple[bool, list[str]]:
    """Enabled only if every required check is present and passed."""
    reasons: list[str] = []
    names = {c["name"] for c in checks}
    for required in CRITERIA_FILE_CHECKS + ELIGIBILITY_CHECKS:
        if required not in names:
            reasons.append(f"CRITERION_MISSING: {required} was not evaluated")
    for c in checks:
        if not c.get("passed"):
            code = "SYNTHETIC_DATA_NOT_ALLOWED" if c["name"] in ("no_synthetic_sessions", "allow_synthetic_sessions") \
                else "CRITERION_FAILED"
            reasons.append(f"{code}: {c['name']} measured {c.get('measured')!r} "
                           f"({c.get('comparator')} {c.get('threshold')!r}) {c.get('detail', '')}".rstrip())
    return (not reasons), reasons


def report_supports_enabled(report: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Re-check a stored report before a model is used.

    ``True`` only if the report says ``enabled``, used no synthetic data, and
    lists every required check as passed.
    """
    reasons: list[str] = []
    if not isinstance(report, Mapping):
        return False, ["REPORT_INVALID: report is not an object"]
    if report.get("report_version") != REPORT_VERSION:
        reasons.append(f"REPORT_INVALID: unknown report_version {report.get('report_version')!r}")
    if report.get("enabled") is not True:
        reasons.append("MODEL_NOT_ENABLED: the evaluation report does not enable this model")
    if report.get("synthetic_data_used") is not False:
        reasons.append("SYNTHETIC_DATA_NOT_ALLOWED: the model was trained or evaluated with synthetic sessions")
    checks = report.get("criteria")
    if not isinstance(checks, list) or not all(isinstance(c, Mapping) for c in checks):
        reasons.append("REPORT_INVALID: no criteria list")
    else:
        ok, why = enabled_decision(checks)
        if not ok:
            reasons += why
    return (not reasons), reasons


def zone_ids_from_classes(classes: Sequence[str]) -> list[str]:
    return [c for c in classes if c not in NON_ZONE_LABELS]
