"""Zip export of one recording: exact contents, provenance, no unrelated files."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from roomsense import PARSER_VERSION, SCHEMA_VERSION
from roomsense.schemas import SourceMode
from roomsense.storage.db import Database
from roomsense.storage.exports import ExportError, export_recording
from roomsense.storage.models import EventKind, LabeledEvent
from roomsense.storage.recordings import Recorder, recording_path

from .storage_helpers import NS, T0, make_consent, make_frame, storage_cfg


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        self.t += NS  # every call one second later: gives the recording a span
        return self.t


@pytest.fixture
def env(tmp_path: Path):
    db = Database(tmp_path / "meta.sqlite3")
    data_dir = tmp_path / "data"
    yield db, data_dir, Recorder(db, data_dir, storage_cfg(), wall_clock_ns=Clock(T0))
    db.close()


def _record(rec: Recorder, db: Database, *, session_id: str = "sess_live", mode: SourceMode = SourceMode.LIVE,
            synthetic: bool = False, n: int = 5):
    info = rec.start(consent=make_consent(purpose="testing exports", participant_count=2), label="export test",
                     scenario=None, session_id=session_id, source_mode=mode, link_ids=["tx1->rx1"],
                     config_version="cfg-1", notes="note")
    db.add_event(LabeledEvent(session_id=session_id, recording_id=info.recording_id, t_unix_ns=T0 + 2 * NS,
                              kind=EventKind.START, label="=HYPERLINK(\"http://x\")", notes="+1 formula"))
    db.add_event(LabeledEvent(session_id=session_id, t_unix_ns=T0 + 3 * NS, kind=EventKind.END, label="MOVING"))
    db.add_event(LabeledEvent(session_id="sess_elsewhere", t_unix_ns=T0 + 3 * NS, kind=EventKind.MARK,
                              label="other session"))
    for i in range(n):
        rec.write(make_frame(session_id=session_id, source_mode=mode, counter=i, synthetic=synthetic))
    rec.write_event({"kind": "DISCONNECTED", "link_id": "tx1->rx1", "receiver_id": "rx1", "detail": "@unplugged"})
    done = rec.stop()
    assert done is not None
    return done


def test_export_has_exactly_the_documented_entries(env) -> None:
    db, data_dir, rec = env
    info = _record(rec, db)
    other = _record(rec, db, session_id="sess_two")
    (data_dir / "room.json").write_text('{"private": true}')
    (data_dir / "recordings" / "stray.txt").write_text("unrelated")

    zpath = export_recording(db, data_dir, info.recording_id)
    assert zpath.parent == (data_dir / "exports").resolve()
    assert zpath.name.startswith(f"{info.recording_id}.export.") and zpath.suffix == ".zip"
    with zipfile.ZipFile(zpath) as zf:
        assert sorted(zf.namelist()) == ["PROVENANCE.txt", "events.csv", "frames.jsonl.gz", "manifest.json"]
        frames_bytes = zf.read("frames.jsonl.gz")
        manifest = json.loads(zf.read("manifest.json"))
        events = list(csv.DictReader(io.StringIO(zf.read("events.csv").decode("utf-8"))))
        provenance = zf.read("PROVENANCE.txt").decode("utf-8")
    original = recording_path(data_dir, info.recording_id).read_bytes()
    assert frames_bytes == original
    assert manifest["files"]["frames.jsonl.gz"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert other.recording_id not in json.dumps(manifest)

    assert manifest["schema_version"] == SCHEMA_VERSION and manifest["parser_version"] == PARSER_VERSION
    assert manifest["file_header"]["format"] == "roomsense-recording-v1"
    assert manifest["recording"]["recording_id"] == info.recording_id and manifest["recording"]["frames"] == 5
    assert manifest["consent"] == {
        "consent_id": info.consent_id, "created_at_unix_ns": manifest["consent"]["created_at_unix_ns"],
        "all_participants_consented": True, "participant_count": 2, "purpose": "testing exports",
        "statement_version": "consent-v1",
    }  # no statement text or other free text beyond the purpose
    assert manifest["provenance"]["source_mode"] == "LIVE" and manifest["provenance"]["synthetic"] is False
    assert manifest["validation_evidence"]["validated_sensing"] is False
    assert "Nothing in this export is validated sensing" in provenance
    assert "LIVE" in provenance and manifest["provenance"]["statement"] == provenance

    labels = [e for e in events if e["source"] == "label"]
    links = [e for e in events if e["source"] == "link"]
    assert [e["label"] for e in labels] == ["'=HYPERLINK(\"http://x\")", "MOVING"]  # formula neutralised
    assert labels[0]["notes"] == "'+1 formula"
    assert all(e["label"] != "other session" for e in events)
    assert len(links) == 1 and links[0]["kind"] == "DISCONNECTED" and links[0]["detail"] == "'@unplugged"
    assert manifest["counts"] == {"label_events": 2, "link_events": 1, "link_events_truncated": False}


def test_synthetic_export_is_labelled(env) -> None:
    db, data_dir, rec = env
    info = _record(rec, db, session_id="sess_sim", mode=SourceMode.SIMULATION, synthetic=True)
    with zipfile.ZipFile(export_recording(db, data_dir, info.recording_id)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        provenance = zf.read("PROVENANCE.txt").decode("utf-8")
    assert "SIMULATION / SYNTHETIC DATA" in provenance and "never be used as validation evidence" in provenance
    assert manifest["provenance"]["banner"] == "SIMULATION / SYNTHETIC DATA"
    assert manifest["validation_evidence"]["usable_as_validation_evidence"] is False


def test_reexport_replaces_previous_export_only_for_that_recording(env) -> None:
    db, data_dir, rec = env
    a = _record(rec, db)
    b = _record(rec, db, session_id="sess_b")
    first_a = export_recording(db, data_dir, a.recording_id, now_ns=T0)
    export_b = export_recording(db, data_dir, b.recording_id, now_ns=T0)
    second_a = export_recording(db, data_dir, a.recording_id, now_ns=T0 + 1)
    assert first_a != second_a and not first_a.exists() and second_a.exists() and export_b.exists()
    assert not any(p.name.endswith(".partial") for p in (data_dir / "exports").iterdir())


def test_export_refusals(env, tmp_path: Path) -> None:
    db, data_dir, rec = env
    with pytest.raises(ValueError):
        export_recording(db, data_dir, "../etc")
    with pytest.raises(ExportError, match="not found"):
        export_recording(db, data_dir, "rec_missing")
    active = rec.start(consent=make_consent(), label="x", session_id="sess_live", source_mode=SourceMode.LIVE)
    with pytest.raises(ExportError, match="stop"):
        export_recording(db, data_dir, active.recording_id)
    rec.stop()
    # The file was replaced by a symlink to something outside the data dir.
    path = recording_path(data_dir, active.recording_id)
    path.unlink()
    outside = tmp_path / "secret.txt"
    outside.write_text("personal")
    os.symlink(outside, path)
    with pytest.raises(ExportError, match="symlink"):
        export_recording(db, data_dir, active.recording_id)
    path.unlink()
    with pytest.raises(ExportError, match="not found"):
        export_recording(db, data_dir, active.recording_id)


def test_interrupted_recording_exports_what_is_readable(env) -> None:
    db, data_dir, rec = env
    info = rec.start(consent=make_consent(), label="crash test", session_id="sess_live", source_mode=SourceMode.LIVE)
    # Nothing flushed yet: the file holds only a partial gzip stream.
    Recorder(db, data_dir, storage_cfg())  # restart marks the row ERROR
    with pytest.raises(ExportError, match="unreadable"):
        export_recording(db, data_dir, info.recording_id)
    # After enough frames the compressor has flushed complete records.
    for i in range(3000):
        rec.write(make_frame(counter=i))
    zpath = export_recording(db, data_dir, info.recording_id)
    with zipfile.ZipFile(zpath) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        provenance = zf.read("PROVENANCE.txt").decode("utf-8")
    assert manifest["recording"]["status"] == "ERROR"
    assert "interrupted" in manifest["recording"]["stop_reason"] and "ERROR" in provenance
    rec.stop()  # the original writer can still be closed cleanly
