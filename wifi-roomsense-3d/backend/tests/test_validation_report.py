"""Through-wall report: status logic, evidence exclusion, NOT MEASURED output.

The "LIVE" datasets below are hand-built activity-log rows labelled LIVE to
exercise the status logic. They are not measurements and say nothing about
whether through-wall sensing works.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from roomsense.config import REPO_ROOT
from roomsense.schemas import SourceMode
from roomsense.storage.db import Database
from roomsense.storage.models import ConsentRecord, RecordingInfo, RecordingStatus, ValidationRunStatus
from roomsense.validation.protocol import SCENARIO_IDS
from roomsense.validation.report import (
    NOT_MEASURED,
    CriteriaError,
    build_validation_report,
    load_criteria,
    render_markdown,
)

from .storage_helpers import (
    NS,
    OFFLINE,
    QUIET,
    T0,
    add_event_series,
    add_interval,
    add_run,
    add_timeline,
    make_consent,
)

CRITERIA = REPO_ROOT / "configs" / "through_wall_criteria.toml"
S1, S2, S3, S4, S5, S6 = SCENARIO_IDS
LINK = "tx1->rx1"


@pytest.fixture
def db(tmp_path: Path):
    d = Database(tmp_path / "meta.sqlite3")
    yield d
    d.close()


def build_dataset(db: Database, *, mode: SourceMode = SourceMode.LIVE, session: str = "sess_live",
                  start_ns: int = T0, s1_hours: float = 4.0, s2_detected: int = 20, s3_detected: int = 2,
                  placement: str | None = None, recording_id: str | None = None) -> int:
    """S1 + S2 + S3 runs in one session. Returns the end time (ns)."""
    extra = {} if placement is None else {"placement": placement}
    t = start_ns
    end = add_timeline(db, session_id=session, link_id=LINK, start_ns=t, pieces=[(s1_hours * 3600, QUIET)],
                       step_s=2.0, source_mode=mode)
    add_run(db, scenario_id=S1, session_id=session, start_ns=t, end_ns=end, source_mode=mode,
            recording_id=recording_id, **extra)
    t = end
    end = add_event_series(db, session_id=session, link_id=LINK, start_ns=t, label="MOVING", n_events=20,
                           detected=[i < s2_detected for i in range(20)], source_mode=mode)
    add_run(db, scenario_id=S2, session_id=session, start_ns=t, end_ns=end, source_mode=mode, **extra)
    t = end
    end = add_event_series(db, session_id=session, link_id=LINK, start_ns=t, label="OUTSIDE_MOTION", n_events=10,
                           detected=[i < s3_detected for i in range(10)], source_mode=mode)
    add_run(db, scenario_id=S3, session_id=session, start_ns=t, end_ns=end, source_mode=mode, **extra)
    return end


# ------------------------------------------------------------------ criteria


def test_criteria_version_is_hash_of_file_and_all_values_required(tmp_path: Path) -> None:
    crit = load_criteria(CRITERIA)
    assert crit.criteria_version == hashlib.sha256(CRITERIA.read_bytes()).hexdigest()[:16]
    assert (crit.s1_min_hours, crit.s1_max_fa_upper, crit.s2_min_events, crit.s2_min_recall_lower,
            crit.s3_min_events, crit.s3_not_distinguishable_fraction) == (2.0, 1.0, 20, 0.8, 10, 0.5)
    text = CRITERIA.read_text()
    assert "must NOT be lowered" in text and "BEFORE any through-wall data" in text
    edited = tmp_path / "c.toml"
    edited.write_text(text + "\n# a comment changes the version\n")
    assert load_criteria(edited).criteria_version != crit.criteria_version
    missing = tmp_path / "missing.toml"
    missing.write_text(text.replace("min_recall_lower95 = 0.8", ""))
    with pytest.raises(CriteriaError, match="min_recall_lower95"):
        load_criteria(missing)
    bad = tmp_path / "bad.toml"
    bad.write_text(text.replace("min_recall_lower95 = 0.8", "min_recall_lower95 = 1.5"))
    with pytest.raises(CriteriaError):
        load_criteria(bad)
    with pytest.raises(CriteriaError):
        load_criteria(tmp_path / "does_not_exist.toml")


# ------------------------------------------------------------------ empty


def test_empty_db_everything_not_measured_and_unverified(db: Database) -> None:
    report = build_validation_report(db, CRITERIA, now_ns=T0)
    json.dumps(report, allow_nan=False)  # strict JSON: no NaN or Infinity anywhere
    assert report["through_wall_status"] == "UNVERIFIED"
    assert report["criteria_version"] == load_criteria(CRITERIA).criteria_version
    assert report["current_setup"] is None and report["runs"] == [] and report["excluded_runs"] == []
    assert {k: v["value"] for k, v in report["evidence_levels"].items()} == {
        "software_tested": None, "hardware_tested": False, "through_wall_validated": False}
    s1 = report["scenarios"][S1]
    assert s1["runs"] == 0 and s1["observed"] is None and s1["false_alarms"]["rate"]["rate_per_hour"] is None
    assert s1["false_alarms"]["onsets"] is None
    assert report["scenarios"][S2]["recall"]["detection"]["fraction"] is None
    assert report["scenarios"][S2]["recall"]["latency"]["median_s"] is None
    assert all(NOT_MEASURED in u for u in report["unmet_requirements"])

    md = render_markdown(report)
    assert "## Through-wall status: UNVERIFIED" in md
    for sid in SCENARIO_IDS:
        section = md.split(f"### {sid}")[1].split("###")[0]
        assert f"**{NOT_MEASURED}**" in section
        for line in section.splitlines():
            if line.startswith("| ") and not line.startswith(("| Metric", "| Counted runs")):
                assert NOT_MEASURED in line, line  # no invented numbers
    assert "software_tested | NOT MEASURED" in md


# ------------------------------------------------------------------ status


def test_live_dataset_meeting_criteria_is_validated(db: Database) -> None:
    build_dataset(db)
    report = build_validation_report(db, CRITERIA, software_tested=True)
    json.dumps(report, allow_nan=False)
    assert report["through_wall_status"] == "VALIDATED", report["unmet_requirements"]
    assert report["unmet_requirements"] == []
    levels = report["evidence_levels"]
    assert levels["through_wall_validated"]["value"] is True and levels["hardware_tested"]["value"] is True
    assert levels["software_tested"]["value"] is True

    s1 = report["scenarios"][S1]
    assert s1["false_alarms"]["onsets"] == 0 and s1["decision_hours"] == pytest.approx(4.0)
    assert s1["false_alarms"]["rate"]["ci_high"] == pytest.approx(3.68888 / 4.0, abs=1e-4)
    s2 = report["scenarios"][S2]["recall"]
    assert (s2["detection"]["successes"], s2["detection"]["trials"]) == (20, 20)
    assert s2["detection"]["ci_low"] == pytest.approx(0.83887, abs=1e-4)
    assert s2["latency"]["median_s"] == pytest.approx(2.0) and s2["missed_events"] == []
    assert report["scenarios"][S2]["per_link_recall"][LINK]["fraction"] == 1.0
    s3 = report["scenarios"][S3]["outside_detection"]["detection"]
    assert (s3["successes"], s3["trials"], s3["fraction"]) == (2, 10, 0.2)

    cs = report["current_setup"]
    assert cs["runs"] == 3 and cs["channel"] == 6 and "plasterboard" in cs["wall_description"]
    assert cs["calibration_ids"] == ["cal_test"] and cs["links"] == [LINK]
    run = report["runs"][0]
    for key in ("placement", "wall_description", "channel", "duration_s", "conditions"):
        assert run[key] not in (None, "")
    md = render_markdown(report)
    assert "## Through-wall status: VALIDATED" in md and report["criteria_version"] in md
    assert "says nothing about a motionless person" in report["explanation"]


def test_outside_room_detection_makes_it_not_distinguishable(db: Database) -> None:
    build_dataset(db, s3_detected=6)  # S1 and S2 would pass on their own
    report = build_validation_report(db, CRITERIA)
    assert report["through_wall_status"] == "NOT_DISTINGUISHABLE"
    assert "OUTSIDE the target room" in report["explanation"]
    assert report["evidence_levels"]["through_wall_validated"]["value"] is False
    assert report["scenarios"][S3]["outside_detection"]["detection"]["fraction"] == 0.6


def test_insufficient_recall_or_hours_stays_unverified(db: Database) -> None:
    build_dataset(db, s2_detected=19, s1_hours=3.0)
    report = build_validation_report(db, CRITERIA)
    assert report["through_wall_status"] == "UNVERIFIED"
    joined = " | ".join(report["unmet_requirements"])
    assert "S2: recall lower 95% bound 0.76" in joined  # 19/20 -> Wilson lower 0.764
    assert "S1: false-alarm upper 95% bound 1.23/h" in joined  # 3.689 / 3 h
    missed = report["scenarios"][S2]["recall"]["missed_events"]
    assert len(missed) == 1 and "no MOTION_DETECTED onset" in missed[0]["reason"]


def test_too_few_events_warns_insufficient(db: Database) -> None:
    t = add_event_series(db, session_id="sess_few", link_id=LINK, start_ns=T0, label="MOVING", n_events=4,
                         detected=[True] * 4)
    add_run(db, scenario_id=S2, session_id="sess_few", start_ns=T0, end_ns=t)
    report = build_validation_report(db, CRITERIA)
    s2 = report["scenarios"][S2]
    assert any("insufficient events" in w for w in s2["recall"]["warnings"])
    assert "S2: 4 usable events; at least 20 required" in report["unmet_requirements"]
    assert report["through_wall_status"] == "UNVERIFIED"


# ------------------------------------------------------------------ exclusions


@pytest.mark.parametrize("mode", [SourceMode.SIMULATION, SourceMode.REPLAY])
def test_synthetic_or_replayed_runs_never_validate(db: Database, mode: SourceMode) -> None:
    build_dataset(db, mode=mode, session=f"sess_{mode.value.lower()}")
    report = build_validation_report(db, CRITERIA)
    assert report["through_wall_status"] == "UNVERIFIED"
    assert report["evidence_levels"]["through_wall_validated"]["value"] is False
    assert report["evidence_levels"]["hardware_tested"]["value"] is False
    assert len(report["runs"]) == 3 and all(not r["counted_as_evidence"] for r in report["runs"])
    assert len(report["excluded_runs"]) == 3
    assert all(any(mode.value in reason for reason in r["reasons"]) for r in report["excluded_runs"])
    assert report["scenarios"][S1]["runs"] == 0  # listed, not counted
    md = render_markdown(report)
    assert mode.value in md and "| no |" in md


def test_live_run_with_simulated_activity_rows_is_excluded(db: Database) -> None:
    end = build_dataset(db)
    # a later LIVE-labelled run whose decisions came from a simulation session
    add_timeline(db, session_id="sess_mixed", link_id=LINK, start_ns=end, pieces=[(60, QUIET)],
                 source_mode=SourceMode.SIMULATION)
    add_run(db, scenario_id=S1, session_id="sess_mixed", start_ns=end, end_ns=end + 60 * NS,
            placement="another placement")
    report = build_validation_report(db, CRITERIA)
    bad = report["excluded_runs"]
    assert len(bad) == 1 and "non-LIVE rows" in bad[0]["reasons"][0]
    assert report["through_wall_status"] == "VALIDATED"  # the untainted setup still stands


def test_run_linked_to_synthetic_recording_is_excluded(db: Database) -> None:
    consent: ConsentRecord = db.add_consent(make_consent())
    db.add_recording(RecordingInfo(
        recording_id="rec_sim", session_id="sess_live", created_at_unix_ns=T0, source_mode=SourceMode.REPLAY,
        original_source_mode=SourceMode.SIMULATION, label="replayed simulation", status=RecordingStatus.COMPLETE,
        consent_id=consent.consent_id, synthetic=True,
    ))
    build_dataset(db, recording_id="rec_sim")
    report = build_validation_report(db, CRITERIA)
    excluded = {r["scenario_id"]: r["reasons"] for r in report["excluded_runs"]}
    assert "the linked recording contains synthetic data" in excluded[S1]
    assert report["through_wall_status"] == "UNVERIFIED"  # S1 no longer counts


def test_missing_metadata_and_running_runs_are_excluded(db: Database) -> None:
    t = add_timeline(db, session_id="sess_meta", link_id=LINK, start_ns=T0, pieces=[(600, QUIET)])
    add_run(db, scenario_id=S1, session_id="sess_meta", start_ns=T0, end_ns=t, wall_description="  ", channel=None)
    add_run(db, scenario_id=S1, session_id="sess_meta", start_ns=t, end_ns=None, status=ValidationRunStatus.RUNNING)
    add_run(db, scenario_id="S9_MADE_UP", session_id="sess_meta", start_ns=T0, end_ns=t)
    report = build_validation_report(db, CRITERIA)
    reasons = [" ".join(r["reasons"]) for r in report["excluded_runs"]]
    assert any("missing required metadata: wall_description, channel" in r for r in reasons)
    assert any("RUNNING, not COMPLETE" in r for r in reasons)
    assert any("unknown scenario" in r for r in reasons)
    assert report["current_setup"] is None and report["through_wall_status"] == "UNVERIFIED"


def test_overlapping_runs_are_not_double_counted(db: Database) -> None:
    end = add_timeline(db, session_id="sess_ov", link_id=LINK, start_ns=T0, pieces=[(3 * 3600, QUIET)], step_s=2.0)
    first = add_run(db, scenario_id=S1, session_id="sess_ov", start_ns=T0, end_ns=end)
    second = add_run(db, scenario_id=S1, session_id="sess_ov", start_ns=T0 + 60 * NS, end_ns=end)
    report = build_validation_report(db, CRITERIA)
    assert report["scenarios"][S1]["runs"] == 1 and report["scenarios"][S1]["run_ids"] == [first.run_id]
    assert report["scenarios"][S1]["decision_hours"] == pytest.approx(3.0)  # not 6 h
    (excluded,) = report["excluded_runs"]
    assert excluded["run_id"] == second.run_id and "counted twice" in excluded["reasons"][0]


def test_status_refers_to_latest_setup(db: Database) -> None:
    end = build_dataset(db)
    t = add_timeline(db, session_id="sess_moved", link_id=LINK, start_ns=end, pieces=[(600, QUIET)])
    add_run(db, scenario_id=S1, session_id="sess_moved", start_ns=end, end_ns=t,
            placement="RX moved to the other corner")
    report = build_validation_report(db, CRITERIA)
    assert report["through_wall_status"] == "UNVERIFIED"
    assert report["current_setup"]["placement"] == "RX moved to the other corner"
    assert [s["status"] for s in report["setups"]] == ["VALIDATED"]
    assert "Other setups" in render_markdown(report)


# ------------------------------------------------------------------ S4-S6


def test_informational_scenarios(db: Database) -> None:
    # S5: walk then stand still; MOTION lingers 3 s into the still period.
    t0 = T0
    t = add_timeline(db, session_id="sess_info", link_id=LINK, start_ns=t0,
                     pieces=[(10, QUIET), (10, "MOTION_DETECTED"), (8, "MOTION_DETECTED"), (52, QUIET)])
    add_interval(db, session_id="sess_info", label="MOVING", start_ns=t0 + 10 * NS, end_ns=t0 + 20 * NS)
    add_interval(db, session_id="sess_info", label="still", start_ns=t0 + 20 * NS, end_ns=t0 + 80 * NS)
    add_run(db, scenario_id=S5, session_id="sess_info", start_ns=t0, end_ns=t)
    # S6: two links; rx2 unplugged 20 s while rx1 stays quiet.
    s6 = t
    add_timeline(db, session_id="sess_info", link_id="tx1->rx2", start_ns=s6,
                 pieces=[(10, QUIET), (20, OFFLINE), (10, QUIET)])
    t = add_timeline(db, session_id="sess_info", link_id=LINK, start_ns=s6, pieces=[(40, QUIET)])
    add_interval(db, session_id="sess_info", label="DISCONNECT", start_ns=s6 + 10 * NS, end_ns=s6 + 30 * NS)
    add_run(db, scenario_id=S6, session_id="sess_info", start_ns=s6, end_ns=t)
    report = build_validation_report(db, CRITERIA)

    s5 = report["scenarios"][S5]
    assert s5["moving_recall"]["detection"]["successes"] == 1
    # still 20-80 s, settle 5 s -> 55 s of decision time, MOTION 25-28 s -> 3 s
    assert s5["stationary_detection"]["fraction"] == pytest.approx(3 / 55)
    assert "motionless person" in s5["limitation"]
    s6r = report["scenarios"][S6]
    assert s6r["false_all_clear"]["fraction"] == 0.0  # combined state is UNKNOWN, never quiet
    assert s6r["state_time_disconnect_s"]["UNKNOWN"] == pytest.approx(20.0)
    assert not any("DEFECT" in w for w in s6r["warnings"])
    assert report["through_wall_status"] == "UNVERIFIED"  # informational scenarios never validate


def test_false_all_clear_is_flagged_as_defect(db: Database) -> None:
    t = add_timeline(db, session_id="sess_bad", link_id=LINK, start_ns=T0, pieces=[(40, QUIET)])
    add_interval(db, session_id="sess_bad", label="DISCONNECT", start_ns=T0 + 10 * NS, end_ns=T0 + 30 * NS)
    add_run(db, scenario_id=S6, session_id="sess_bad", start_ns=T0, end_ns=t)
    report = build_validation_report(db, CRITERIA)
    s6 = report["scenarios"][S6]
    assert s6["false_all_clear"]["fraction"] == 1.0
    assert any("DEFECT" in w for w in s6["warnings"])
    assert "DEFECT" in render_markdown(report)
