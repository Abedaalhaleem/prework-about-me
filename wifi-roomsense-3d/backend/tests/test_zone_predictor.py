"""Zone predictor gating: when it must be DISABLED, ABSTAIN, or may ESTIMATE.

The "enabled" models here are HAND-BUILT FAKES (random-number training data,
hand-written passing report) stored in a temporary directory. They exist only
to prove the gates work: a real model can only be enabled by passing the
predefined criteria on held-out real sessions.
"""

from __future__ import annotations

import itertools

import pytest

from roomsense.acquisition.base import FrameEvent
from roomsense.config import AppConfig, ProcessingConfig
from roomsense.inference.zone.criteria import default_criteria_path
from roomsense.inference.zone.predictor import ZonePredictor, runtime_inputs
from roomsense.inference.zone.registry import ZoneModelRegistry
from roomsense.processing.pipeline import ProcessingEngine
from roomsense.schemas import (
    ActivityState,
    GeometryProvenance,
    QualityLevel,
    SourceMode,
    Vec2,
    Vec3,
    ZoneState,
)

from .zone_helpers import (
    CRITERIA_VERSION,
    LABELS,
    LINK_ORDER,
    T_END_NS,
    fake_provenance,
    features_for,
    make_fv,
    make_room,
    passing_report,
    save_fake_model,
    synthetic_spec,
)

HW = "hw-fake-0001"
CFG = AppConfig()
ROOM = make_room()
READY = {lid: ActivityState.MOTION_DETECTED for lid in LINK_ORDER}


@pytest.fixture
def reg(tmp_path):
    return ZoneModelRegistry(tmp_path / "data")


def predictor(reg, **kw) -> ZonePredictor:
    kw.setdefault("room", ROOM)
    return ZonePredictor(reg, CFG, **kw)


def call(p: ZonePredictor, label: str = "A", *, fvs=None, states=None, room_hash=None, hw=HW, config=None,
         criteria=CRITERIA_VERSION, provenance="default", **kw):
    return p.predict(
        features_for(label) if fvs is None else fvs,
        READY if states is None else states,
        ROOM.config_hash() if room_hash is None else room_hash,
        hw,
        CFG.config_version() if config is None else config,
        criteria,
        fake_provenance() if provenance == "default" else provenance,
        **kw,
    )


def code(pred) -> str:
    return pred.reasons[0].split(":", 1)[0]


# ------------------------------------------------------------------ DISABLED


def test_no_model_means_disabled(reg):
    pred = call(predictor(reg))
    assert pred.state == ZoneState.DISABLED and pred.zone_id is None and pred.display_anchor is None
    assert code(pred) == "NO_ENABLED_MODEL"


def test_model_whose_report_does_not_pass_is_disabled(reg):
    failing = passing_report()
    failing["criteria"][-1]["passed"] = False
    save_fake_model(reg, report=failing)
    pred = call(predictor(reg))
    assert pred.state == ZoneState.DISABLED
    assert code(pred) == "NO_ENABLED_MODEL" and any(r.startswith("MODEL_NOT_ENABLED") for r in pred.reasons)
    pinned = save_fake_model(reg, report=failing)
    pred = call(predictor(reg, model_id=pinned.model_id))
    assert pred.state == ZoneState.DISABLED and code(pred) == "MODEL_NOT_ENABLED"


def test_enabled_fake_model_estimates_with_a_display_anchor_only(reg):
    b = save_fake_model(reg)
    pred = call(predictor(reg), "B")
    assert pred.state == ZoneState.ESTIMATE, pred.reasons
    assert pred.zone_id == "B" and pred.zone_label == "Zone B"
    zone_b = next(z for z in ROOM.zones if z.id == "B")
    assert pred.display_anchor == zone_b.display_anchor()
    assert isinstance(pred.display_anchor, Vec2) and not isinstance(pred.display_anchor, Vec3)
    assert "display anchor" in pred.display_anchor_note
    assert set(pred.model_scores) == set(LABELS)
    assert abs(sum(pred.model_scores.values()) - 1.0) < 1e-9
    assert pred.model_id == b.model_id and pred.criteria_version == CRITERIA_VERSION
    assert pred.provenance is not None and pred.provenance.model_version == b.model_id
    assert pred.provenance.link_ids == list(LINK_ORDER)
    assert any("not probabilities" in r for r in pred.reasons)


def test_anchor_is_the_zone_centroid_not_a_receiver_position(reg):
    save_fake_model(reg)
    pred = call(predictor(reg), "C")
    assert pred.state == ZoneState.ESTIMATE
    # Zone C spans x in [8/3, 4], y in [0, 3.5]; its centroid is not any node position.
    assert pred.display_anchor is not None
    assert (pred.display_anchor.x, pred.display_anchor.y) == pytest.approx(((8 / 3 + 4) / 2, 1.75))
    node_xy = {(n.position.x, n.position.y) for n in ROOM.nodes}
    assert (pred.display_anchor.x, pred.display_anchor.y) not in node_xy


def test_criteria_change_disables(reg, tmp_path):
    save_fake_model(reg)
    pred = call(predictor(reg), criteria="0123456789abcdef")
    assert pred.state == ZoneState.DISABLED and code(pred) == "CRITERIA_CHANGED"
    # Tampering with the criteria FILE changes its version and disables too.
    tampered = tmp_path / "zone_enablement.toml"
    tampered.write_text(default_criteria_path().read_text("utf-8").replace(
        "max_test_abstention_rate = 0.30", "max_test_abstention_rate = 0.90"), "utf-8")
    pred = call(predictor(reg, criteria_path=tampered), criteria=None)
    assert pred.state == ZoneState.DISABLED and code(pred) == "CRITERIA_CHANGED"
    # The untouched file keeps it enabled.
    assert call(predictor(reg), criteria=None).state == ZoneState.ESTIMATE
    broken = tmp_path / "broken.toml"
    broken.write_text("not = = toml", "utf-8")
    pred = call(predictor(reg, criteria_path=broken), criteria=None)
    assert pred.state == ZoneState.DISABLED and code(pred) == "CRITERIA_UNAVAILABLE"


def test_hardware_signature_change_disables(reg):
    save_fake_model(reg)
    pred = call(predictor(reg), hw="hw-other")
    assert pred.state == ZoneState.DISABLED and code(pred) == "HARDWARE_SIGNATURE_MISMATCH"
    pred = call(predictor(reg), hw=None)
    assert pred.state == ZoneState.DISABLED and code(pred) == "HARDWARE_SIGNATURE_UNAVAILABLE"


def test_room_change_disables(reg):
    save_fake_model(reg)
    moved = ROOM.model_copy(deep=True)
    moved.nodes[1].position = Vec3(x=3.9, y=0.6, z=1.0)  # a receiver was moved
    assert moved.config_hash() != ROOM.config_hash()
    pred = call(predictor(reg, room=moved), room_hash=moved.config_hash())
    assert pred.state == ZoneState.DISABLED and code(pred) == "ROOM_CHANGED"
    example = make_room(GeometryProvenance.EXAMPLE)
    pred = call(predictor(reg, room=example), room_hash=example.config_hash())
    assert pred.state == ZoneState.DISABLED and code(pred) == "ROOM_CHANGED"


def test_config_change_disables(reg):
    save_fake_model(reg)
    other = AppConfig(processing=ProcessingConfig(window_s=3.0)).config_version()
    pred = call(predictor(reg), config=other)
    assert pred.state == ZoneState.DISABLED and code(pred) == "CONFIG_CHANGED"


def test_wrong_model_shape_disables(reg):
    save_fake_model(reg, n_features=12)  # deliberately wrong-shaped pipeline
    pred = call(predictor(reg))
    assert pred.state == ZoneState.DISABLED and code(pred) == "MODEL_INPUT_SHAPE_MISMATCH"


def test_feature_layout_change_at_runtime_disables(reg):
    save_fake_model(reg)
    fvs = features_for("A")
    fvs[LINK_ORDER[0]] = make_fv(LINK_ORDER[0], 0.3, names=("new_feature", "other"))
    pred = call(predictor(reg), fvs=fvs)
    assert pred.state == ZoneState.DISABLED and code(pred) == "MODEL_INPUT_SHAPE_MISMATCH"


def test_tampered_model_file_disables(reg):
    b = save_fake_model(reg)
    p = predictor(reg)
    pm = reg.models_dir / f"{b.model_id}.joblib"
    pm.write_bytes(pm.read_bytes() + b"tampered")
    pred = call(p)
    assert pred.state == ZoneState.DISABLED and code(pred) == "MODEL_FILE_INVALID"


def test_simulated_source_disables(reg):
    save_fake_model(reg)
    pred = call(predictor(reg), provenance=fake_provenance(SourceMode.SIMULATION))
    assert pred.state == ZoneState.DISABLED and code(pred) == "SIMULATED_SOURCE"


def test_missing_room_geometry_disables(reg):
    save_fake_model(reg)
    pred = call(ZonePredictor(reg, CFG))
    assert pred.state == ZoneState.DISABLED and code(pred) == "ROOM_UNAVAILABLE"


def test_newest_matching_enabled_model_is_used(reg):
    other = save_fake_model(reg, hardware_signature="hw-elsewhere", created_at_unix_ns=2)
    mine = save_fake_model(reg, created_at_unix_ns=1)
    p = predictor(reg)
    assert call(p).model_id == mine.model_id
    pred = call(p, hw="hw-elsewhere")
    assert pred.model_id == other.model_id and pred.state == ZoneState.ESTIMATE


# ------------------------------------------------------------------- ABSTAIN


def test_missing_receiver_abstains(reg):
    save_fake_model(reg)
    pred = call(predictor(reg), fvs=features_for("A", links=LINK_ORDER[:2]))
    assert pred.state == ZoneState.ABSTAIN and pred.zone_id is None
    assert code(pred) == "LINK_MISSING" and LINK_ORDER[2] in pred.reasons[0]


@pytest.mark.parametrize("state", [ActivityState.UNKNOWN, ActivityState.SENSOR_OFFLINE, ActivityState.CALIBRATING])
def test_link_not_ready_abstains(reg, state):
    save_fake_model(reg)
    pred = call(predictor(reg), states={**READY, LINK_ORDER[1]: state})
    assert pred.state == ZoneState.ABSTAIN and code(pred) == "LINK_NOT_READY"
    assert state.value in pred.reasons[0]


def test_missing_link_state_abstains(reg):
    save_fake_model(reg)
    states = {k: v for k, v in READY.items() if k != LINK_ORDER[0]}
    pred = call(predictor(reg), states=states)
    assert pred.state == ZoneState.ABSTAIN and code(pred) == "LINK_STATE_UNKNOWN"


def test_no_motion_links_can_still_be_answered(reg):
    save_fake_model(reg)
    states = {lid: ActivityState.NO_MOTION_DETECTED for lid in LINK_ORDER}
    assert call(predictor(reg), states=states).state == ZoneState.ESTIMATE


def test_stale_window_abstains(reg):
    save_fake_model(reg)
    p = predictor(reg)
    assert call(p, now_ns=T_END_NS + 1_000_000_000).state == ZoneState.ESTIMATE
    pred = call(p, now_ns=T_END_NS + int((CFG.acquisition.stale_after_s + 1) * 1e9))
    assert pred.state == ZoneState.ABSTAIN and code(pred) == "LINK_STALE"


def test_misaligned_links_abstain(reg):
    save_fake_model(reg)
    fvs = features_for("A", skew_ns={LINK_ORDER[2]: int((CFG.acquisition.alignment_tolerance_s + 0.2) * 1e9)})
    pred = call(predictor(reg), fvs=fvs)
    assert pred.state == ZoneState.ABSTAIN and code(pred) == "LINKS_NOT_ALIGNED"


def test_low_quality_abstains(reg):
    save_fake_model(reg)
    q = {lid: QualityLevel.GOOD for lid in LINK_ORDER}
    assert call(predictor(reg), link_quality=q).state == ZoneState.ESTIMATE
    pred = call(predictor(reg), link_quality={**q, LINK_ORDER[0]: QualityLevel.BAD})
    assert pred.state == ZoneState.ABSTAIN and code(pred) == "LOW_QUALITY"


def test_no_target_zone_predicted_abstains(reg):
    save_fake_model(reg)
    for label in ("EMPTY", "OUTSIDE_TARGET_ROOM"):
        pred = call(predictor(reg), label)
        assert pred.state == ZoneState.ABSTAIN and code(pred) == "NO_TARGET_ZONE_PREDICTED"
        assert max(pred.model_scores, key=pred.model_scores.get) == label


def test_score_below_frozen_threshold_abstains(reg):
    save_fake_model(reg, threshold=0.9)
    # Between the fake A and B patterns: the model is unsure.
    fvs = {LINK_ORDER[0]: make_fv(LINK_ORDER[0], 0.08), LINK_ORDER[1]: make_fv(LINK_ORDER[1], 0.08),
           LINK_ORDER[2]: make_fv(LINK_ORDER[2], 0.02)}
    pred = call(predictor(reg), fvs=fvs)
    assert pred.state == ZoneState.ABSTAIN and code(pred) == "LOW_MODEL_SCORE"
    assert max(pred.model_scores.values()) < 0.9


def test_missing_provenance_abstains(reg):
    save_fake_model(reg)
    pred = call(predictor(reg), provenance=None)
    assert pred.state == ZoneState.ABSTAIN and any(r.startswith("NO_PROVENANCE") for r in pred.reasons)


# ----------------------------------------------------------------- invariants


def test_never_estimates_without_an_enabled_model(reg):
    failing = passing_report()
    failing["enabled"] = False
    save_fake_model(reg, report=failing)
    save_fake_model(reg, synthetic_data_used=True)
    p = predictor(reg)
    for label, state in itertools.product(LABELS, list(ActivityState)):
        pred = call(p, label, states={lid: state for lid in LINK_ORDER})
        assert pred.state == ZoneState.DISABLED and pred.zone_id is None


def test_status_reports_ready_or_disabled(reg):
    kw = dict(room_hash=ROOM.config_hash(), hardware_signature=HW, config_version=CFG.config_version())
    p = predictor(reg)
    assert p.status(**kw)["state"] == "DISABLED"
    b = save_fake_model(reg)
    p.refresh()
    st = p.status(**kw)
    assert st["state"] == "ENABLED" and st["model_id"] == b.model_id and st["criteria_version"] == CRITERIA_VERSION
    assert st["criteria"]["criteria_version"] == CRITERIA_VERSION and st["report"]["enabled"] is True
    assert p.status(**{**kw, "hardware_signature": "hw-other"})["state"] == "DISABLED"


def test_runtime_inputs_pairs_features_with_the_same_window():
    engine = ProcessingEngine(CFG)
    spec = synthetic_spec("A", 0)
    engine.reset("syn_r0_A", SourceMode.SIMULATION)
    step_every = int(CFG.processing.hop_s * 1e9)
    next_step = None
    for f in spec.frames():  # type: ignore[misc,union-attr]
        engine.on_event(FrameEvent(f))
        t = f.host_arrival_monotonic_ns
        if next_step is None or t >= next_step:
            engine.step(now_ns=t)
            next_step = t + step_every
    feats, states, quality = runtime_inputs(engine, LINK_ORDER)
    assert set(feats) == set(LINK_ORDER)
    assert set(runtime_inputs(engine)[0]) == set(LINK_ORDER)  # default: every engine link
    latest = engine.latest()
    for lid, fv in feats.items():
        assert fv.t_unix_end_ns == latest[lid].provenance.window_end_unix_ns
        # No baseline was recorded, so the detector cannot decide: the
        # predictor would abstain (and on simulated data it is disabled anyway).
        assert states[lid] in (ActivityState.UNKNOWN, ActivityState.CALIBRATING)
        assert quality[lid] in set(QualityLevel)
