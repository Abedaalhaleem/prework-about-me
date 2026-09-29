"""HTTP API: source selection (simulation, replay, live, stop) and signals."""

from __future__ import annotations

from pathlib import Path

from roomsense.runtime import AppRuntime
from tests.api_helpers import (  # noqa: F401  (autouse fixture)
    LINK,
    MissingPortFactory,
    PacedSerialFactory,
    _no_api_token_in_env,
    api_client,
    make_cfg,
    receiver_cfg,
    wait_until,
)


def test_simulation_requires_explicit_acknowledgement(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        for body in ({"scenario": "demo_walk"},
                     {"scenario": "demo_walk", "acknowledge_simulated": False}):
            r = c.post("/api/source/simulation", json=body)
            assert r.status_code == 422
            assert r.json()["code"] == "SIMULATION_NOT_ACKNOWLEDGED"
        for sloppy in ("true", "yes", 1):  # only a literal JSON true counts
            r = c.post("/api/source/simulation", json={"scenario": "demo_walk", "acknowledge_simulated": sloppy})
            assert r.status_code == 422
        st = c.get("/api/status").json()
        assert st["source_banner"] == "NO SOURCE" and st["simulated"] is False and st["session_id"] is None


def test_simulation_start_status_and_stop(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/source/simulation", json={"scenario": "quiet_only", "seed": 11,
                                                   "acknowledge_simulated": True})
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["source_mode"] == "SIMULATION" and st["source_banner"] == "SIMULATION"
        assert st["simulated"] is True and st["session_id"].startswith("sim_")
        assert "SIMULATED DATA" in st["source_detail"]
        assert any(n.startswith("SIMULATED_DATA") for n in st["notes"])
        caps = {x["capability"]: x for x in st["capabilities"]}
        assert caps["A_ACQUISITION"]["state"] == "HARDWARE_REQUIRED"
        assert caps["C_ZONE"]["state"] == "DISABLED"
        assert wait_until(lambda: len(c.get("/api/status").json()["activity"]) > 0, 5)
        st = c.get("/api/status").json()
        assert all(a["provenance"]["source_mode"] == "SIMULATION" for a in st["activity"])
        assert all(a["calibrated_probability"] is None for a in st["activity"])
        # No baseline yet: nothing may claim "no motion".
        assert all(a["state"] in ("UNKNOWN", "SENSOR_OFFLINE") for a in st["activity"])

        sig = c.get("/api/signal", params={"link_id": LINK, "seconds": 10})
        assert sig.status_code == 200
        snap = sig.json()
        assert snap["source_mode"] == "SIMULATION" and snap["link_id"] == LINK
        assert set(snap) >= {"score", "enter_threshold", "exit_threshold", "rate_hz", "rssi_dbm", "amplitude",
                             "latest_profile", "gaps"}
        assert c.get("/api/signal", params={"link_id": "tx9->rx9"}).status_code == 404
        assert c.get("/api/signal", params={"link_id": LINK, "seconds": 0}).status_code == 422

        st = c.post("/api/source/stop").json()
        assert st["source_banner"] == "NO SOURCE" and st["session_id"] is None and st["activity"] == []


def test_unknown_scenario_and_bad_seed(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/source/simulation", json={"scenario": "nope", "acknowledge_simulated": True})
        assert r.status_code == 404 and r.json()["code"] == "UNKNOWN_SCENARIO"
        r = c.post("/api/source/simulation", json={"scenario": "demo_walk", "seed": -1, "acknowledge_simulated": True})
        assert r.status_code == 422


def test_replay_errors(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/source/replay", json={"recording_id": "rec_does_not_exist"})
        assert r.status_code == 404 and r.json()["code"] == "RECORDING_NOT_FOUND"
        assert c.post("/api/source/replay", json={"recording_id": "a.b"}).status_code == 422
        assert c.post("/api/source/replay", json={"recording_id": "rec_x", "speed": 0}).status_code == 422
        assert c.post("/api/source/replay", json={"recording_id": "rec_x", "speed": 5000}).status_code == 422


def test_live_start_with_configured_receiver(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    rt = AppRuntime(cfg, serial_factory=PacedSerialFactory(rate_hz=50))
    with api_client(cfg, rt) as c:
        r = c.post("/api/source/live", json={})
        assert r.status_code == 200
        st = r.json()
        assert st["source_mode"] == "LIVE" and st["source_banner"] == "LIVE MEASUREMENTS"
        assert st["simulated"] is False and st["session_id"].startswith("live_")
        assert "/dev/fake-rx1" in st["source_detail"]
        assert wait_until(lambda: c.get("/api/status").json()["source_state"] == "RUNNING", 5)
        assert wait_until(lambda: c.get("/api/status").json()["hardware_required"] is False, 5)
        links = c.get("/api/status").json()["links"]
        assert links[0]["link_id"] == LINK and links[0]["layout_id"] is not None


def test_live_with_missing_board_reports_disconnected(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    rt = AppRuntime(cfg, serial_factory=MissingPortFactory())
    with api_client(cfg, rt) as c:
        assert c.post("/api/source/live", json={}).status_code == 200
        assert wait_until(lambda: c.get("/api/status").json()["source_state"] == "DISCONNECTED", 5)
        assert wait_until(lambda: [a["state"] for a in c.get("/api/status").json()["activity"]] == ["SENSOR_OFFLINE"],
                          5)
        st = c.get("/api/status").json()
        assert st["source_mode"] == "LIVE" and st["simulated"] is False
        assert st["hardware_required"] is True


def test_hardware_required_is_not_sticky(tmp_path: Path) -> None:
    """hardware_required = NOT (LIVE and a link delivered documented-layout frames
    within stale_after_s): a live session earlier in the process does not clear it."""
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    rt = AppRuntime(cfg, serial_factory=PacedSerialFactory(rate_hz=50))
    with api_client(cfg, rt) as c:
        assert c.get("/api/status").json()["hardware_required"] is True  # no source
        assert c.post("/api/source/live", json={}).status_code == 200
        assert wait_until(lambda: c.get("/api/status").json()["hardware_required"] is False, 5)
        st = c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True}).json()
        assert st["hardware_required"] is True
        assert wait_until(lambda: len(c.get("/api/status").json()["activity"]) > 0, 5)
        st = c.get("/api/status").json()
        assert st["source_mode"] == "SIMULATION" and st["hardware_required"] is True
        assert c.post("/api/source/stop").json()["hardware_required"] is True


def test_configured_receivers_endpoint(tmp_path: Path) -> None:
    full = {"receiver_id": "rx1", "port": "/dev/ttyUSB0", "baud": 115200, "input_format": "roomsense-rscsi-v1",
            "transmitter_id": "tx1", "transmitter_mac": "1a:00:00:00:00:00", "declared_chip": "esp32s3",
            "declared_board": "ESP32-S3-DevKitC-1U", "allow_undocumented_layout_assumption": True}
    upstream = {"receiver_id": "rx_router", "port": "COM5", "input_format": "esp-csi-upstream-classic-v1",
                "transmitter_id": "router", "declared_chip": "esp32", "ltf_config": "lltf_only"}
    with api_client(make_cfg(tmp_path, acquisition={"receivers": [full, upstream]})) as c:
        r = c.get("/api/source/receivers")
        assert r.status_code == 200
        assert r.json() == [
            {"receiver_id": "rx1", "port": "/dev/ttyUSB0", "baud": 115200, "input_format": "roomsense-rscsi-v1",
             "transmitter_id": "tx1", "transmitter_mac": "1a:00:00:00:00:00", "declared_chip": "esp32s3",
             "declared_board": "ESP32-S3-DevKitC-1U", "ltf_config": None, "link_id": "tx1->rx1"},
            {"receiver_id": "rx_router", "port": "COM5", "baud": 921600,
             "input_format": "esp-csi-upstream-classic-v1", "transmitter_id": "router", "transmitter_mac": None,
             "declared_chip": "esp32", "declared_board": None, "ltf_config": "lltf_only",
             "link_id": "router->rx_router"},
        ]
        # Listing receivers never starts or opens anything.
        assert c.get("/api/status").json()["source_banner"] == "NO SOURCE"
    with api_client(make_cfg(tmp_path)) as c:
        assert c.get("/api/source/receivers").json() == []
