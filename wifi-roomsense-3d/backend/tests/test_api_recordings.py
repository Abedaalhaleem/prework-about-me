"""HTTP API: consented recordings, export, deletion and labelled events."""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from roomsense.storage.models import CONSENT_STATEMENT_V1
from tests.api_helpers import CONSENT, _no_api_token_in_env, api_client, make_cfg  # noqa: F401  (autouse fixture)


def _start_sim(c: TestClient) -> dict[str, Any]:
    r = c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True})
    assert r.status_code == 200, r.text
    return r.json()


def _record(c: TestClient, seconds: float = 0.8, **extra: Any) -> dict[str, Any]:
    r = c.post("/api/recordings/start", json={"consent": CONSENT, "label": "sim capture", **extra})
    assert r.status_code == 200, r.text
    time.sleep(seconds)
    r = c.post("/api/recordings/stop")
    assert r.status_code == 200, r.text
    return r.json()


def test_consent_statement_endpoint(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        body = c.get("/api/recordings/consent-statement").json()
        assert body == {"version": "consent-v1", "text": CONSENT_STATEMENT_V1}


def test_recording_requires_a_source_and_valid_consent(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with api_client(cfg) as c:
        r = c.post("/api/recordings/start", json={"consent": CONSENT, "label": "x"})
        assert r.status_code == 409 and r.json()["code"] == "NO_SOURCE"
        _start_sim(c)
        bad_consents = [
            {**CONSENT, "all_participants_consented": False},
            {**CONSENT, "all_participants_consented": "true"},  # only a literal true counts
            {**CONSENT, "participant_count": 0},
            {**CONSENT, "purpose": "   "},
            {**CONSENT, "statement_version": "consent-v0"},
            {**CONSENT, "statement_text": "I agree to something else"},  # the server supplies the text
            {k: v for k, v in CONSENT.items() if k != "all_participants_consented"},
        ]
        for consent in bad_consents:
            r = c.post("/api/recordings/start", json={"consent": consent, "label": "x"})
            assert r.status_code in (400, 422), (consent, r.text)
        r = c.post("/api/recordings/start", json={"label": "no consent at all"})
        assert r.status_code == 422
        assert c.get("/api/recordings").json() == []
        assert c.get("/api/status").json()["recording_active"] is False
        rec_dir = cfg.storage.resolved_data_dir() / "recordings"
        assert not rec_dir.exists() or not any(rec_dir.iterdir())


def test_record_list_export_delete(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with api_client(cfg) as c:
        sim = _start_sim(c)
        r = c.post("/api/recordings/start", json={"consent": CONSENT, "label": "sim capture", "notes": "test"})
        assert r.status_code == 200
        info = r.json()
        assert info["status"] == "RECORDING" and info["synthetic"] is True
        assert info["session_id"] == sim["session_id"] and info["source_mode"] == "SIMULATION"
        st = c.get("/api/status").json()
        assert st["recording_active"] is True and st["recording_id"] == info["recording_id"]
        r = c.post("/api/recordings/start", json={"consent": CONSENT, "label": "second"})
        assert r.status_code == 409 and r.json()["code"] == "ALREADY_RECORDING"
        time.sleep(0.8)
        done = c.post("/api/recordings/stop").json()
        assert done["status"] == "COMPLETE" and done["frames"] > 5 and done["original_source_mode"] == "SIMULATION"
        r = c.post("/api/recordings/stop")
        assert r.status_code == 409 and r.json()["code"] == "NOT_RECORDING"
        rid = done["recording_id"]
        assert [x["recording_id"] for x in c.get("/api/recordings").json()] == [rid]

        r = c.get(f"/api/recordings/{rid}/export")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert f"roomsense-{rid}.zip" in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert set(zf.namelist()) == {"manifest.json", "frames.jsonl.gz", "events.csv", "PROVENANCE.txt"}
            assert "SIMULATION" in zf.read("PROVENANCE.txt").decode()
        assert c.get("/api/recordings/rec_missing/export").status_code == 404

        path = cfg.storage.resolved_data_dir() / "recordings" / f"{rid}.jsonl.gz"
        assert path.is_file()
        r = c.delete(f"/api/recordings/{rid}")
        assert r.status_code == 200 and r.json() == {"deleted": True}
        assert not path.exists()
        assert not list((cfg.storage.resolved_data_dir() / "exports").glob(f"{rid}*"))
        assert c.get("/api/recordings").json() == []
        assert c.delete(f"/api/recordings/{rid}").status_code == 404
        assert c.delete("/api/recordings/bad.id").status_code == 422


def test_active_recording_cannot_be_deleted_and_switch_finalises_it(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        _start_sim(c)
        rid = c.post("/api/recordings/start", json={"consent": CONSENT, "label": "x"}).json()["recording_id"]
        r = c.delete(f"/api/recordings/{rid}")
        assert r.status_code == 409 and r.json()["code"] == "RECORDING_ACTIVE"
        time.sleep(0.3)
        c.post("/api/source/stop")
        rec = c.get("/api/recordings").json()[0]
        assert rec["recording_id"] == rid and rec["status"] == "COMPLETE"


def test_recording_stops_at_max_seconds_and_says_so(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        _start_sim(c)
        r = c.post("/api/recordings/start", json={"consent": CONSENT, "label": "x", "max_seconds": 0.3})
        assert r.status_code == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and c.get("/api/status").json()["recording_active"]:
            time.sleep(0.05)
        st = c.get("/api/status").json()
        assert st["recording_active"] is False
        assert any(n.startswith("RECORDING_STOPPED") and "TRUNCATED_LIMIT" in n for n in st["notes"])


def test_labelled_events(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/events", json={"label": "MOVING", "kind": "START"})
        assert r.status_code == 409  # needs an active session
        sim = _start_sim(c)
        r = c.post("/api/events", json={"label": "MOVING", "kind": "START", "notes": "walk"})
        assert r.status_code == 200, r.text
        ev = r.json()
        assert ev["session_id"] == sim["session_id"] and ev["kind"] == "START" and ev["t_unix_ns"] > 0
        r = c.post("/api/events", json={"label": "MOVING", "kind": "END", "t_unix_ns": ev["t_unix_ns"] + 10**9})
        assert r.status_code == 200
        events = c.get("/api/events").json()
        assert [e["kind"] for e in events] == ["START", "END"]
        assert c.get("/api/events", params={"session_id": "other_session"}).json() == []
        assert c.post("/api/events", json={"label": "  ", "kind": "MARK"}).status_code == 422
