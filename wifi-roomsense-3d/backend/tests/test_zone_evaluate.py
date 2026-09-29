"""Zone evaluation metrics, threshold rule and the enablement decision.

Hand-built score arrays only (software tests of the arithmetic).
"""

from __future__ import annotations

import numpy as np
import pytest

from roomsense.inference.zone.criteria import load_criteria
from roomsense.inference.zone.evaluate import (
    ABSTAIN,
    CRITERIA_FILE_CHECKS,
    ELIGIBILITY_CHECKS,
    abstention_sweep,
    balanced_accuracy,
    choose_threshold,
    decide,
    enabled_decision,
    evaluate_criteria,
    evaluate_predictions,
    json_safe,
    report_supports_enabled,
    split_counts,
)

from .zone_helpers import passing_report

CLASSES = ["A", "B", "EMPTY", "OUTSIDE_TARGET_ROOM"]
ZONES = ["A", "B"]


def onehot(labels, conf=0.9):
    s = np.full((len(labels), len(CLASSES)), (1 - conf) / (len(CLASSES) - 1))
    for i, lbl in enumerate(labels):
        s[i, CLASSES.index(lbl)] = conf
    return s


def test_decide_abstains_strictly_below_threshold():
    s = onehot(["A", "B"], conf=0.6)
    pred, abst, top = decide(s, CLASSES, 0.6)
    assert list(pred) == ["A", "B"] and not abst.any()
    _, abst, _ = decide(s, CLASSES, 0.61)
    assert abst.all()
    with pytest.raises(ValueError):
        decide(np.zeros((2, 3)), CLASSES, 0.5)


def test_balanced_accuracy_averages_recall_over_present_classes():
    y = ["A", "A", "A", "A", "B"]
    p = ["A", "A", "A", "A", "A"]
    assert balanced_accuracy(y, p, CLASSES) == pytest.approx(0.5)
    assert balanced_accuracy([], [], CLASSES) is None


def test_confusion_matrix_abstention_and_rates():
    y = ["A"] * 4 + ["B"] * 4 + ["EMPTY"] * 4 + ["OUTSIDE_TARGET_ROOM"] * 4
    predicted = ["A", "A", "A", "B",
                 "B", "B", "B", "B",
                 "EMPTY", "EMPTY", "EMPTY", "A",
                 "OUTSIDE_TARGET_ROOM", "OUTSIDE_TARGET_ROOM", "OUTSIDE_TARGET_ROOM", "OUTSIDE_TARGET_ROOM"]
    s = onehot(predicted)
    s[7] = 0.25  # a B window with a flat score: abstains at t=0.5
    m = evaluate_predictions(y, s, CLASSES, ZONES, 0.5,
                             session_ids=["s1"] * 8 + ["s2"] * 8,
                             session_meta={"s1": {"label": "mix", "start_unix_ns": 2}, "s2": {"start_unix_ns": 1}})
    cm = m["confusion_matrix"]
    assert cm["columns_predicted"] == CLASSES + [ABSTAIN]
    assert cm["counts"][0] == [3, 1, 0, 0, 0]
    assert cm["counts"][1] == [0, 3, 0, 0, 1]
    assert cm["counts"][2] == [1, 0, 3, 0, 0]
    assert m["abstained"] == 1 and m["abstention_rate"] == pytest.approx(1 / 16)
    assert m["answered"] == 15 and m["correct"] == 13
    assert m["accuracy_non_abstained"] == pytest.approx(13 / 15)
    assert m["per_zone_errors"]["A"] == {"recall_non_abstained": 0.75, "answered": 4, "errors": {"B": 1},
                                         "error_count": 1}
    assert m["per_zone_errors"]["B"]["recall_non_abstained"] == 1.0
    assert m["empty_predicted_as_zone"]["rate_over_all_windows"] == pytest.approx(0.25)
    assert m["outside_predicted_as_zone"]["criterion_value"] == 0.0
    w = m["accuracy_wilson_95"]
    assert w["trials"] == 15 and 0 < w["low"] < 13 / 15 < w["high"] <= 1
    # Later-session view is sorted by start time.
    assert [r["session_key"] for r in m["per_session"]] == ["s2", "s1"]
    assert m["per_session"][1]["abstained"] == 1


def test_empty_as_zone_uses_the_stricter_of_the_two_rates():
    y = ["EMPTY"] * 4
    s = onehot(["A", "EMPTY", "EMPTY", "EMPTY"])
    s[1:] = 0.25  # three EMPTY windows abstain
    m = evaluate_predictions(y, s, CLASSES, ZONES, 0.5)
    e = m["empty_predicted_as_zone"]
    assert e["rate_over_all_windows"] == 0.25
    assert e["rate_over_non_abstained"] == 1.0
    assert e["criterion_value"] == 1.0  # abstaining cannot hide the false zone


def test_threshold_rule_smallest_threshold_with_best_balanced_accuracy():
    sweep = [
        {"threshold": 0.0, "abstention_rate": 0.0, "balanced_accuracy_non_abstained": 0.80},
        {"threshold": 0.3, "abstention_rate": 0.05, "balanced_accuracy_non_abstained": 0.90},
        {"threshold": 0.4, "abstention_rate": 0.10, "balanced_accuracy_non_abstained": 0.90},
        {"threshold": 0.5, "abstention_rate": 0.40, "balanced_accuracy_non_abstained": 0.99},
    ]
    t, rule = choose_threshold(sweep, 0.30)
    assert t == 0.3 and "smallest" in rule
    t, rule = choose_threshold(sweep[3:], 0.30)
    assert t is None and rule.startswith("NO_ELIGIBLE_THRESHOLD")


def test_sweep_matches_decide():
    y = ["A", "B", "EMPTY", "A"]
    s = onehot(["A", "B", "EMPTY", "B"], conf=0.7)
    s[3] = [0.45, 0.55, 0.0, 0.0]
    sweep = abstention_sweep(s, y, CLASSES, [0.0, 0.6])
    assert sweep[0]["abstained"] == 0 and sweep[0]["balanced_accuracy_non_abstained"] == pytest.approx((0.5 + 1 + 1) / 3)
    assert sweep[1]["abstained"] == 1 and sweep[1]["balanced_accuracy_non_abstained"] == 1.0


def test_split_counts_by_class_and_session():
    labels = ["A", "A", "B", "EMPTY", "A"]
    sids = ["s1", "s1", "s2", "s3", "s4"]
    c = split_counts(labels, sids, {"s1": "train", "s2": "train", "s3": "test", "s4": "test"}, CLASSES,
                     ["train", "validation", "test"])
    assert c["train"]["windows"]["A"] == 2 and c["train"]["sessions_with_windows"]["A"] == 1
    assert c["test"]["windows"] == {"A": 1, "B": 0, "EMPTY": 1, "OUTSIDE_TARGET_ROOM": 0}
    assert c["test"]["class_balance"]["A"] == 0.5
    assert c["validation"]["total_windows"] == 0 and c["validation"]["class_balance"]["A"] is None


def _criteria_kwargs(**over):
    kw = dict(
        zone_ids=ZONES, classes=CLASSES,
        sessions_per_split={"train": {c: 3 for c in CLASSES}, "validation": {c: 1 for c in CLASSES},
                            "test": {c: 1 for c in CLASSES}},
        test_windows_per_class={c: 40 for c in CLASSES},
        test_after_train_and_validation=True, synthetic_sessions=[], receivers=["rx1", "rx2", "rx3"],
        min_receivers=3, hardware_signatures=["hw1"], room_user_provided=True,
        test={
            "balanced_accuracy_non_abstained": 0.95, "accuracy_wilson_95": {"low": 0.9},
            "abstention_rate": 0.1, "empty_predicted_as_zone": {"criterion_value": 0.0},
            "outside_predicted_as_zone": {"criterion_value": 0.02},
            "per_zone_errors": {"A": {"recall_non_abstained": 0.9}, "B": {"recall_non_abstained": 0.8}},
        },
    )
    kw.update(over)
    return kw


def test_all_criteria_pass_only_with_everything_measured():
    crit = load_criteria()
    checks = evaluate_criteria(crit, **_criteria_kwargs())
    assert {c["name"] for c in checks} == set(CRITERIA_FILE_CHECKS + ELIGIBILITY_CHECKS)
    ok, reasons = enabled_decision(checks)
    assert ok and reasons == []


@pytest.mark.parametrize(
    "override,failing",
    [
        ({"synthetic_sessions": ["syn_1"]}, "no_synthetic_sessions"),
        ({"receivers": ["rx1", "rx2"]}, "min_receivers"),
        ({"hardware_signatures": ["hw1", "hw2"]}, "single_hardware_signature"),
        ({"room_user_provided": False}, "room_user_provided"),
        ({"test_after_train_and_validation": False}, "require_test_after_train_and_validation"),
        ({"test_after_train_and_validation": None}, "require_test_after_train_and_validation"),
        ({"test_windows_per_class": {"A": 29, "B": 40, "EMPTY": 40, "OUTSIDE_TARGET_ROOM": 40}},
         "min_test_windows_per_class"),
        ({"test": {}}, "min_test_balanced_accuracy_non_abstained"),
        ({"zone_ids": ["A"]}, "min_zones"),
    ],
)
def test_any_failed_or_unmeasured_criterion_disables(override, failing):
    checks = evaluate_criteria(load_criteria(), **_criteria_kwargs(**override))
    failed = {c["name"] for c in checks if not c["passed"]}
    assert failing in failed
    ok, reasons = enabled_decision(checks)
    assert not ok and reasons


def test_unmeasured_per_zone_recall_fails_not_passes():
    kw = _criteria_kwargs()
    kw["test"]["per_zone_errors"] = {"A": {"recall_non_abstained": 0.9}, "B": {"recall_non_abstained": None}}
    checks = evaluate_criteria(load_criteria(), **kw)
    c = next(c for c in checks if c["name"] == "min_per_zone_recall")
    assert c["passed"] is False and c["measured"] is None and c["detail"].startswith("NOT MEASURED")


def test_enabled_decision_requires_every_check():
    checks = [{"name": "min_zones", "passed": True}]
    ok, reasons = enabled_decision(checks)
    assert not ok and any(r.startswith("CRITERION_MISSING") for r in reasons)


def test_report_supports_enabled():
    ok, why = report_supports_enabled(passing_report())
    assert ok and why == []
    r = passing_report()
    r["synthetic_data_used"] = True
    assert report_supports_enabled(r)[0] is False
    r = passing_report()
    r["criteria"][3]["passed"] = False
    assert report_supports_enabled(r)[0] is False
    r = passing_report()
    r["enabled"] = False
    assert report_supports_enabled(r)[0] is False
    r = passing_report()
    del r["criteria"]
    assert report_supports_enabled(r)[0] is False
    assert report_supports_enabled("nope")[0] is False  # type: ignore[arg-type]


def test_json_safe_drops_nan_and_numpy_types():
    out = json_safe({"a": np.float64("nan"), "b": np.int64(3), "c": np.array([1.5, np.inf]), "d": (True,)})
    assert out == {"a": None, "b": 3, "c": [1.5, None], "d": [True]}
