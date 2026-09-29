"""Zone training: session-level splits, train-only fitting, validation-only
threshold tuning, a single test evaluation, and "synthetic never enables".

SOFTWARE TESTS ONLY on SIMULATED sessions. The toy simulator's zones are easy
to separate, so good scores here say nothing about a real room; that is
exactly why a model trained on them must stay disabled.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from roomsense.config import AppConfig
from roomsense.inference.zone.criteria import default_criteria_path, load_criteria
from roomsense.inference.zone.dataset import SessionSummary
from roomsense.inference.zone.evaluate import abstention_sweep, choose_threshold
from roomsense.inference.zone.predictor import ZonePredictor
from roomsense.inference.zone.registry import ZoneModelRegistry
from roomsense.inference.zone.train import (
    SplitPlan,
    TrainingRefused,
    _scores_in_order,
    plan_splits,
    run_training,
    train_and_evaluate,
)
from roomsense.schemas import GeometryProvenance, ZoneState
from roomsense.storage.db import Database

from .zone_helpers import LABELS, ZONE_IDS, cached_dataset, fake_provenance, features_for, make_room, synthetic_specs

CRIT = load_criteria()


@pytest.fixture(scope="module")
def trained():
    return train_and_evaluate(cached_dataset(), CRIT, room=make_room())


def _summary(key: str, label: str, start: int | None, split: str | None = None) -> SessionSummary:
    return SessionSummary(session_key=key, label=label, requested_split=split, recording_id=None, synthetic=False,
                          synthetic_reasons=[], start_unix_ns=start, end_unix_ns=None if start is None else start + 10,
                          source_modes=["LIVE"], hardware_signature="hw", link_ids_seen=[], layout_ids=[])


# --------------------------------------------------------------------- splits


def test_automatic_splits_are_chronological_per_class():
    sessions = [_summary(f"{c}{i}", c, start=1000 * i + j) for j, c in enumerate(LABELS) for i in (4, 2, 0, 3, 1)]
    plan = plan_splits(sessions, LABELS, CRIT)
    assert plan.method == "chronological_per_class"
    for c in LABELS:
        assert [plan.assignment[f"{c}{i}"] for i in range(5)] == ["train"] * 3 + ["validation", "test"]


def test_more_sessions_give_larger_holdouts_but_keep_order():
    sessions = [_summary(f"A{i}", "A", start=i) for i in range(10)]
    plan = plan_splits(sessions, ["A"], CRIT)
    assert [plan.assignment[f"A{i}"] for i in range(10)] == ["train"] * 6 + ["validation"] * 2 + ["test"] * 2


def test_too_few_sessions_are_refused():
    sessions = [_summary(f"A{i}", "A", start=i) for i in range(4)] + [_summary("B0", "B", start=9)]
    with pytest.raises(TrainingRefused) as exc:
        plan_splits(sessions, ["A", "B"], CRIT)
    assert exc.value.code == "INSUFFICIENT_SESSIONS" and "'A': 4" in exc.value.detail


def test_user_splits_are_all_or_nothing_and_used_as_given():
    sessions = [_summary("a", "A", 1, "test"), _summary("b", "A", 2, "train")]
    assert plan_splits(sessions, ["A"], CRIT) == SplitPlan({"a": "test", "b": "train"}, "user")
    with pytest.raises(TrainingRefused, match="PARTIAL_SPLITS"):
        plan_splits([_summary("a", "A", 1, "test"), _summary("b", "A", 2)], ["A"], CRIT)


def test_sessions_without_start_time_cannot_be_split_chronologically():
    with pytest.raises(TrainingRefused, match="SESSION_START_UNKNOWN"):
        plan_splits([_summary("a", "A", None)] * 5, ["A"], CRIT)


def test_no_leakage_between_splits(trained):
    ds = cached_dataset()
    masks = trained.split_masks
    total = np.zeros(ds.y.size, dtype=int)
    for m in masks.values():
        total += m.astype(int)
    assert np.all(total == 1), "every window is in exactly one split"
    for key in set(ds.session_ids.tolist()):
        in_session = ds.session_ids == key
        splits = [sp for sp, m in masks.items() if np.any(m & in_session)]
        assert len(splits) == 1, f"windows of session {key} appear in {splits}"
    split_sessions = {sp: set(ds.session_ids[m].tolist()) for sp, m in masks.items()}
    assert not split_sessions["train"] & split_sessions["validation"]
    assert not split_sessions["train"] & split_sessions["test"]
    assert not split_sessions["validation"] & split_sessions["test"]
    leak = trained.report["splits"]["leakage_check"]
    assert leak["unit"] == "session" and leak["no_session_in_two_splits"] is True


def test_scaler_is_fitted_on_train_windows_only(trained):
    ds = cached_dataset()
    scaler = trained.pipeline.named_steps["scaler"]
    train = ds.X[trained.split_masks["train"]]
    np.testing.assert_allclose(scaler.mean_, train.mean(axis=0), rtol=1e-10, atol=1e-12)
    np.testing.assert_allclose(scaler.scale_, train.std(axis=0), rtol=1e-10, atol=1e-12)
    assert scaler.n_samples_seen_ == train.shape[0] < ds.X.shape[0]
    assert not np.allclose(scaler.mean_, ds.X.mean(axis=0))
    assert trained.report["model"]["fitted_on"] == "train"


def test_threshold_is_tuned_on_validation_windows(trained):
    ds = cached_dataset()
    val = trained.split_masks["validation"]
    tuning = trained.report["threshold_tuning"]
    assert tuning["split"] == "validation" and tuning["n_windows"] == int(val.sum())
    scores = _scores_in_order(trained.pipeline, ds.X[val], trained.classes)
    sweep = abstention_sweep(scores, ds.y[val], trained.classes, CRIT.threshold_grid)
    expected, _ = choose_threshold(sweep, CRIT.max_validation_abstention_rate)
    assert trained.threshold == expected == tuning["chosen_threshold"]
    assert tuning["sweep"] == sweep
    assert trained.report["test_evaluations"] == 1


def test_test_windows_influence_neither_the_model_nor_the_threshold(trained):
    ds = cached_dataset()
    test = trained.split_masks["test"]
    rng = np.random.default_rng(3)
    X = ds.X.copy()
    X[test] = rng.normal(0.0, 5.0, size=X[test].shape)  # replace every test window with noise
    tampered = dataclasses.replace(ds, X=X)
    again = train_and_evaluate(tampered, CRIT, room=make_room())
    clf_a = trained.pipeline.named_steps["classifier"]
    clf_b = again.pipeline.named_steps["classifier"]
    np.testing.assert_array_equal(clf_a.coef_, clf_b.coef_)
    np.testing.assert_array_equal(clf_a.intercept_, clf_b.intercept_)
    assert again.threshold == trained.threshold
    assert again.report["threshold_tuning"] == trained.report["threshold_tuning"]
    assert again.report["test"] != trained.report["test"]


# ------------------------------------------------------------ synthetic data


def test_simulated_sessions_train_end_to_end_but_never_enable(trained):
    r = trained.report
    assert trained.synthetic_data_used is True
    assert trained.enabled is False and r["enabled"] is False
    failed = {c["name"] for c in r["criteria"] if not c["passed"]}
    assert {"allow_synthetic_sessions", "no_synthetic_sessions"} <= failed
    assert any(x.startswith("SYNTHETIC_DATA_NOT_ALLOWED") for x in r["enabled_reasons"])
    # The report has everything the evaluation promised.
    t = r["test"]
    assert t["confusion_matrix"]["rows_true"] == list(LABELS)
    assert set(t["per_zone_errors"]) == set(ZONE_IDS)
    for key in ("abstention_rate", "balanced_accuracy_non_abstained", "accuracy_wilson_95",
                "empty_predicted_as_zone", "outside_predicted_as_zone", "per_session"):
        assert key in t
    assert [s["start_unix_ns"] for s in t["per_session"]] == sorted(s["start_unix_ns"] for s in t["per_session"])
    assert r["criteria_version"] == CRIT.criteria_version
    assert set(r["counts"]) == {"train", "validation", "test"}
    assert r["binding"]["link_order"] == list(trained.link_order)
    assert "not calibrated probabilities" in r["scores_note"]


def test_synthetic_model_is_stored_disabled_and_predictor_refuses_it(tmp_path, trained):
    db = Database(tmp_path / "meta.sqlite3")
    try:
        reg = ZoneModelRegistry(tmp_path / "data", db=db)
        b = reg.save(trained.pipeline, hardware_signature=trained.hardware_signature,
                     room_config_hash=trained.room_config_hash, config_version=trained.config_version,
                     criteria_version=trained.criteria_version, link_order=trained.link_order,
                     feature_names=trained.feature_names, feature_set_version=trained.feature_set_version,
                     threshold=trained.threshold, classes=trained.classes, zone_ids=trained.zone_ids,
                     synthetic_data_used=trained.synthetic_data_used, report=trained.report)
        assert b.enabled is False and b.synthetic_data_used is True
        row = db.get_zone_model(b.model_id)
        assert row is not None and row.enabled is False and row.synthetic_data_used is True
        pred = ZonePredictor(reg, AppConfig(), room=make_room()).predict(
            features_for("A"), {}, trained.room_config_hash, trained.hardware_signature, trained.config_version,
            trained.criteria_version, fake_provenance())
        assert pred.state == ZoneState.DISABLED and pred.zone_id is None
        assert pred.reasons[0].startswith("NO_ENABLED_MODEL")
    finally:
        db.close()


def test_example_room_is_not_eligible():
    t = train_and_evaluate(cached_dataset(), CRIT, room=make_room(GeometryProvenance.EXAMPLE))
    failed = {c["name"] for c in t.report["criteria"] if not c["passed"]}
    assert "room_user_provided" in failed and t.enabled is False


def test_labels_must_be_room_zones():
    with pytest.raises(TrainingRefused) as exc:
        train_and_evaluate(cached_dataset(), CRIT, room=make_room(zones=("A", "B")))
    assert exc.value.code == "UNKNOWN_LABEL"


def test_test_sessions_recorded_before_training_fail_the_ordering_criterion():
    ds = cached_dataset()
    # Put the earliest round in test and the latest in training.
    assign = {s.session_key: ("test" if "_r0_" in s.session_key else "train" if "_r4_" in s.session_key
                              else "validation" if "_r3_" in s.session_key else "train") for s in ds.sessions}
    t = train_and_evaluate(ds, CRIT, room=make_room(), plan=SplitPlan(assign, "user"))
    check = next(c for c in t.report["criteria"] if c["name"] == "require_test_after_train_and_validation")
    assert check["measured"] is False and check["passed"] is False


def test_run_training_wires_criteria_dataset_training_and_registry(tmp_path):
    # A lowered-minimum COPY of the criteria file, only to keep this wiring test
    # small. It gets its own criteria_version, and the synthetic data disables
    # the model anyway.
    text = default_criteria_path().read_text("utf-8")
    text = text.replace("min_sessions_per_class_train = 3", "min_sessions_per_class_train = 1")
    crit_path = tmp_path / "criteria_copy.toml"
    crit_path.write_text(text, "utf-8")
    labels = ("A", "B", "EMPTY", "OUTSIDE_TARGET_ROOM")
    specs = synthetic_specs(rounds=(0, 1, 2), labels=labels, duration_s=18.0, key_prefix="wire")
    reg = ZoneModelRegistry(tmp_path / "data")
    trained, binding = run_training(specs, AppConfig(), make_room(zones=("A", "B")), data_dir=tmp_path / "data",
                                    criteria_path=crit_path, registry=reg)
    assert binding is not None and binding.enabled is False
    assert binding.criteria_version == load_criteria(crit_path).criteria_version != CRIT.criteria_version
    assert trained.report["splits"]["method"] == "chronological_per_class"
    assert reg.load(binding.model_id).binding.model_id == binding.model_id
