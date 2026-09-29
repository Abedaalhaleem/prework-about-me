"""End-to-end tests of the ``roomsense`` command line.

The live capture reads from a fake serial port (hand-made firmware lines);
``simulate`` writes clearly flagged synthetic data. Nothing here touches
hardware or says anything about sensing accuracy.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

import roomsense.acquisition.serial_source as serial_source
from roomsense import __version__
from roomsense.cli import main
from roomsense.config import API_TOKEN_ENV, load_config
from roomsense.runtime import DB_FILENAME, AppRuntime
from roomsense.schemas import SourceMode
from roomsense.storage.db import Database
from roomsense.storage.recordings import Recorder, recording_path
from tests.api_helpers import PacedSerialFactory, _no_api_token_in_env  # noqa: F401  (autouse fixture)
from tests.storage_helpers import make_consent, make_frame

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _write_cfg(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    q = json.dumps  # JSON string escaping is valid for TOML basic strings
    text = f"""
[storage]
data_dir = {q(str(data))}

[room]
geometry_file = {q(str(data / "room.json"))}

[acquisition]
reconnect_initial_s = 0.02
reconnect_max_s = 0.1
stale_after_s = 0.5

[[acquisition.receivers]]
receiver_id = "rx1"
port = "/dev/fake-rx1"
input_format = "roomsense-rscsi-v1"
transmitter_id = "tx1"
"""
    path = tmp_path / "roomsense.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _json_out(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


def test_simulate_writes_flagged_file_and_replay_info_reads_it(tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    cfg = str(_write_cfg(tmp_path))
    out = tmp_path / "sim.jsonl.gz"
    assert main(["simulate", "--config", cfg, "--scenario", "disconnect", "--seed", "3", "--out", str(out)]) == 0
    assert "SIMULATED" in capsys.readouterr().out
    assert out.is_file()
    assert main(["simulate", "--config", cfg, "--scenario", "disconnect", "--out", str(out)]) == 2  # no overwrite
    assert main(["simulate", "--config", cfg, "--scenario", "nope", "--out", str(tmp_path / "x.gz")]) == 2
    capsys.readouterr()

    assert main(["replay-info", str(out)]) == 0
    captured = capsys.readouterr()
    info = json.loads(captured.out)
    assert info["synthetic"] is True and info["source_mode"] == "SIMULATION"
    assert info["original_source_mode"] == "SIMULATION" and info["complete"] is True
    assert list(info["frames_per_link"]) == ["tx1->rx1"]
    assert 2240 <= info["frames_per_link"]["tx1->rx1"] <= 2260  # ~90 s at 25 Hz, then the simulated disconnect
    assert "SIMULATED" in captured.err
    assert main(["replay-info", str(tmp_path / "missing.gz")]) == 2


def test_capture_refuses_without_consent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = str(_write_cfg(tmp_path))
    base = ["capture", "--config", cfg, "--receiver", "rx1", "--seconds", "1", "--label", "x"]
    assert main(base) == 2
    err = capsys.readouterr().err
    assert "consent is required" in err and "consent-v1" in err
    assert main(base + ["--consent-all-participants", "--purpose", "test"]) == 2  # participant count missing
    assert main(base + ["--participants", "1", "--purpose", "test"]) == 2  # confirmation missing
    assert not (tmp_path / "data").exists()  # nothing was opened or written


def test_capture_records_live_then_list_export_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    factory = PacedSerialFactory(rate_hz=50)
    monkeypatch.setattr(serial_source, "_default_serial_factory", factory)
    cfg = str(_write_cfg(tmp_path))
    assert main(["capture", "--config", cfg, "--receiver", "rx9", "--seconds", "1", "--label", "x",
                 "--consent-all-participants", "--participants", "1", "--purpose", "test"]) == 2  # unknown receiver
    capsys.readouterr()
    rc = main(["capture", "--config", cfg, "--receiver", "rx1", "--seconds", "1.2", "--label", "desk test",
               "--consent-all-participants", "--participants", "1", "--purpose", "software test"])
    assert rc == 0
    info = _json_out(capsys)
    assert info["status"] == "COMPLETE" and info["source_mode"] == "LIVE" and info["synthetic"] is False
    assert info["frames"] > 20 and info["label"] == "desk test"
    rid = info["recording_id"]

    assert main(["recordings", "list", "--config", cfg, "--json"]) == 0
    assert [r["recording_id"] for r in _json_out(capsys)] == [rid]
    assert main(["recordings", "list", "--config", cfg]) == 0
    assert rid in capsys.readouterr().out

    assert main(["recordings", "export", "--config", cfg, rid]) == 0
    zip_path = Path(capsys.readouterr().out.strip())
    with zipfile.ZipFile(zip_path) as zf:
        assert "PROVENANCE.txt" in zf.namelist()

    assert main(["recordings", "delete", "--config", cfg, rid]) == 0
    assert not (tmp_path / "data" / "recordings" / f"{rid}.jsonl.gz").exists()
    assert main(["recordings", "delete", "--config", cfg, rid]) == 1


def test_validate_report_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = str(_write_cfg(tmp_path))
    assert main(["validate-report", "--config", cfg, "--markdown"]) == 0
    assert capsys.readouterr().out.startswith("# RoomSense through-wall validation report")
    assert main(["validate-report", "--config", cfg]) == 0
    assert _json_out(capsys)["through_wall_status"] == "UNVERIFIED"


def test_ports_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = [{"device": "/dev/ttyUSB3", "description": "CP2102 USB to UART", "hwid": "x", "vid": 0x10C4,
             "pid": 0xEA60, "likely_usb_uart_bridge": True}]
    monkeypatch.setattr(serial_source, "list_serial_ports", lambda: fake)
    assert main(["ports"]) == 0
    out = capsys.readouterr().out
    assert "/dev/ttyUSB3" in out and "does not prove" in out
    assert main(["ports", "--json"]) == 0
    assert _json_out(capsys) == fake


def test_serve_refuses_unsafe_bind_before_starting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    import uvicorn

    def never(*_: Any, **__: Any) -> None:
        raise AssertionError("uvicorn must not start")

    monkeypatch.setattr(uvicorn, "run", never)
    cfg = str(_write_cfg(tmp_path))
    assert main(["serve", "--config", cfg, "--host", "0.0.0.0"]) == 2
    assert "allow_non_loopback" in capsys.readouterr().err
    assert main(["serve", "--config", str(tmp_path / "missing.toml")]) == 2


def test_serve_runs_uvicorn_on_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    import roomsense.logging_setup as logging_setup

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    monkeypatch.setattr(logging_setup, "configure_logging", lambda *a, **k: None)
    cfg = str(_write_cfg(tmp_path))
    assert main(["serve", "--config", cfg, "--port", "8799"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8799
    assert seen["log_config"] is None and seen["access_log"] is False
    assert seen["app"].title == "WiFi RoomSense 3D"


def test_zone_train_cli_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = str(_write_cfg(tmp_path))
    assert main(["zone-train", "--config", cfg, "--sessions", "[{\"label\": \"A\"}]"]) == 2
    assert main(["zone-train", "--config", cfg, "--sessions", str(tmp_path / "missing.json")]) == 2
    spec = json.dumps([{"recording_id": "rec_missing", "label": "A", "split": "train"}])
    assert main(["zone-train", "--config", cfg, "--sessions", spec]) == 1
    assert "rec_missing" in capsys.readouterr().err


def test_python_dash_m_entry_point() -> None:
    r = subprocess.run([sys.executable, "-m", "roomsense", "--version"], cwd=BACKEND_DIR, capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 0
    assert r.stdout.strip() == f"roomsense {__version__}"


def _stored_recording(cfg_path: Path) -> str:
    """One small recording made with the storage layer (hand-built test frames)."""
    cfg = load_config(cfg_path)
    data = cfg.storage.resolved_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    with Database(data / DB_FILENAME) as db:
        rec = Recorder(db, data, cfg.storage)
        info = rec.start(consent=make_consent(), label="cli lock test", session_id="sess_cli",
                         source_mode=SourceMode.LIVE)
        rec.write(make_frame(session_id="sess_cli"))
        rec.stop()
    return info.recording_id


def test_cli_refuses_to_change_the_data_folder_while_a_server_uses_it(tmp_path: Path,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = _write_cfg(tmp_path)
    cfg = str(cfg_path)
    rid = _stored_recording(cfg_path)
    spec = json.dumps([{"recording_id": rid, "label": "A", "split": "train"}])
    server = AppRuntime(load_config(cfg_path), background_processing=False)  # holds the data-folder lock
    try:
        for argv in (["recordings", "delete", "--config", cfg, rid],
                     ["recordings", "export", "--config", cfg, rid],
                     ["zone-train", "--config", cfg, "--sessions", spec]):
            assert main(argv) == 1, argv
            err = capsys.readouterr().err
            assert "DATA_DIR_LOCKED" in err and "Stop the server" in err
            if argv[0] == "zone-train":
                # There is no zone-training form in the UI; point at the API instead.
                assert "POST /api/zone/train" in err and "use the UI" not in err
            else:
                assert "use the UI" in err
        assert server.db.get_recording(rid) is not None
        assert recording_path(tmp_path / "data", rid).is_file()
        assert not (tmp_path / "data" / "exports").exists()
        assert main(["recordings", "list", "--config", cfg, "--json"]) == 0  # reading is fine
        assert [r["recording_id"] for r in _json_out(capsys)] == [rid]
    finally:
        server.shutdown()
    # Once the server is stopped the same commands work.
    assert main(["recordings", "export", "--config", cfg, rid]) == 0
    assert Path(capsys.readouterr().out.strip()).is_file()
    assert main(["recordings", "delete", "--config", cfg, rid]) == 0
    assert f"deleted {rid}" in capsys.readouterr().out
    assert not recording_path(tmp_path / "data", rid).exists()
    assert not list((tmp_path / "data" / "exports").glob(f"{rid}*"))


@pytest.mark.parametrize("bad", ["xq7Zk9", "", "   ", "0123456789abcdef 01234", "0123456789abcdefghij\n",
                                 "t\u00f6ken-0123456789abcdef"])
def test_serve_refuses_an_unusable_api_token_on_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str], bad: str) -> None:
    import uvicorn

    def never(*_: Any, **__: Any) -> None:
        raise AssertionError("uvicorn must not start")

    monkeypatch.setattr(uvicorn, "run", never)
    monkeypatch.setenv(API_TOKEN_ENV, bad)
    assert main(["serve", "--config", str(_write_cfg(tmp_path))]) == 2
    captured = capsys.readouterr()
    assert "ROOMSENSE_API_TOKEN" in captured.err
    if bad.strip():
        assert bad.strip() not in captured.err + captured.out  # the token is never printed
