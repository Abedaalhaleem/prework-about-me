"""Tests for the capability registry.

These check state logic and evidence bookkeeping only. No test here touches
hardware, and passing them says nothing about sensing accuracy.
"""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from roomsense.capabilities import (
    DEFAULT_EVIDENCE_PATH,
    SIMULATION_ONLY_REASON,
    UNSUPPORTED_CAPABILITIES,
    CapabilityContext,
    EvidenceError,
    build_capabilities,
    load_verification_evidence,
    verification_for,
)
from roomsense.config import REPO_ROOT
from roomsense.inference.pose import evaluate_pose_gate
from roomsense.schemas import CapabilityId, CapabilityState, PoseStatus, SourceMode, SourceState

TESTS_DIR = Path(__file__).resolve().parent
FLAGS = ("software_tested", "firmware_compiled", "hardware_tested", "through_wall_validated")


@pytest.fixture(scope="module")
def evidence() -> dict[str, Any]:
    return load_verification_evidence()


@pytest.fixture(scope="module")
def pose_status() -> PoseStatus:
    return evaluate_pose_gate(REPO_ROOT / "configs" / "pose_model_manifest.json", {})


def ctx(**kw: Any) -> CapabilityContext:
    base: dict[str, Any] = dict(
        source_mode=None,
        source_state=SourceState.NO_SOURCE,
        live_frames_with_valid_layout=0,
        connected_live_links=0,
        baseline_valid=False,
        calibration_detail=None,
        zone_enabled=False,
        zone_reasons=[],
        pose_status=PoseStatus(),
        through_wall_status="UNVERIFIED",
        evidence={},
    )
    base.update(kw)
    return CapabilityContext(**base)


def by_id(caps) -> dict[CapabilityId, Any]:
    return {c.capability: c for c in caps}


def live_running(**kw: Any) -> CapabilityContext:
    base: dict[str, Any] = dict(
        source_mode=SourceMode.LIVE,
        source_state=SourceState.RUNNING,
        live_frames_with_valid_layout=500,
        connected_live_links=2,
        baseline_valid=True,
    )
    base.update(kw)
    return ctx(**base)


# ---------------------------------------------------------------------------
# Source-mode behaviour
# ---------------------------------------------------------------------------


def test_order_and_titles(evidence):
    caps = build_capabilities(ctx(evidence=evidence))
    assert [c.capability for c in caps] == [
        CapabilityId.A_ACQUISITION,
        CapabilityId.B_MOTION,
        CapabilityId.C_ZONE,
        CapabilityId.D_POSE,
    ]
    assert all(c.title and c.reasons for c in caps)


def test_no_source(evidence, pose_status):
    caps = by_id(build_capabilities(ctx(evidence=evidence, pose_status=pose_status)))
    assert caps[CapabilityId.A_ACQUISITION].state == CapabilityState.HARDWARE_REQUIRED
    assert caps[CapabilityId.B_MOTION].state == CapabilityState.REQUIRES_CALIBRATION
    assert caps[CapabilityId.C_ZONE].state == CapabilityState.DISABLED
    assert caps[CapabilityId.D_POSE].state == CapabilityState.DISABLED
    assert "No source selected" in caps[CapabilityId.A_ACQUISITION].reasons[0]


def test_simulation_is_not_acquisition_and_motion_is_a_software_path(evidence):
    c = ctx(
        source_mode=SourceMode.SIMULATION,
        source_state=SourceState.RUNNING,
        live_frames_with_valid_layout=0,
        connected_live_links=0,
        baseline_valid=True,
        zone_enabled=True,  # even a (hypothetically) enabled zone model...
        evidence=evidence,
    )
    caps = by_id(build_capabilities(c))
    a, b, z = caps[CapabilityId.A_ACQUISITION], caps[CapabilityId.B_MOTION], caps[CapabilityId.C_ZONE]
    assert a.state == CapabilityState.HARDWARE_REQUIRED
    assert any("not live acquisition" in r for r in a.reasons)
    assert b.state == CapabilityState.ENABLED
    assert any(SIMULATION_ONLY_REASON in r for r in b.reasons)
    assert SIMULATION_ONLY_REASON == "simulation only — software path, not a measurement"
    assert z.state == CapabilityState.DISABLED  # ...is never enabled on simulated data
    # Simulation never earns hardware evidence.
    assert b.verification.hardware_tested is False


def test_simulation_without_baseline_still_says_simulation():
    caps = by_id(build_capabilities(ctx(source_mode=SourceMode.SIMULATION, source_state=SourceState.RUNNING)))
    b = caps[CapabilityId.B_MOTION]
    assert b.state == CapabilityState.REQUIRES_CALIBRATION
    assert any(SIMULATION_ONLY_REASON in r for r in b.reasons)


def test_replay_is_not_live_acquisition():
    caps = by_id(
        build_capabilities(
            ctx(source_mode=SourceMode.REPLAY, source_state=SourceState.RUNNING, baseline_valid=True)
        )
    )
    a = caps[CapabilityId.A_ACQUISITION]
    assert a.state == CapabilityState.HARDWARE_REQUIRED
    assert any("Replay" in r and "not live acquisition" in r for r in a.reasons)
    assert caps[CapabilityId.B_MOTION].state == CapabilityState.ENABLED
    assert any("not live" in r for r in caps[CapabilityId.B_MOTION].reasons)


def test_live_with_frames_and_baseline_enables_a_and_b_without_hardware_evidence(evidence):
    caps = by_id(build_capabilities(live_running(evidence=evidence)))
    a, b = caps[CapabilityId.A_ACQUISITION], caps[CapabilityId.B_MOTION]
    assert a.state == CapabilityState.ENABLED
    assert b.state == CapabilityState.ENABLED
    # Running live does not upgrade evidence: only the evidence file does.
    for cap in caps.values():
        assert cap.verification.hardware_tested is False
        assert cap.verification.through_wall_validated is False
    assert any("not a probability" in r for r in b.reasons)
    assert any("UNVERIFIED" in r for r in b.reasons)


def test_hardware_tested_comes_only_from_the_evidence_file(evidence):
    doctored = copy.deepcopy(evidence)
    doctored["capabilities"]["A_ACQUISITION"]["hardware_tested"] = True
    caps = by_id(build_capabilities(live_running(evidence=doctored)))
    assert caps[CapabilityId.A_ACQUISITION].verification.hardware_tested is True
    assert caps[CapabilityId.B_MOTION].verification.hardware_tested is False


def test_live_without_valid_layout_frames_requires_hardware():
    caps = by_id(build_capabilities(live_running(live_frames_with_valid_layout=0)))
    a = caps[CapabilityId.A_ACQUISITION]
    assert a.state == CapabilityState.HARDWARE_REQUIRED
    assert "documented CSI layout" in a.reasons[0]


def test_live_running_without_connected_links_requires_hardware():
    caps = by_id(build_capabilities(live_running(connected_live_links=0)))
    assert caps[CapabilityId.A_ACQUISITION].state == CapabilityState.HARDWARE_REQUIRED


def test_live_disconnected_never_falls_back():
    c = ctx(
        source_mode=SourceMode.LIVE,
        source_state=SourceState.DISCONNECTED,
        live_frames_with_valid_layout=100,
        connected_live_links=0,
        baseline_valid=True,
    )
    caps = by_id(build_capabilities(c))
    a, b = caps[CapabilityId.A_ACQUISITION], caps[CapabilityId.B_MOTION]
    assert a.state == CapabilityState.HARDWARE_REQUIRED
    assert "does not fall back" in a.reasons[0]
    assert b.state == CapabilityState.HARDWARE_REQUIRED
    assert any("SENSOR_OFFLINE" in r for r in b.reasons)


def test_finished_replay_disables_motion():
    c = ctx(source_mode=SourceMode.REPLAY, source_state=SourceState.FINISHED, baseline_valid=True)
    assert by_id(build_capabilities(c))[CapabilityId.B_MOTION].state == CapabilityState.DISABLED


def test_calibration_detail_is_surfaced():
    c = live_running(baseline_valid=False, calibration_detail="baseline rejected: too few windows")
    b = by_id(build_capabilities(c))[CapabilityId.B_MOTION]
    assert b.state == CapabilityState.REQUIRES_CALIBRATION
    assert any("too few windows" in r for r in b.reasons)


# ---------------------------------------------------------------------------
# Zone (C) and pose (D)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, state, baseline, frames",
    list(
        itertools.product(
            [None, SourceMode.LIVE, SourceMode.REPLAY, SourceMode.SIMULATION],
            list(SourceState),
            [False, True],
            [0, 100],
        )
    ),
)
def test_zone_never_enabled_without_zone_enabled(mode, state, baseline, frames):
    c = ctx(
        source_mode=mode,
        source_state=state,
        baseline_valid=baseline,
        live_frames_with_valid_layout=frames,
        connected_live_links=1 if frames else 0,
        zone_enabled=False,
    )
    caps = by_id(build_capabilities(c))
    assert caps[CapabilityId.C_ZONE].state in (CapabilityState.DISABLED, CapabilityState.REQUIRES_VALIDATION)
    # Acquisition is enabled only for a running LIVE source with valid frames.
    a_enabled = caps[CapabilityId.A_ACQUISITION].state == CapabilityState.ENABLED
    assert a_enabled == (mode == SourceMode.LIVE and state == SourceState.RUNNING and frames > 0)


def test_zone_requires_validation_once_baseline_exists():
    c = live_running(zone_reasons=["no model trained"])
    z = by_id(build_capabilities(c))[CapabilityId.C_ZONE]
    assert z.state == CapabilityState.REQUIRES_VALIDATION
    assert "no model trained" in z.reasons


def test_zone_enabled_only_with_validated_model_and_running_source():
    z = by_id(build_capabilities(live_running(zone_enabled=True)))[CapabilityId.C_ZONE]
    assert z.state == CapabilityState.ENABLED
    assert any("EXPERIMENTAL" in r and "display anchors" in r for r in z.reasons)
    stopped = ctx(source_mode=SourceMode.LIVE, source_state=SourceState.DISCONNECTED, zone_enabled=True, baseline_valid=True)
    assert by_id(build_capabilities(stopped))[CapabilityId.C_ZONE].state == CapabilityState.DISABLED


def test_pose_mirrors_the_closed_gate(pose_status):
    assert pose_status.enabled is False
    d = by_id(build_capabilities(live_running(pose_status=pose_status)))[CapabilityId.D_POSE]
    assert d.state == CapabilityState.DISABLED
    assert any(r.startswith("R02_MODEL_INSTALLED") for r in d.reasons)
    assert "MODEL_COMPATIBILITY.md" in d.reasons[0]


# ---------------------------------------------------------------------------
# Verification evidence file
# ---------------------------------------------------------------------------


def test_evidence_file_claims_no_firmware_hardware_or_through_wall_evidence(evidence):
    assert set(evidence["capabilities"]) == {c.value for c in CapabilityId}
    for cap_id, entry in evidence["capabilities"].items():
        assert entry["firmware_compiled"] is False, cap_id
        assert entry["hardware_tested"] is False, cap_id
        assert entry["through_wall_validated"] is False, cap_id
        # Every flag says what evidence would change (or backs) it.
        assert set(entry["would_change"]) == set(FLAGS), cap_id


def test_software_tested_claims_are_backed_by_existing_test_files(evidence):
    # A software_tested=true claim must name test files that actually exist.
    for cap_id, entry in evidence["capabilities"].items():
        if entry["software_tested"]:
            matched = [p for g in entry["software_test_globs"] for p in TESTS_DIR.glob(g)]
            assert matched, f"{cap_id} claims software_tested but no test file matches {entry['software_test_globs']}"


def test_pose_software_tested_claim_is_backed_by_existing_tests(evidence):
    globs = evidence["capabilities"]["D_POSE"]["software_test_globs"]
    matched = {p.name for g in globs for p in TESTS_DIR.glob(g)}
    assert {"test_pose_gate.py", "test_pose_interface.py"} <= matched


def test_verification_notes_explain_each_flag(evidence):
    v = verification_for(evidence, CapabilityId.B_MOTION)
    joined = "\n".join(v.notes)
    for flag in FLAGS:
        assert f"{flag}=" in joined
    assert "synthetic" in joined


def test_missing_or_invalid_evidence_never_yields_true(evidence):
    empty = verification_for({}, CapabilityId.A_ACQUISITION)
    assert not any(getattr(empty, f) for f in FLAGS)
    assert "No verification evidence" in empty.notes[0]

    bad_cases = []
    wrong_schema = copy.deepcopy(evidence)
    wrong_schema["schema"] = "something-else"
    bad_cases.append(wrong_schema)
    string_flag = copy.deepcopy(evidence)
    string_flag["capabilities"]["A_ACQUISITION"]["software_tested"] = "true"  # strings are not booleans
    bad_cases.append(string_flag)
    missing_cap = copy.deepcopy(evidence)
    del missing_cap["capabilities"]["D_POSE"]
    bad_cases.append(missing_cap)
    extra_key = copy.deepcopy(evidence)
    extra_key["capabilities"]["A_ACQUISITION"]["measured_accuracy"] = 0.99
    bad_cases.append(extra_key)
    for bad in bad_cases:
        v = verification_for(bad, CapabilityId.A_ACQUISITION)
        assert not any(getattr(v, f) for f in FLAGS)
        assert "rejected" in v.notes[0]


def test_load_verification_evidence_errors(tmp_path: Path):
    with pytest.raises(EvidenceError):
        load_verification_evidence(tmp_path / "missing.json")
    p = tmp_path / "e.json"
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(EvidenceError):
        load_verification_evidence(p)
    p.write_text(json.dumps({"schema": "roomsense-verification-evidence-v1"}), encoding="utf-8")
    with pytest.raises(EvidenceError):
        load_verification_evidence(p)
    assert load_verification_evidence(DEFAULT_EVIDENCE_PATH)["schema"] == "roomsense-verification-evidence-v1"


# ---------------------------------------------------------------------------
# Unsupported claims
# ---------------------------------------------------------------------------


def test_unsupported_capabilities_cover_required_claims():
    ids = {u["id"] for u in UNSUPPORTED_CAPABILITIES}
    assert {
        "CAMERA_LIKE_IMAGING",
        "OBJECT_RECOGNITION_THROUGH_WALLS",
        "IDENTIFYING_INDIVIDUALS",
        "PEOPLE_COUNTING",
        "RELIABLE_OCCUPANCY_OF_MOTIONLESS_PEOPLE",
        "ANATOMICAL_DETAIL",
        "CONTINUOUS_TRACKING",
        "AOA_TOF_POSITIONING",
    } <= ids
    assert len(ids) == len(UNSUPPORTED_CAPABILITIES)
    for u in UNSUPPORTED_CAPABILITIES:
        assert u["claim"] and u["reason"]
    json.dumps(UNSUPPORTED_CAPABILITIES)  # serialisable for the API/UI


# ---------------------------------------------------------------------------
# Hardening
# ---------------------------------------------------------------------------


def test_software_tested_without_test_files_is_rejected(evidence):
    bad = copy.deepcopy(evidence)
    bad["capabilities"]["A_ACQUISITION"]["software_test_globs"] = []
    v = verification_for(bad, CapabilityId.D_POSE)
    assert not any(getattr(v, f) for f in FLAGS)
    assert "software_test_globs" in v.notes[0]


def test_unknown_would_change_flag_is_rejected(evidence):
    bad = copy.deepcopy(evidence)
    bad["capabilities"]["B_MOTION"]["would_change"]["accuracy_proven"] = "yes"
    v = verification_for(bad, CapabilityId.B_MOTION)
    assert not any(getattr(v, f) for f in FLAGS)


def test_context_accepts_enum_strings_and_rejects_unknown_values():
    c = ctx(source_mode="LIVE", source_state="RUNNING", baseline_valid=True,
            live_frames_with_valid_layout=10, connected_live_links=1)
    assert c.source_mode is SourceMode.LIVE and c.source_state is SourceState.RUNNING
    assert by_id(build_capabilities(c))[CapabilityId.A_ACQUISITION].state == CapabilityState.ENABLED
    with pytest.raises(ValueError):
        ctx(source_mode="CAMERA")
    with pytest.raises(ValueError):
        ctx(source_state="GUESSING")


def test_capability_statuses_serialise_for_the_api(evidence, pose_status):
    caps = build_capabilities(live_running(evidence=evidence, pose_status=pose_status))
    payload = [c.model_dump(mode="json") for c in caps]
    json.dumps(payload)
    assert payload[3]["state"] == "DISABLED"
