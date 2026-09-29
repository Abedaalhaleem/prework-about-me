"""Recorder: consent, bounds, quota, deletion, path safety, round trip,
synthetic flag. All frames are hand-built test objects (see storage_helpers)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from roomsense.recording_format import read_header, read_recording
from roomsense.schemas import SourceMode
from roomsense.storage.db import Database
from roomsense.storage.models import (
    CONSENT_STATEMENT_V1,
    ConsentRecord,
    EventKind,
    LabeledEvent,
    RecordingStatus,
)
from roomsense.storage.recordings import (
    Recorder,
    RecordingRefused,
    delete_recording,
    iter_recording,
    iter_recording_events,
    list_recordings,
    recording_file_for_read,
    recording_path,
    total_recording_bytes,
)

from .storage_helpers import NS, T0, make_consent, make_frame, storage_cfg


class FakeClock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * NS)


@pytest.fixture
def env(tmp_path: Path):
    db = Database(tmp_path / "meta.sqlite3")
    data_dir = tmp_path / "data"
    yield db, data_dir
    db.close()


def _recorder(db: Database, data_dir: Path, **cfg: object) -> tuple[Recorder, FakeClock, list]:
    mono = FakeClock(0)
    stopped: list = []
    rec = Recorder(db, data_dir, storage_cfg(**cfg), wall_clock_ns=FakeClock(T0), monotonic_ns=mono,
                   on_auto_stop=stopped.append)
    return rec, mono, stopped


def _start(rec: Recorder, **kw: object):
    args: dict[str, object] = dict(consent=make_consent(), label="hallway walk",
                                   scenario="S2_PERSON_MOVING_BEHIND_WALL",
                                   session_id="sess_live", source_mode=SourceMode.LIVE, link_ids=["tx1->rx1"],
                                   config_version="cfg-abc")
    args.update(kw)
    return rec.start(**args)  # type: ignore[arg-type]


# ------------------------------------------------------------------ consent


def test_consent_model_refuses_without_everyones_agreement() -> None:
    with pytest.raises(ValidationError, match="every person present"):
        make_consent(all_participants_consented=False)
    with pytest.raises(ValidationError):
        make_consent(participant_count=0)
    with pytest.raises(ValidationError):
        make_consent(purpose="   ")
    with pytest.raises(ValidationError):
        make_consent(purpose="x" * 501)
    with pytest.raises(ValidationError):
        make_consent(statement_version="consent-v0")
    with pytest.raises(ValidationError):
        make_consent(statement_text="I agree to anything")
    with pytest.raises(ValidationError):  # no identifying fields can be added
        make_consent(participant_names=["Alice"])
    c = make_consent()
    assert c.statement_text == CONSENT_STATEMENT_V1
    for phrase in ("local", "opt-in", "controls", "agreed", "deleted at any time", "No identification"):
        assert phrase in CONSENT_STATEMENT_V1


def test_recording_refused_without_valid_consent(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    forged = ConsentRecord.model_construct(**{**make_consent().model_dump(), "all_participants_consented": False})
    with pytest.raises(RecordingRefused) as ei:
        _start(rec, consent=forged)
    assert ei.value.code == "CONSENT_INVALID"
    with pytest.raises(RecordingRefused) as ei:
        _start(rec, consent={"all_participants_consented": False, "participant_count": 1, "purpose": "x"})
    assert ei.value.code == "CONSENT_INVALID"
    with pytest.raises(RecordingRefused):
        _start(rec, consent=None)
    assert rec.active is None
    assert db.list_recordings() == [] and db.list_consents() == []
    assert total_recording_bytes(data_dir) == 0


def test_invalid_arguments_refused(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    for kw in ({"label": "  "}, {"session_id": "../../etc"}, {"max_seconds": 0}, {"max_seconds": float("nan")},
               {"source_mode": "LIVEISH"}):
        with pytest.raises(RecordingRefused) as ei:
            _start(rec, **kw)
        assert ei.value.code == "INVALID_ARGUMENT"
    assert rec.active is None and db.list_recordings() == []


# ------------------------------------------------------------------ round trip


def test_round_trip_frames_events_header_footer(env) -> None:
    db, data_dir = env
    rec, mono, _ = _recorder(db, data_dir)
    info = _start(rec, notes="first try")
    assert info.status == RecordingStatus.RECORDING and not info.synthetic
    assert db.get_recording(info.recording_id).status == RecordingStatus.RECORDING  # type: ignore[union-attr]
    with pytest.raises(RecordingRefused) as ei:
        _start(rec)
    assert ei.value.code == "ALREADY_RECORDING"

    frames = [make_frame(counter=i, raw_line=f"RSCSI,1,{i}") for i in range(30)]
    for f in frames[:15]:
        assert rec.write(f)
    assert rec.write_event({"kind": "DISCONNECTED", "link_id": "tx1->rx1", "receiver_id": "rx1",
                            "detail": "serial port vanished", "payload": "must not be stored",
                            "host_monotonic_ns": 5})
    for f in frames[15:]:
        assert rec.write(f)
    # Frames from another session or another source mode are never mixed in.
    assert not rec.write(make_frame(session_id="sess_other", counter=99))
    assert not rec.write(make_frame(source_mode=SourceMode.SIMULATION, counter=98, synthetic=True))
    assert rec.status()["frames_rejected"] == 2
    mono.advance(12.5)
    done = rec.stop()
    assert done is not None and rec.active is None and rec.stop() is None
    assert done.status == RecordingStatus.COMPLETE and done.frames == 30 and done.duration_s == pytest.approx(12.5)
    path = recording_path(data_dir, done.recording_id)
    assert done.bytes == path.stat().st_size > 0
    assert db.get_recording(done.recording_id) == done
    assert list_recordings(db) == [done]

    assert list(iter_recording(path)) == frames
    events = list(iter_recording_events(path))
    assert events == [{"kind": "DISCONNECTED", "link_id": "tx1->rx1", "receiver_id": "rx1",
                       "detail": "serial port vanished", "host_monotonic_ns": 5, "host_unix_ns": T0}]
    header = read_header(path)
    for key, value in {"consent_id": done.consent_id, "label": "hallway walk", "config_version": "cfg-abc",
                       "scenario": "S2_PERSON_MOVING_BEHIND_WALL", "original_source_mode": "LIVE",
                       "session_id": "sess_live", "synthetic": False, "notes": "first try"}.items():
        assert header[key] == value
    footer = [r for r in read_recording(path, decode_frames=False) if r["type"] == "footer"][0]
    assert footer["status"] == "COMPLETE" and footer["frames"] == 30 and footer["frames_rejected"] == 2
    # The consent record is stored alongside.
    consent = db.get_consent(done.consent_id)
    assert consent is not None and consent.all_participants_consented


def test_link_event_objects_are_accepted_duck_typed(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    _start(rec)

    class Ev:  # shaped like acquisition.base.LinkEvent
        kind = "RECONNECTING"
        link_id = "tx1->rx1"
        receiver_id = "rx1"
        detail = "x" * 5000
        host_monotonic_ns = None
        data = {"secret": "never stored"}

    assert rec.write_event(Ev())
    with pytest.raises(ValueError):
        rec.write_event({"link_id": "tx1->rx1"})  # no kind
    info = rec.stop()
    assert info is not None
    ev = list(iter_recording_events(recording_path(data_dir, info.recording_id)))[0]
    assert ev["kind"] == "RECONNECTING" and len(ev["detail"]) == 1000 and "data" not in ev and "secret" not in str(ev)


def test_concurrent_writers_are_serialised(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    _start(rec)
    ok = []

    def worker(k: int) -> None:
        ok.append(sum(rec.write(make_frame(counter=k * 1000 + i)) for i in range(100)))

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    info = rec.stop()
    assert info is not None and sum(ok) == 600 and info.frames == 600
    assert len(list(iter_recording(recording_path(data_dir, info.recording_id)))) == 600


# ------------------------------------------------------------------ bounds


def test_byte_limit_truncates(env) -> None:
    db, data_dir = env
    rec, _, stopped = _recorder(db, data_dir, max_recording_bytes=1)
    _start(rec)
    assert rec.write(make_frame(counter=0))  # written, then the limit stops the recording
    assert rec.active is None
    assert not rec.write(make_frame(counter=1))
    info = rec.last_finished
    assert info is not None and info.status == RecordingStatus.TRUNCATED_LIMIT
    assert "max_recording_bytes" in (info.stop_reason or "")
    assert stopped == [info]
    assert db.get_recording(info.recording_id) == info
    # A truncated recording is still a complete, replayable file.
    assert len(list(iter_recording(recording_path(data_dir, info.recording_id)))) == 1


def test_duration_limit_uses_smaller_of_request_and_config(env) -> None:
    db, data_dir = env
    rec, mono, stopped = _recorder(db, data_dir, max_recording_seconds=100.0)
    _start(rec, max_seconds=2.0)
    assert rec.write(make_frame(counter=0))
    mono.advance(1.0)
    assert rec.write(make_frame(counter=1)) and rec.active is not None
    mono.advance(1.5)
    assert rec.write(make_frame(counter=2))
    info = rec.last_finished
    assert rec.active is None and info is not None and info.status == RecordingStatus.TRUNCATED_LIMIT
    assert info.frames == 3 and "2 s" in (info.stop_reason or "")

    rec2, mono2, _ = _recorder(db, data_dir, max_recording_seconds=1.0)
    _start(rec2, max_seconds=100.0)
    mono2.advance(1.0)
    stopped_info = rec2.check_limits()  # no frames needed to enforce the duration
    assert stopped_info is not None and stopped_info.status == RecordingStatus.TRUNCATED_LIMIT
    assert rec2.active is None and stopped_info.frames == 0


def test_total_quota_refuses_new_recordings(env) -> None:
    db, data_dir = env
    rdir = data_dir / "recordings"
    rdir.mkdir(parents=True)
    (rdir / "rec_old.jsonl.gz").write_bytes(b"\0" * 5000)
    (rdir / "not_a_recording.txt").write_bytes(b"\0" * 10_000)  # not counted
    assert total_recording_bytes(data_dir) == 5000
    rec, _, _ = _recorder(db, data_dir, max_total_recording_bytes=5000)
    with pytest.raises(RecordingRefused) as ei:
        _start(rec)
    assert ei.value.code == "QUOTA_EXCEEDED" and "delete" in ei.value.detail
    assert db.list_recordings() == []


def test_remaining_quota_bounds_the_active_recording(env) -> None:
    db, data_dir = env
    rdir = data_dir / "recordings"
    rdir.mkdir(parents=True)
    (rdir / "rec_old.jsonl.gz").write_bytes(b"\0" * 5000)
    rec, _, _ = _recorder(db, data_dir, max_total_recording_bytes=5001, max_recording_bytes=10_000_000)
    _start(rec)
    rec.write(make_frame(counter=0))
    info = rec.last_finished
    assert info is not None and info.status == RecordingStatus.TRUNCATED_LIMIT
    assert "total recording quota" in (info.stop_reason or "")


def test_write_failure_marks_error(env, monkeypatch) -> None:
    db, data_dir = env
    rec, _, stopped = _recorder(db, data_dir)
    _start(rec)
    writer = rec._active.writer  # type: ignore[union-attr]

    def boom(frame):
        raise OSError("disk full")

    monkeypatch.setattr(writer, "write_frame", boom)
    assert not rec.write(make_frame())
    info = rec.last_finished
    assert info is not None and info.status == RecordingStatus.ERROR and "disk full" in (info.stop_reason or "")
    assert stopped == [info] and rec.active is None


def test_interrupted_recordings_are_marked_error_on_restart(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    info = _start(rec)
    rec.write(make_frame())
    # Simulate a crash: a new Recorder on the same DB while the row says RECORDING.
    Recorder(db, data_dir, storage_cfg())
    got = db.get_recording(info.recording_id)
    assert got is not None and got.status == RecordingStatus.ERROR and "interrupted" in (got.stop_reason or "")
    assert got.ended_at_unix_ns is None  # unknown, not invented


# ------------------------------------------------------------------ synthetic


def test_simulation_recording_is_synthetic_and_never_evidence(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    info = _start(rec, session_id="sess_sim", source_mode=SourceMode.SIMULATION)
    assert info.synthetic and info.original_source_mode == SourceMode.SIMULATION
    assert not rec.write(make_frame(session_id="sess_sim", source_mode=SourceMode.LIVE))
    assert rec.write(make_frame(session_id="sess_sim", source_mode=SourceMode.SIMULATION, synthetic=True))
    done = rec.stop()
    assert done is not None and done.synthetic and not done.usable_as_validation_evidence
    assert read_header(recording_path(data_dir, done.recording_id))["synthetic"] is True


def test_replay_of_synthetic_frames_becomes_synthetic(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    info = _start(rec, session_id="sess_rep", source_mode=SourceMode.REPLAY)
    assert info.original_source_mode is None and not info.synthetic  # unknown until frames say otherwise
    rec.write(make_frame(session_id="sess_rep", source_mode=SourceMode.REPLAY, synthetic=True))
    done = rec.stop()
    assert done is not None and done.synthetic and done.original_source_mode == SourceMode.SIMULATION
    assert not done.usable_as_validation_evidence
    live = _start(rec)
    rec.write(make_frame())
    live_done = rec.stop()
    assert live_done is not None and live_done.usable_as_validation_evidence
    assert info.recording_id != live.recording_id


# ------------------------------------------------------------------ delete / paths


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..", "a/b", "rec.jsonl", "", "a\\b", "x" * 65, "rec_1\n"])
def test_path_traversal_ids_rejected(env, bad: str) -> None:
    db, data_dir = env
    with pytest.raises(ValueError):
        recording_path(data_dir, bad)
    with pytest.raises(ValueError):
        delete_recording(db, data_dir, bad)
    with pytest.raises(ValueError):
        recording_file_for_read(data_dir, bad)


def test_delete_removes_file_rows_events_and_exports(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    info = _start(rec)
    db.add_event(LabeledEvent(session_id="sess_live", recording_id=info.recording_id, t_unix_ns=T0,
                              kind=EventKind.MARK, label="door"))
    with pytest.raises(RecordingRefused) as ei:
        delete_recording(db, data_dir, info.recording_id)  # still recording
    assert ei.value.code == "RECORDING_ACTIVE"
    rec.write(make_frame())
    rec.stop()
    path = recording_path(data_dir, info.recording_id)
    exports = data_dir / "exports"
    exports.mkdir()
    own_export = exports / f"{info.recording_id}.export.20260101T000000000000000Z.zip"
    other_export = exports / f"{info.recording_id}_x.export.20260101T000000000000000Z.zip"
    own_export.write_bytes(b"zip")
    other_export.write_bytes(b"zip")
    assert path.exists()
    assert delete_recording(db, data_dir, info.recording_id)
    assert not path.exists() and not own_export.exists() and other_export.exists()
    assert db.get_recording(info.recording_id) is None
    assert db.list_events(recording_id=info.recording_id) == []
    assert not delete_recording(db, data_dir, info.recording_id)  # nothing left


def test_symlinked_recording_is_never_followed(env, tmp_path: Path) -> None:
    db, data_dir = env
    outside = tmp_path / "outside.txt"
    outside.write_text("personal file")
    rdir = data_dir / "recordings"
    rdir.mkdir(parents=True)
    link = rdir / "rec_link.jsonl.gz"
    os.symlink(outside, link)
    assert total_recording_bytes(data_dir) == 0  # symlinks are not counted
    with pytest.raises(ValueError, match="symlink"):
        recording_file_for_read(data_dir, "rec_link")
    assert delete_recording(db, data_dir, "rec_link")
    assert not link.exists() and not link.is_symlink()
    assert outside.read_text() == "personal file"  # the target is untouched


def test_raw_lines_follow_storage_config(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir, keep_raw_lines=False)
    _start(rec)
    rec.write(make_frame(raw_line="RSCSI,1,abc"))
    info = rec.stop()
    assert info is not None
    assert [f.raw_line for f in iter_recording(recording_path(data_dir, info.recording_id))] == [None]

    rec2, _, _ = _recorder(db, data_dir, max_raw_line_chars=8)
    _start(rec2)
    rec2.write(make_frame(raw_line="RSCSI,1,0123456789"))
    info2 = rec2.stop()
    assert info2 is not None
    assert [f.raw_line for f in iter_recording(recording_path(data_dir, info2.recording_id))] == ["RSCSI,1,"]


def test_non_numeric_max_seconds_refused(env) -> None:
    db, data_dir = env
    rec, _, _ = _recorder(db, data_dir)
    with pytest.raises(RecordingRefused) as ei:
        _start(rec, max_seconds="ten")
    assert ei.value.code == "INVALID_ARGUMENT"
