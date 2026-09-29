"""The through-wall protocol is complete, consistent and JSON-ready."""

from __future__ import annotations

import json

from roomsense.storage.models import ValidationRun
from roomsense.schemas import SourceMode
from roomsense.validation.protocol import (
    REQUIRED_METADATA,
    SCENARIO_IDS,
    SCENARIOS,
    get_scenario,
    missing_run_metadata,
    protocol_as_dict,
)


def test_all_six_scenarios_in_order() -> None:
    assert SCENARIO_IDS == (
        "S1_EMPTY_TARGET_ROOM",
        "S2_PERSON_MOVING_BEHIND_WALL",
        "S3_MOTION_NEAR_SENSORS_OUTSIDE",
        "S4_DOOR_AND_INTERFERENCE",
        "S5_STILL_AFTER_MOVING",
        "S6_DISCONNECT_AND_DEGRADED",
    )
    assert [s.scenario_id for s in SCENARIOS if s.counts_toward_status] == list(SCENARIO_IDS[:3])


def test_every_scenario_is_fully_described() -> None:
    assert set(REQUIRED_METADATA) == {"placement", "wall_description", "channel", "duration", "labelled_events",
                                      "conditions"}
    for s in SCENARIOS:
        assert s.title and s.purpose and s.instructions and s.measured and s.pass_fail_meaning
        assert s.required_metadata == REQUIRED_METADATA and s.min_duration_s > 0
        for label in s.interval_labels:
            assert any(label in text for text in s.instructions), (s.scenario_id, label)
    assert get_scenario("S2_PERSON_MOVING_BEHIND_WALL").interval_labels == ("MOVING",)  # type: ignore[union-attr]
    assert get_scenario("nope") is None


def test_protocol_is_json_ready_and_states_live_only() -> None:
    d = protocol_as_dict()
    json.dumps(d)
    assert len(d["scenarios"]) == 6
    assert any("LIVE" in rule and "never count" in rule for rule in d["general_rules"])
    assert any("consent" in rule.lower() or "agreeing" in rule for rule in d["general_rules"])


def test_missing_run_metadata() -> None:
    run = ValidationRun(scenario_id="S2_PERSON_MOVING_BEHIND_WALL", session_id="sess_1", source_mode=SourceMode.LIVE,
                        started_at_unix_ns=0)
    assert missing_run_metadata(run, n_labelled_events=0) == [
        "placement", "wall_description", "channel", "duration", "conditions", "labelled_events"]
    full = run.model_copy(update={"placement": "p", "wall_description": "w", "channel": 6, "conditions": "c",
                                  "ended_at_unix_ns": 10})
    assert missing_run_metadata(full, n_labelled_events=2) == []
    s1 = full.model_copy(update={"scenario_id": "S1_EMPTY_TARGET_ROOM"})
    assert missing_run_metadata(s1, n_labelled_events=0) == []  # S1 needs no interval labels
