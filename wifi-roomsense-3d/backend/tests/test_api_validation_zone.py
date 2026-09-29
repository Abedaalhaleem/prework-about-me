"""HTTP API: validation protocol/runs/report, zone status/training, pose, hardware."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import roomsense
from tests.api_helpers import _no_api_token_in_env, api_client, make_cfg  # noqa: F401  (autouse fixture)


def test_validation_protocol(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        proto = c.get("/api/validation/protocol").json()
        assert proto["protocol_version"] and proto["scenarios"]


def test_validation_runs_and_report(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        proto = c.get("/api/validation/protocol").json()
        sid = proto["scenarios"][0]["scenario_id"]
        body = {"scenario_id": sid, "placement": "tx west wall, rx east wall", "wall_description": "drywall",
                "channel": 6, "conditions": "evening", "notes": "software test"}
        r = c.post("/api/validation/runs", json=body)
        assert r.status_code == 409 and r.json()["code"] == "NO_SOURCE"
        c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True})
        r = c.post("/api/validation/runs", json={**body, "scenario_id": "NO_SUCH_SCENARIO"})
        assert r.status_code == 422 and r.json()["code"] == "UNKNOWN_SCENARIO"
        r = c.post("/api/validation/runs", json=body)
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["status"] == "RUNNING" and run["source_mode"] == "SIMULATION"
        assert run["link_ids"] == ["tx1->rx1"]
        assert c.post("/api/validation/runs", json=body).status_code == 409
        r = c.post(f"/api/validation/runs/{run['run_id']}/stop")
        assert r.status_code == 200 and r.json()["status"] == "COMPLETE" and r.json()["ended_at_unix_ns"]
        assert c.post(f"/api/validation/runs/{run['run_id']}/stop").status_code == 409
        assert c.post("/api/validation/runs/vrun_missing/stop").status_code == 404
        assert [x["run_id"] for x in c.get("/api/validation/runs").json()] == [run["run_id"]]

        report = c.get("/api/validation/report").json()
        assert report["through_wall_status"] == "UNVERIFIED"  # simulation never counts as evidence
        assert run["run_id"] in [x["run_id"] for x in report["excluded_runs"]]
        md = c.get("/api/validation/report.md")
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
        assert "attachment" in md.headers["content-disposition"]
        assert md.text.startswith("# ") and "UNVERIFIED" in md.text
        assert c.get("/api/status").json()["through_wall_status"] == "UNVERIFIED"


def test_switching_source_aborts_a_running_validation_run(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True})
        sid = c.get("/api/validation/protocol").json()["scenarios"][0]["scenario_id"]
        run = c.post("/api/validation/runs", json={"scenario_id": sid}).json()
        c.post("/api/source/stop")
        runs = c.get("/api/validation/runs").json()
        assert runs[0]["run_id"] == run["run_id"] and runs[0]["status"] == "ABORTED"


def test_zone_status_disabled_without_model(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        z = c.get("/api/zone/status").json()
        assert z["state"] == "DISABLED"
        assert any(r.startswith("NO_ENABLED_MODEL") for r in z["reasons"])
        assert z["criteria"] is not None and z["criteria"]["criteria_version"]


def test_zone_training_refuses_unknown_recordings(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        body = {"sessions": [{"recording_id": "rec_missing", "label": "A", "split": "train"}]}
        r = c.post("/api/zone/train", json=body)
        assert r.status_code in (409, 422), r.text
        assert r.json()["code"]
        assert c.post("/api/zone/train", json={"sessions": [{"recording_id": "rec_x", "label": "A",
                                                              "split": "later"}]}).status_code == 422
        assert c.get("/api/zone/status").json()["state"] == "DISABLED"


def test_pose_status_is_disabled_with_reasons(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        pose = c.get("/api/pose/status").json()
        assert pose["enabled"] is False and pose["label"] == "EXPERIMENTAL"
        assert pose["missing_requirements"]


def test_hardware_inspection_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import roomsense.hardware as hardware

    calls: list[int] = []

    def fake_inspect(*, redact: bool = True) -> dict[str, Any]:
        calls.append(1)
        report = {"format": "roomsense-hardware-report-v1", "detected": {"serial_ports": []},
                  "recommended": {"boards": []}, "errors": []}
        report["assessment"] = hardware.assess(report)
        return report

    monkeypatch.setattr(hardware, "inspect_host", fake_inspect)
    with api_client(make_cfg(tmp_path)) as c:
        first = c.get("/api/hardware").json()
        assert set(first) >= {"detected", "recommended", "assessment", "cache_age_s"}
        assert first["assessment"]["csi_path_confirmed"] is False
        c.get("/api/hardware")
        assert len(calls) == 1  # cached
        c.get("/api/hardware", params={"refresh": True})
        assert len(calls) == 2


def test_hardware_module_unavailable_is_503_not_fake_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(roomsense, "hardware", raising=False)
    monkeypatch.setitem(sys.modules, "roomsense.hardware", None)  # type: ignore[arg-type]
    with api_client(make_cfg(tmp_path)) as c:
        r = c.get("/api/hardware")
        assert r.status_code == 503
        assert r.json()["code"] == "HARDWARE_MODULE_UNAVAILABLE"


def _write_zone_recording(rec_dir: Path, recording_id: str, label: str, seed: int) -> None:
    """A short SIMULATED single-label recording file (software test data only)."""
    from roomsense.acquisition.synthetic import generate_frames, zone_session_scenario
    from roomsense.recording_format import RecordingWriter

    scn = zone_session_scenario(label, seed=seed, duration_s=8.0, rate_hz=12.5)
    header = {"recording_id": recording_id, "session_id": f"sess_{recording_id}", "source_mode": "SIMULATION",
              "original_source_mode": "SIMULATION", "synthetic": True, "created_at_unix_ns": 1_700_000_000 * 10**9
              + seed * 10**12, "label": label, "scenario": scn.name, "link_ids": scn.link_ids()}
    writer = RecordingWriter(rec_dir / f"{recording_id}.jsonl.gz", header)
    for frame in generate_frames(scn, f"sess_{recording_id}"):
        writer.write_frame(frame)
    writer.close({"status": "COMPLETE", "synthetic": True})


def test_zone_training_runs_on_recordings_and_refuses_too_few_sessions(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    rec_dir = cfg.storage.resolved_data_dir() / "recordings"
    rec_dir.mkdir(parents=True)
    for i, label in enumerate(("A", "B", "EMPTY")):
        _write_zone_recording(rec_dir, f"sim_zone_{i}", label, seed=100 + i)
    with api_client(cfg) as c:
        body = {"sessions": [{"recording_id": f"sim_zone_{i}", "label": label}
                             for i, label in enumerate(("A", "B", "EMPTY"))]}
        r = c.post("/api/zone/train", json=body)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "INSUFFICIENT_SESSIONS"
        assert c.get("/api/zone/status").json()["state"] == "DISABLED"
