"""Source transitions through the HTTP API: simulation -> replay -> live -> stop.

The live source reads from a fake serial port emitting hand-made firmware
lines (tests.api_helpers.PacedSerial). Checks: every switch creates a new
session, the provenance of every result matches the active source, and
nothing (results, simulated flags, calibrations) is carried over.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from roomsense.runtime import AppRuntime
from roomsense.schemas import SourceMode
from tests.api_helpers import (  # noqa: F401  (autouse fixture)
    CONSENT,
    LINK,
    PacedSerialFactory,
    _no_api_token_in_env,
    api_client,
    make_cfg,
    receiver_cfg,
    wait_until,
)


def _status(c: TestClient) -> dict[str, Any]:
    r = c.get("/api/status")
    assert r.status_code == 200
    return r.json()


def _activity_for(c: TestClient, sid: str) -> list[dict[str, Any]]:
    return [a for a in _status(c)["activity"] if a["provenance"]["session_id"] == sid]


def _cap(st: dict[str, Any], cap: str) -> str:
    return next(x["state"] for x in st["capabilities"] if x["capability"] == cap)


def test_simulation_replay_live_transitions(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    rt = AppRuntime(cfg, serial_factory=PacedSerialFactory(rate_hz=50))
    with api_client(cfg, rt) as c:
        # 1. SIMULATION (explicitly acknowledged)
        st = c.post("/api/source/simulation",
                    json={"scenario": "quiet_only", "seed": 3, "acknowledge_simulated": True}).json()
        sim_sid = st["session_id"]
        assert st["source_mode"] == "SIMULATION" and st["simulated"] is True and st["source_banner"] == "SIMULATION"
        assert wait_until(lambda: len(_activity_for(c, sim_sid)) > 0, 5)
        r = c.post("/api/recordings/start", json={"consent": CONSENT, "label": "simulated capture"})
        assert r.status_code == 200, r.text
        time.sleep(1.2)
        info = c.post("/api/recordings/stop").json()
        assert info["synthetic"] is True and info["source_mode"] == "SIMULATION" and info["frames"] > 10

        # 2. REPLAY of that recording: new session, still flagged simulated, nothing carried over
        st = c.post("/api/source/replay", json={"recording_id": info["recording_id"], "speed": 1.0}).json()
        rep_sid = st["session_id"]
        assert rep_sid != sim_sid
        assert st["source_mode"] == "REPLAY" and st["source_banner"] == "RECORDED REPLAY"
        assert st["simulated"] is True
        assert st["recording_active"] is False  # switching the source stops any recording
        assert all(a["provenance"]["session_id"] == rep_sid for a in st["activity"])
        assert wait_until(lambda: len(_activity_for(c, rep_sid)) > 0, 5)
        st = _status(c)
        assert all(a["provenance"]["session_id"] == rep_sid and a["provenance"]["source_mode"] == "REPLAY"
                   for a in st["activity"])

        # 3. LIVE (fake serial port): new session, not simulated, only LIVE results
        st = c.post("/api/source/live", json={}).json()
        live_sid = st["session_id"]
        assert live_sid not in (sim_sid, rep_sid)
        assert st["source_mode"] == "LIVE" and st["simulated"] is False and st["source_banner"] == "LIVE MEASUREMENTS"
        assert all(a["provenance"]["session_id"] == live_sid for a in st["activity"])
        assert st["calibration"] is None and st["calibration_valid"] is False
        assert wait_until(lambda: len(_activity_for(c, live_sid)) > 0
                          and _cap(_status(c), "A_ACQUISITION") == "ENABLED", 6)
        st = _status(c)
        assert st["hardware_required"] is False
        assert all(a["provenance"]["source_mode"] == "LIVE" for a in st["activity"])
        assert all(a["state"] != "MOTION_DETECTED" and a["state"] != "NO_MOTION_DETECTED" for a in st["activity"])
        sig = c.get("/api/signal", params={"link_id": LINK, "seconds": 30}).json()
        assert sig["source_mode"] == "LIVE" and sig["link_id"] == LINK

        # 4. STOP: no source, no results shown from the finished session
        st = c.post("/api/source/stop").json()
        assert st["source_banner"] == "NO SOURCE" and st["session_id"] is None and st["activity"] == []
        assert st["simulated"] is False
        assert c.get("/api/signal", params={"link_id": LINK}).status_code == 404

        # Every session is recorded once, with the right mode, and was closed.
        for sid, mode in ((sim_sid, SourceMode.SIMULATION), (rep_sid, SourceMode.REPLAY),
                          (live_sid, SourceMode.LIVE)):
            sess = rt.db.get_session(sid)
            assert sess is not None and sess.source_mode == mode and sess.ended_at_unix_ns is not None
            rows = rt.db.list_activity(session_id=sid)
            assert rows and {row.source_mode for row in rows} == {mode}
    assert rt.closed
