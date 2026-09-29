"""HTTP API: health, default status, capabilities, scenarios, ports, errors, static UI."""

from __future__ import annotations

from pathlib import Path

import pytest

import roomsense.api.routes_source as routes_source
import roomsense.api.routes_status as routes_status
from roomsense import SCHEMA_VERSION, __version__
from roomsense.capabilities import UNSUPPORTED_CAPABILITIES
from roomsense.schemas import SystemStatus
from tests.api_helpers import _no_api_token_in_env, api_client, make_cfg  # noqa: F401  (autouse fixture)


def test_health(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert body["schema_version"] == SCHEMA_VERSION
        assert body["uptime_s"] >= 0
        assert body["source_state"] == "NO_SOURCE"
        assert body["bind_host"] == "127.0.0.1"
        assert isinstance(body["websocket_support"], bool)


def test_default_status_is_honest(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with api_client(cfg) as c:
        r = c.get("/api/status")
        assert r.status_code == 200
        st = SystemStatus.model_validate(r.json())
        assert st.source_mode is None and st.source_banner == "NO SOURCE"
        assert st.simulated is False
        assert st.source_state.value == "NO_SOURCE"
        assert st.session_id is None
        assert st.hardware_required is True
        assert st.zone.state.value == "DISABLED" and st.zone.zone_id is None
        assert st.localization_status == "DISABLED: no zone model has passed the predefined criteria"
        assert st.pose.enabled is False and st.pose.label == "EXPERIMENTAL"
        assert st.through_wall_status == "UNVERIFIED"
        assert st.calibration is None and st.calibration_valid is False
        assert st.activity == [] and st.links == []
        assert st.recording_active is False
        assert st.stale_clear_timeout_s == cfg.detection.clear_stale_after_s
        states = {c.capability.value: c.state.value for c in st.capabilities}
        assert states["A_ACQUISITION"] == "HARDWARE_REQUIRED"
        assert all(v != "ENABLED" for v in states.values())
        assert [u.id for u in st.unsupported_capabilities] == [u["id"] for u in UNSUPPORTED_CAPABILITIES]
        assert any(n.startswith("ROOM_EXAMPLE") for n in st.notes)


def test_capability_endpoints(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        unsupported = c.get("/api/capabilities/unsupported").json()
        assert unsupported == [dict(u) for u in UNSUPPORTED_CAPABILITIES]
        caps = c.get("/api/capabilities").json()
        assert [x["capability"] for x in caps] == ["A_ACQUISITION", "B_MOTION", "C_ZONE", "D_POSE"]


def test_simulation_scenarios_are_labelled_simulation(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        scns = c.get("/api/simulation/scenarios").json()
        names = {s["name"] for s in scns}
        assert {"demo_walk", "quiet_only", "disconnect", "zones_3rx"} <= names
        for s in scns:
            assert s["duration_s"] > 0
            assert "SIMULATION" in s["description"]
            assert s["simulated"] is True


def test_serial_ports_are_listed_not_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = [{"device": "/dev/ttyUSB7", "description": "CP2102", "hwid": "USB VID:PID=10C4:EA60", "vid": 0x10C4,
             "pid": 0xEA60, "likely_usb_uart_bridge": True}]
    monkeypatch.setattr(routes_status, "list_serial_ports", lambda: fake)
    with api_client(make_cfg(tmp_path)) as c:
        assert c.get("/api/serial/ports").json() == fake

    def broken() -> list[dict[str, object]]:
        raise OSError("no backend")

    monkeypatch.setattr(routes_status, "list_serial_ports", broken)
    with api_client(make_cfg(tmp_path)) as c:
        r = c.get("/api/serial/ports")
        assert r.status_code == 503 and r.json()["detail"].startswith("SERIAL_ENUMERATION_FAILED")


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/source/simulation", "{not json"),
        ("/api/source/simulation", '{"scenario": 5, "acknowledge_simulated": true}'),
        ("/api/source/simulation", '{"scenario": "demo_walk", "acknowledge_simulated": true, "extra": 1}'),
        ("/api/source/replay", '{"recording_id": "../../etc/passwd"}'),
        ("/api/recordings/start", '{"label": "x"}'),
        ("/api/events", '{"label": "x", "kind": "SOMETIMES"}'),
        ("/api/calibration/invalidate", '{"reason": ""}'),
        ("/api/validation/runs", '{"scenario_id": "a/b"}'),
        ("/api/zone/train", '{"sessions": []}'),
        ("/api/source/live", '{"receivers": [{"receiver_id": "rx 1", "port": "/dev/x"}]}'),
    ],
)
def test_malformed_bodies_are_422(tmp_path: Path, path: str, body: str) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post(path, content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, r.text
        assert c.get("/api/status").json()["source_banner"] == "NO SOURCE"


def test_malformed_room_is_422(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        room = c.get("/api/room/example").json()
        room["doors"][0]["wall_id"] = "no_such_wall"
        assert c.put("/api/room", json=room).status_code == 422
        assert c.put("/api/room", content="[", headers={"Content-Type": "application/json"}).status_code == 422


def test_unknown_api_route_is_json_404_and_errors_do_not_leak(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.get("/api/does-not-exist")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")


def test_security_headers_and_no_store(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.get("/api/health")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
        assert c.get("/api/openapi.json").status_code == 200


def test_placeholder_page_without_built_frontend(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path), frontend_dist=tmp_path / "no-dist") as c:
        r = c.get("/")
        assert r.status_code == 200 and "npm run build" in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        # The interactive docs would load scripts from a CDN; they are disabled.
        assert c.get("/docs").status_code == 404


def test_frontend_is_served_with_spa_fallback_and_no_traversal(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>RoomSense UI</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('ui')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside dist", encoding="utf-8")
    with api_client(make_cfg(tmp_path), frontend_dist=dist) as c:
        r = c.get("/")
        assert r.status_code == 200 and "RoomSense UI" in r.text
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert c.get("/assets/app.js").text == "console.log('ui')"
        assert "RoomSense UI" in c.get("/calibration/room").text  # client-side route
        for probe in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
            assert "outside dist" not in c.get(probe).text
        assert c.get("/api/nope").status_code == 404  # API paths never fall back to the UI


def test_live_start_without_configured_receivers_is_409(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/source/live", json={})
        assert r.status_code == 409
        assert r.json()["code"] == "NO_RECEIVERS_CONFIGURED"
        r = c.post("/api/source/live")  # no body at all
        assert r.status_code == 409


def test_live_start_refuses_ports_that_are_not_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_source, "list_serial_ports", lambda: [])
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/source/live", json={"receivers": [{"receiver_id": "rx1", "port": "/dev/sda"}]})
        assert r.status_code == 422
        assert r.json()["detail"].startswith("PORT_NOT_AVAILABLE")
        assert c.get("/api/status").json()["source_banner"] == "NO SOURCE"
