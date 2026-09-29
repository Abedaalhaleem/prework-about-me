"""HTTP API: room geometry and calibration (quiet baseline, walk test, invalidation)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from roomsense.room import example_room
from roomsense.runtime import AppRuntime
from roomsense.schemas import CalibrationKind, CalibrationRecord, SourceMode
from tests.api_helpers import (  # noqa: F401  (autouse fixture)
    FAST_CALIBRATION,
    LINK,
    _no_api_token_in_env,
    api_client,
    make_cfg,
    wait_until,
)


def _stored_calibration(rt: AppRuntime, cal_id: str, room_hash: str) -> None:
    rt.db.add_calibration(CalibrationRecord(
        calibration_id=cal_id, kind=CalibrationKind.QUIET_BASELINE, created_at_unix_ns=time.time_ns(),
        session_id="sess_old", source_mode=SourceMode.LIVE, link_ids=[LINK], hardware_signature="hw-test",
        room_config_hash=room_hash, processing_config_version=rt.config_version, duration_s=60.0,
        frame_count=1500, window_count=100, valid=True))


def _calibrate_sim(c: TestClient) -> dict[str, Any]:
    r = c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True})
    assert r.status_code == 200
    time.sleep(0.4)
    r = c.post("/api/calibration/baseline/start", json={"confirm_room_empty": True})
    assert r.status_code == 200, r.text
    assert c.get("/api/calibration").json()["in_progress"] is not None
    time.sleep(2.2)
    r = c.post("/api/calibration/baseline/stop")
    assert r.status_code == 200, r.text
    return r.json()


def test_room_defaults_to_example(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        room = c.get("/api/room").json()
        assert room["provenance"] == "EXAMPLE" and "EXAMPLE" in room["name"]
        assert c.get("/api/room/example").json() == room
        assert {z["id"] for z in room["zones"]} == {"A", "B", "C"}
        assert len(room["links"]) == 3 and len(room["doors"]) == 1


def test_put_room_forces_user_provided_and_invalidates_calibrations(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    rt = AppRuntime(cfg)
    with api_client(cfg, rt) as c:
        _stored_calibration(rt, "cal-example-room", example_room().config_hash())

        room = c.get("/api/room/example").json()
        room["name"] = "My flat, bedroom"
        room["provenance"] = "EXAMPLE"  # whatever the client says, a saved room is USER_PROVIDED
        r = c.put("/api/room", json=room)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["room"]["provenance"] == "USER_PROVIDED"
        # Adopting the example geometry as the user's room changes the calibration-relevant
        # hash (it includes the provenance), so calibrations bound to the EXAMPLE are invalid.
        inv = body["invalidated"]
        assert inv["room_hash_changed"] is True and inv["calibrations"] == ["cal-example-room"]
        stored = rt.db.get_calibration("cal-example-room")
        assert stored is not None and not stored.record.valid
        assert stored.record.invalidated_reason.startswith("ROOM_CHANGED")
        saved = json.loads((cfg.storage.resolved_data_dir() / "room.json").read_text())
        assert saved["provenance"] == "USER_PROVIDED" and saved["name"] == "My flat, bedroom"
        assert c.get("/api/room").json()["provenance"] == "USER_PROVIDED"
        assert not any(n.startswith("ROOM_EXAMPLE") for n in c.get("/api/status").json()["notes"])

        user_hash = inv["room_hash"]
        _stored_calibration(rt, "cal-user-room", user_hash)
        room["name"] = "Bedroom"
        room["notes"] = "renamed only"
        inv = c.put("/api/room", json=room).json()["invalidated"]
        assert inv["room_hash_changed"] is False and inv["calibrations"] == []
        assert rt.db.get_calibration("cal-user-room").record.valid  # type: ignore[union-attr]

        room["nodes"][1]["position"]["x"] = 4.5  # a receiver moved: calibrations no longer apply
        inv = c.put("/api/room", json=room).json()["invalidated"]
        assert inv["room_hash_changed"] is True and inv["previous_room_hash"] == user_hash
        assert inv["calibrations"] == ["cal-user-room"]
        history = c.get("/api/calibration").json()["history"]
        assert {h["calibration_id"]: h["valid"] for h in history} == {"cal-example-room": False,
                                                                     "cal-user-room": False}
        assert c.get("/api/room/example").json()["provenance"] == "EXAMPLE"  # the sample is unchanged
    # The saved room is what a new runtime loads.
    with api_client(cfg) as c:
        room = c.get("/api/room").json()
        assert room["name"] == "Bedroom" and room["provenance"] == "USER_PROVIDED"


def test_active_baseline_is_invalidated_by_room_change(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, **FAST_CALIBRATION)
    with api_client(cfg) as c:
        rec = _calibrate_sim(c)
        assert rec["valid"] is True, rec
        assert rec["source_mode"] == "SIMULATION" and rec["kind"] == "QUIET_BASELINE"
        assert rec["room_config_hash"] == example_room().config_hash()
        st = c.get("/api/status").json()
        assert st["calibration_valid"] is True and st["calibration"]["calibration_id"] == rec["calibration_id"]
        overview = c.get("/api/calibration").json()
        assert overview["active"]["calibration_id"] == rec["calibration_id"] and overview["in_progress"] is None

        room = c.get("/api/room").json()
        room["zones"][0]["label"] = "Desk"  # zones are part of the calibration-relevant hash
        inv = c.put("/api/room", json=room).json()["invalidated"]
        assert inv["room_hash_changed"] is True and inv["active_baselines_invalidated"] is True
        assert rec["calibration_id"] in inv["calibrations"]
        st = c.get("/api/status").json()
        assert st["calibration_valid"] is False and st["calibration"] is None
        assert st["calibration_detail"].startswith("ROOM_CHANGED")
        assert wait_until(lambda: all(a["state"] in ("UNKNOWN", "SENSOR_OFFLINE", "CALIBRATING")
                                      for a in c.get("/api/status").json()["activity"]), 3)


def test_baseline_needs_confirmation_and_a_source(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/calibration/baseline/start", json={"confirm_room_empty": True})
        assert r.status_code == 409 and r.json()["code"] == "NO_SOURCE"
        c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True})
        for body in ({}, {"confirm_room_empty": False}):
            r = c.post("/api/calibration/baseline/start", json=body)
            assert r.status_code == 422 and r.json()["code"] == "ROOM_NOT_CONFIRMED_EMPTY"
        assert c.post("/api/calibration/baseline/start", json={"confirm_room_empty": "yes"}).status_code == 422
        r = c.post("/api/calibration/baseline/start", json={"confirm_room_empty": True, "link_ids": ["tx9->rx9"]})
        assert r.status_code == 422 and r.json()["code"] == "UNKNOWN_LINK"
        r = c.post("/api/calibration/walk-test/start", json={"link_ids": ["tx9->rx9"]})
        assert r.status_code == 422 and r.json()["code"] == "UNKNOWN_LINK"
        r = c.post("/api/calibration/baseline/stop")
        assert r.status_code == 409 and r.json()["code"] == "NOT_CALIBRATING"
        assert c.post("/api/calibration/baseline/cancel").json() == {"cancelled": False}


def test_too_short_baseline_is_rejected_with_reasons(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:  # default: 60 s minimum
        c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True})
        time.sleep(0.3)
        assert c.post("/api/calibration/baseline/start", json={"confirm_room_empty": True}).status_code == 200
        time.sleep(0.8)
        rec = c.post("/api/calibration/baseline/stop").json()
        assert rec["valid"] is False
        assert rec["invalidated_reason"].startswith("BASELINE_REJECTED")
        per_link = rec["summary"]["per_link"][LINK]
        assert per_link["accepted"] is False and per_link["reasons"]
        st = c.get("/api/status").json()
        assert st["calibration_valid"] is False
        assert st["calibration_detail"].startswith("BASELINE_REJECTED")


def test_walk_test_and_invalidate(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, **FAST_CALIBRATION)
    with api_client(cfg) as c:
        r = c.post("/api/calibration/walk-test/stop")
        assert r.status_code == 409 and r.json()["code"] == "NO_WALK_TEST"
        rec = _calibrate_sim(c)
        assert rec["valid"] is True
        assert c.post("/api/calibration/walk-test/start", json={}).status_code == 200
        assert c.get("/api/calibration").json()["walk_test_active"] is True
        time.sleep(0.8)
        report = c.post("/api/calibration/walk-test/stop").json()
        assert report["source_mode"] == "SIMULATION"
        assert report["links"][0]["link_id"] == LINK and report["links"][0]["windows"] > 0
        assert "does not establish behind-wall performance" in report["note"]
        assert c.get("/api/calibration").json()["last_walk_test"]["session_id"] == report["session_id"]

        assert c.post("/api/calibration/invalidate", json={"reason": ""}).status_code == 422
        r = c.post("/api/calibration/invalidate", json={"reason": "moved the router"})
        assert r.status_code == 200 and r.json()["invalidated"] == [rec["calibration_id"]]
        st = c.get("/api/status").json()
        assert st["calibration_valid"] is False
        assert st["calibration_detail"] == "INVALIDATED_BY_OPERATOR: moved the router"
