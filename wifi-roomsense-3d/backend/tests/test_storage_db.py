"""SQLite metadata store: migrations, CRUD, constraints, thread-safety smoke."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from roomsense.schemas import (
    ActivityResult,
    ActivityState,
    CalibrationKind,
    CalibrationRecord,
    GeometryProvenance,
    Provenance,
    QualityLevel,
    QualityReport,
    RoomGeometry,
    SourceMode,
)
from roomsense.storage.db import LATEST_SCHEMA_VERSION, Database, StorageError
from roomsense.storage.models import (
    ActivityLogEntry,
    ConsentRecord,
    EventKind,
    LabeledEvent,
    RecordingInfo,
    RecordingStatus,
    ValidationRunStatus,
    ZoneModelRecord,
    validate_id,
)

from .storage_helpers import NS, T0, add_run, add_timeline, make_consent


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "meta.sqlite3")
    yield d
    d.close()


def _recording(consent_id: str, **kw: object) -> RecordingInfo:
    data: dict[str, object] = dict(
        recording_id="rec_a", session_id="sess_1", created_at_unix_ns=T0, source_mode=SourceMode.LIVE,
        original_source_mode=SourceMode.LIVE, label="walk test", status=RecordingStatus.COMPLETE,
        consent_id=consent_id,
    )
    data.update(kw)
    return RecordingInfo(**data)  # type: ignore[arg-type]


# ---------------------------------------------------------------- migrations


def test_wal_foreign_keys_and_migrations_recorded(tmp_path: Path) -> None:
    d = Database(tmp_path / "m.sqlite3")
    assert d.journal_mode == "wal"
    assert d.schema_version() == LATEST_SCHEMA_VERSION
    assert d.applied_migrations() == [(1, "initial schema")]
    fk = d._query_one("PRAGMA foreign_keys")
    assert fk is not None and fk[0] == 1
    d.close()


def test_migrations_are_idempotent_and_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "m.sqlite3"
    d = Database(path)
    d.add_consent(make_consent(consent_id="consent_x"))
    d.close()
    for _ in range(3):
        d = Database(path)
        assert d.schema_version() == LATEST_SCHEMA_VERSION
        assert len(d.applied_migrations()) == len({v for v, _ in d.applied_migrations()}) == 1
        assert d.get_consent("consent_x") is not None
        d.close()


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "m.sqlite3"
    Database(path).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO schema_migrations (version, description, applied_at_unix_ns) VALUES (999, 'future', 0)")
    conn.commit()
    conn.close()
    with pytest.raises(StorageError, match="newer"):
        Database(path)


def test_closed_database_raises(tmp_path: Path) -> None:
    d = Database(tmp_path / "m.sqlite3")
    d.close()
    d.close()  # idempotent
    with pytest.raises(StorageError):
        d.list_sessions()


# ---------------------------------------------------------------- ids


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "a.b", "a b", "x" * 65, "abc\n", "..", "a\\b", None, 5])
def test_validate_id_rejects_unsafe_ids(bad: object) -> None:
    with pytest.raises(ValueError):
        validate_id(bad)


def test_validate_id_accepts_safe_ids() -> None:
    assert validate_id("rec_ABC-123") == "rec_ABC-123"
    assert validate_id("x" * 64) == "x" * 64


# ---------------------------------------------------------------- CRUD


def test_sessions_crud_and_mode_mismatch(db: Database) -> None:
    s = db.ensure_session("sess_1", SourceMode.LIVE, T0)
    assert db.ensure_session("sess_1", SourceMode.LIVE) == s
    with pytest.raises(ValueError, match="LIVE"):
        db.ensure_session("sess_1", SourceMode.SIMULATION)
    assert db.end_session("sess_1", T0 + NS)
    assert not db.end_session("sess_1")  # already ended
    got = db.get_session("sess_1")
    assert got is not None and got.ended_at_unix_ns == T0 + NS
    assert [x.session_id for x in db.list_sessions()] == ["sess_1"]
    assert db.delete_session("sess_1") and db.get_session("sess_1") is None


def test_consent_crud_and_db_constraint(db: Database) -> None:
    c = db.add_consent(make_consent(consent_id="consent_1", participant_count=2))
    assert db.get_consent("consent_1") == c
    assert [x.consent_id for x in db.list_consents()] == ["consent_1"]
    # The table itself refuses a row without consent, even bypassing pydantic.
    with pytest.raises(sqlite3.IntegrityError):
        with db._write() as conn:
            conn.execute(
                "INSERT INTO consents VALUES ('consent_bad', 0, 0, 1, 'p', 'consent-v1', 'x')"
            )
    # model_construct bypasses validators; add_consent re-validates.
    forged = ConsentRecord.model_construct(**{**c.model_dump(), "consent_id": "consent_2",
                                              "all_participants_consented": False})
    with pytest.raises(ValidationError):
        db.add_consent(forged)
    assert db.get_consent("consent_2") is None


def test_recording_crud_and_consent_restrict(db: Database) -> None:
    db.add_consent(make_consent(consent_id="consent_1"))
    info = db.add_recording(_recording("consent_1", link_ids=["tx1->rx1"]))
    assert db.get_recording("rec_a") == info
    updated = info.model_copy(update={"frames": 10, "bytes": 1234, "duration_s": 2.5, "stop_reason": "done"})
    assert db.update_recording(updated)
    assert db.get_recording("rec_a") == updated
    assert [r.recording_id for r in db.list_recordings(session_id="sess_1")] == ["rec_a"]
    assert db.list_recordings(status=RecordingStatus.RECORDING) == []
    # Unknown consent id: foreign key refuses the row.
    with pytest.raises(sqlite3.IntegrityError):
        db.add_recording(_recording("consent_missing", recording_id="rec_b"))
    # A consent referenced by a recording cannot be deleted first.
    with pytest.raises(sqlite3.IntegrityError):
        db.delete_consent("consent_1")
    db.add_event(LabeledEvent(event_id="evt_1", session_id="sess_1", recording_id="rec_a", t_unix_ns=T0,
                              kind=EventKind.MARK, label="door"))
    db.add_event(LabeledEvent(event_id="evt_2", session_id="sess_1", t_unix_ns=T0, kind=EventKind.MARK,
                              label="unrelated"))
    run = add_run(db, scenario_id="S1_EMPTY_TARGET_ROOM", session_id="sess_1", start_ns=T0, end_ns=T0 + NS,
                  recording_id="rec_a")
    assert db.delete_recording("rec_a")
    assert db.get_recording("rec_a") is None
    assert db.get_event("evt_1") is None  # the recording's events go with it
    assert db.get_event("evt_2") is not None  # other events stay
    got_run = db.get_validation_run(run.run_id)
    assert got_run is not None and got_run.recording_id is None
    assert not db.delete_recording("rec_a")
    assert db.delete_consent("consent_1")


def test_synthetic_recording_must_be_flagged() -> None:
    with pytest.raises(ValidationError, match="synthetic"):
        _recording("consent_1", source_mode=SourceMode.SIMULATION, original_source_mode=SourceMode.SIMULATION,
                   synthetic=False)
    ok = _recording("consent_1", source_mode=SourceMode.REPLAY, original_source_mode=SourceMode.SIMULATION,
                    synthetic=True)
    assert not ok.usable_as_validation_evidence


def _calibration(cid: str, t: int, valid: bool = True) -> CalibrationRecord:
    return CalibrationRecord(
        calibration_id=cid, kind=CalibrationKind.QUIET_BASELINE, created_at_unix_ns=t, session_id="sess_1",
        source_mode=SourceMode.LIVE, link_ids=["tx1->rx1"], hardware_signature="hw1", room_config_hash="room1",
        processing_config_version="cfg-1", duration_s=60.0, frame_count=1500, window_count=100, valid=valid,
    )


def test_calibrations_crud_and_invalidation(db: Database) -> None:
    db.add_calibration(_calibration("cal_1", T0), {"tx1->rx1": {"feature_median": {"a": 1.0}, "bad": float("nan")}})
    db.add_calibration(_calibration("cal_2", T0 + NS))
    got = db.get_calibration("cal_1")
    assert got is not None and got.record.valid
    assert got.baselines["tx1->rx1"]["bad"] is None  # NaN stored as null, never as a number
    assert db.latest_valid_calibration().record.calibration_id == "cal_2"  # type: ignore[union-attr]
    assert db.latest_valid_calibration(hardware_signature="other") is None
    assert db.invalidate_calibration("cal_2", "sensor moved")
    got2 = db.get_calibration("cal_2")
    assert got2 is not None and not got2.record.valid and got2.record.invalidated_reason == "sensor moved"
    assert db.latest_valid_calibration().record.calibration_id == "cal_1"  # type: ignore[union-attr]
    assert db.invalidate_all_calibrations("room changed") == 1
    assert db.latest_valid_calibration() is None
    assert len(db.list_calibrations()) == 2 and db.list_calibrations(valid_only=True) == []
    assert not db.invalidate_calibration("cal_missing", "x")
    assert db.delete_calibration("cal_1") and db.get_calibration("cal_1") is None


def test_events_crud_and_time_filter(db: Database) -> None:
    for i, kind in enumerate([EventKind.START, EventKind.END, EventKind.MARK]):
        db.add_event(LabeledEvent(event_id=f"evt_{i}", session_id="sess_1", t_unix_ns=T0 + i * NS, kind=kind,
                                  label="MOVING"))
    assert [e.event_id for e in db.list_events(session_id="sess_1")] == ["evt_0", "evt_1", "evt_2"]
    assert [e.event_id for e in db.list_events(start_unix_ns=T0 + NS, end_unix_ns=T0 + NS)] == ["evt_1"]
    ev = db.get_event("evt_2")
    assert ev is not None
    assert db.update_event(ev.model_copy(update={"notes": "late label"}))
    assert db.get_event("evt_2").notes == "late label"  # type: ignore[union-attr]
    assert db.delete_event("evt_2") and not db.delete_event("evt_2")
    with pytest.raises(ValidationError):
        LabeledEvent(session_id="../x", t_unix_ns=T0, kind=EventKind.MARK, label="a")


def _result(state: ActivityState, t: int, score: float | None) -> ActivityResult:
    prov = Provenance(source_mode=SourceMode.LIVE, session_id="sess_1", link_ids=["tx1->rx1"],
                      window_start_unix_ns=t - 2 * NS, window_end_unix_ns=t, window_frame_count=50,
                      config_version="cfg-1", calibration_id="cal_1", computed_at_unix_ns=t + 1)
    return ActivityResult(link_id="tx1->rx1", state=state, activity_score=score, enter_threshold=4.0,
                          exit_threshold=2.5, quality=QualityReport(level=QualityLevel.GOOD), provenance=prov)


def test_activity_log_from_results_and_filters(db: Database) -> None:
    db.add_activity_result(_result(ActivityState.MOTION_DETECTED, T0, 5.0))
    db.add_activity_result(_result(ActivityState.UNKNOWN, T0 + NS, float("nan")))
    rows = db.list_activity(session_id="sess_1")
    assert [(r.state, r.score, r.calibration_id) for r in rows] == [
        ("MOTION_DETECTED", 5.0, "cal_1"), ("UNKNOWN", None, "cal_1")
    ]
    assert rows[0].t_end_unix_ns == T0  # window end, not compute time
    assert len(db.list_activity(start_unix_ns=T0 + 1)) == 1
    with pytest.raises(ValidationError):
        ActivityLogEntry(session_id="sess_1", link_id="l", t_end_unix_ns=0, state="PRESENT",
                         source_mode=SourceMode.LIVE)
    assert db.delete_activity("sess_1") == 2


def test_prune_activity_log_keeps_validation_evidence(db: Database) -> None:
    add_timeline(db, session_id="sess_1", link_id="l1", start_ns=T0, pieces=[(50, "NO_MOTION_DETECTED")],
                 step_s=1.0)  # 50 rows at T0 .. T0+49 s
    add_run(db, scenario_id="S1_EMPTY_TARGET_ROOM", session_id="sess_1", start_ns=T0 + 10 * NS,
            end_ns=T0 + 19 * NS)  # protects 10 rows
    deleted = db.prune_activity_log(20)
    assert deleted == 30
    remaining = db.list_activity()
    assert len(remaining) == 20
    times = {(r.t_end_unix_ns - T0) // NS for r in remaining}
    assert set(range(10, 20)) <= times  # evidence rows survive
    assert db.prune_activity_log(5) == 10  # only unprotected rows can go
    assert db.count_activity() == 10
    assert db.prune_activity_log(5, protect_validation_runs=False) == 5
    assert db.prune_activity_log(1000) == 0
    with pytest.raises(ValueError):
        db.prune_activity_log(-1)


def test_validation_runs_crud(db: Database) -> None:
    run = add_run(db, scenario_id="S2_PERSON_MOVING_BEHIND_WALL", session_id="sess_1", start_ns=T0, end_ns=None,
                  status=ValidationRunStatus.RUNNING, link_ids=["tx1->rx1"])
    assert db.get_validation_run(run.run_id) == run
    ended = db.end_validation_run(run.run_id, ended_at_unix_ns=T0 + 60 * NS)
    assert ended is not None and ended.status == ValidationRunStatus.COMPLETE and ended.duration_s == 60.0
    assert db.get_validation_run(run.run_id) == ended
    assert [r.run_id for r in db.list_validation_runs(scenario_id="S2_PERSON_MOVING_BEHIND_WALL")] == [run.run_id]
    assert db.list_validation_runs(session_id="other") == []
    assert db.end_validation_run("vrun_missing") is None
    assert db.delete_validation_run(run.run_id) and db.get_validation_run(run.run_id) is None


def test_zone_models_never_enabled_with_synthetic_data(db: Database) -> None:
    m = ZoneModelRecord(model_id="zm_1", created_at_unix_ns=T0, criteria_version="c1", hardware_signature="hw",
                        room_config_hash="room", config_version="cfg", link_ids=["a", "b"],
                        report={"acc": float("inf")}, artifact_relpath="models/zm_1.joblib",
                        synthetic_data_used=True)
    db.add_zone_model(m)
    got = db.get_zone_model("zm_1")
    assert got is not None and got.report == {"acc": None} and not got.enabled
    with pytest.raises(ValidationError):
        db.set_zone_model_enabled("zm_1", True)
    with pytest.raises(sqlite3.IntegrityError):  # the table refuses it too
        with db._write() as conn:
            conn.execute("UPDATE zone_models SET enabled = 1 WHERE model_id = 'zm_1'")
    live = m.model_copy(update={"model_id": "zm_2", "synthetic_data_used": False})
    db.add_zone_model(live)
    assert db.set_zone_model_enabled("zm_2", True)
    assert [x.model_id for x in db.list_zone_models(enabled_only=True)] == ["zm_2"]
    assert db.delete_zone_model("zm_1") and not db.set_zone_model_enabled("zm_1", False)
    for bad in ("/etc/passwd", "../x", "a/../../b", "C:\\x", ""):
        with pytest.raises(ValidationError):
            ZoneModelRecord(model_id="zm_3", created_at_unix_ns=T0, criteria_version="c", hardware_signature="h",
                            room_config_hash="r", config_version="c", artifact_relpath=bad)


def test_room_versions(db: Database) -> None:
    room = RoomGeometry(geometry_id="g1", provenance=GeometryProvenance.USER_PROVIDED, name="Lab", width_m=4,
                        depth_m=3)
    v1 = db.add_room_version(room, T0)
    v2 = db.add_room_version(room.model_copy(update={"width_m": 5.0}), T0 + NS)
    assert v1.version_id is not None and v2.version_id is not None and v2.version_id > v1.version_id
    assert v1.config_hash != v2.config_hash
    latest = db.latest_room_version()
    assert latest is not None and latest.version_id == v2.version_id and latest.provenance == "USER_PROVIDED"
    assert RoomGeometry.model_validate(latest.geometry).width_m == 5.0
    assert db.get_room_version(v1.version_id) == v1
    assert len(db.list_room_versions()) == 2
    assert db.delete_room_version(v2.version_id)
    assert db.latest_room_version() == v1


# ---------------------------------------------------------------- threads


def test_thread_safety_smoke(tmp_path: Path) -> None:
    d = Database(tmp_path / "t.sqlite3")
    errors: list[BaseException] = []

    def writer(k: int) -> None:
        try:
            for i in range(50):
                d.add_activity(ActivityLogEntry(session_id="sess_t", link_id=f"l{k}", t_end_unix_ns=T0 + i,
                                                state="NO_MOTION_DETECTED", source_mode=SourceMode.LIVE))
                d.add_event(LabeledEvent(session_id="sess_t", t_unix_ns=T0 + i, kind=EventKind.MARK, label=f"k{k}"))
                d.list_activity(session_id="sess_t", limit=10)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert d.count_activity() == 8 * 50
    assert len(d.list_events(session_id="sess_t", limit=None)) == 8 * 50
    d.close()
