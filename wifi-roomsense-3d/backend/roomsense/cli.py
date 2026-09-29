"""``roomsense`` command line (also ``python -m roomsense``).

Subcommands::

    serve            run the local web app (loopback by default)
    inspect-hardware read-only inspection of this computer (never opens ports)
    ports            list serial ports (never opens them)
    capture          headless LIVE recording from one configured receiver (consent required)
    recordings       list | delete ID | export ID  (delete/export refuse while a server uses the data folder)
    simulate         write a SIMULATED recording file (clearly flagged as synthetic)
    replay-info      summarise a recording file
    validate-report  print the through-wall validation report (JSON or Markdown)
    zone-train       train/evaluate a zone model on labelled recordings (refused while a server runs)

Nothing here flashes, erases or reconfigures devices or routers, scans Wi-Fi,
or needs elevated privileges.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Sequence

from . import SCHEMA_VERSION, __version__
from .config import AppConfig, api_token, load_config

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
# `capture` stops its recording itself; the recorder's duration bound is this much later.
CAPTURE_GRACE_S = 2.0


def _err(msg: str) -> None:
    print(f"roomsense: {msg}", file=sys.stderr)


def _load(args: argparse.Namespace) -> AppConfig:
    return load_config(getattr(args, "config", None))


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=False, default=str, allow_nan=False))


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from .api.app import create_app
    from .api.security import StartupRefused, check_bind_allowed
    from .logging_setup import configure_logging

    cfg = _load(args)
    server = cfg.server.model_dump()
    if args.host:
        server["host"] = args.host
    if args.port:
        server["port"] = args.port
    try:
        cfg = AppConfig.model_validate({**cfg.model_dump(), "server": server})
    except ValueError as exc:
        _err(f"invalid server settings: {exc}")
        return EXIT_USAGE
    try:
        check_bind_allowed(cfg)  # before anything else, so a refused start changes nothing
    except StartupRefused as exc:
        _err(str(exc))
        return EXIT_USAGE
    configure_logging(args.log_level, secrets=[api_token()])
    app = create_app(cfg)
    import uvicorn

    host = cfg.server.host
    shown = f"[{host}]" if ":" in host else host
    print(f"RoomSense {__version__} serving on http://{shown}:{cfg.server.port}", file=sys.stderr)
    uvicorn.run(
        app,
        host=host,
        port=cfg.server.port,
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=10,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# inspect-hardware / ports
# ---------------------------------------------------------------------------


def cmd_inspect_hardware(args: argparse.Namespace) -> int:
    from .hardware import inspect_host, render_markdown

    report = inspect_host(redact=not args.include_identifiers)
    text = json.dumps(report, indent=2, allow_nan=False) + "\n" if args.json else render_markdown(report)
    if not args.write:
        sys.stdout.write(text)
        return EXIT_OK
    out = Path(args.write).expanduser()
    if out.is_dir():
        _err(f"{out} is a directory; give a file name")
        return EXIT_USAGE
    if not out.parent.is_dir():
        _err(f"directory {out.parent} does not exist")
        return EXIT_USAGE
    out.write_text(text, encoding="utf-8")
    a = report.get("assessment", {})
    print(f"wrote {out} (csi_path_available={a.get('csi_path_available')}, "
          f"hardware_required={a.get('hardware_required')})")
    return EXIT_OK


def cmd_ports(args: argparse.Namespace) -> int:
    from .acquisition.serial_source import list_serial_ports

    ports = list_serial_ports()
    if args.json:
        _print_json(ports)
        return EXIT_OK
    if not ports:
        print("No serial ports found. (Ports are listed, never opened.)")
        return EXIT_OK
    for p in ports:
        flag = " [USB-UART bridge used on ESP32 boards]" if p["likely_usb_uart_bridge"] else ""
        vid = "-" if p["vid"] is None else f"{p['vid']:04x}:{(p['pid'] or 0):04x}"
        print(f"{p['device']:<28} {vid:<10} {p['description']}{flag}")
    print("A listed bridge does not prove that an ESP32 or RoomSense firmware is attached.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def cmd_capture(args: argparse.Namespace) -> int:
    from .runtime import AppRuntime, OperationRefused
    from .storage.models import CONSENT_STATEMENT_V1, CONSENT_STATEMENT_VERSION

    if not args.consent_all_participants or args.participants is None or not (args.purpose or "").strip():
        _err("recording refused: consent is required. Read the statement below, make sure every person "
             "present agreed, then pass --consent-all-participants --participants N --purpose TEXT.\n")
        print(CONSENT_STATEMENT_V1, file=sys.stderr)
        return EXIT_USAGE
    cfg = _load(args)
    rx = [r for r in cfg.acquisition.receivers if r.receiver_id == args.receiver]
    if not rx:
        known = ", ".join(r.receiver_id for r in cfg.acquisition.receivers) or "none"
        _err(f"receiver {args.receiver!r} is not configured (configured: {known}); add it to configs/roomsense.toml")
        return EXIT_USAGE
    if not (0 < args.seconds <= cfg.storage.max_recording_seconds):
        _err(f"--seconds must be in (0, {cfg.storage.max_recording_seconds:g}]")
        return EXIT_USAGE

    stop = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        stop.set()

    old_term = signal.signal(signal.SIGTERM, _on_signal)
    try:
        with AppRuntime(cfg) as rt:
            try:
                rt.start_live(rx)
                # This command stops the recording after --seconds. The recorder's
                # own duration bound is only a safety net (a stalled command), so it
                # gets a grace period instead of racing the stop below; otherwise a
                # capture of exactly the requested length could randomly end as
                # TRUNCATED_LIMIT instead of COMPLETE.
                info = rt.start_recording(
                    consent={"all_participants_consented": True, "participant_count": args.participants,
                             "purpose": args.purpose, "statement_version": CONSENT_STATEMENT_VERSION},
                    label=args.label, scenario=args.scenario, notes=args.notes,
                    max_seconds=args.seconds + CAPTURE_GRACE_S,
                )
            except OperationRefused as exc:
                _err(f"{exc.code}: {exc.detail}")
                return EXIT_FAILED
            print(f"recording {info.recording_id} from {rx[0].port} for {args.seconds:g} s (Ctrl-C stops early)",
                  file=sys.stderr)
            deadline = time.monotonic() + args.seconds
            last_report = 0.0
            try:
                while not stop.is_set() and time.monotonic() < deadline and rt.recorder.active is not None:
                    stop.wait(max(0.0, min(0.2, deadline - time.monotonic())))
                    if time.monotonic() - last_report >= 5.0:
                        last_report = time.monotonic()
                        st = rt.build_status()
                        frames = sum(ls.frames_total for ls in st.links)
                        print(f"  {st.source_state.value}: {frames} frame(s) received", file=sys.stderr)
            except KeyboardInterrupt:
                print("stopping (interrupted)", file=sys.stderr)
            try:
                final = rt.stop_recording()
            except OperationRefused:
                final = rt.recorder.last_finished
            status = rt.build_status()
    finally:
        signal.signal(signal.SIGTERM, old_term)
    if final is None:
        _err("the recording did not finish cleanly")
        return EXIT_FAILED
    _print_json(final.model_dump(mode="json"))
    if final.frames == 0:
        _err(f"no frames were recorded (source state {status.source_state.value}: {status.source_detail}). "
             "Check the port, cable and firmware.")
        return EXIT_FAILED
    return EXIT_OK


# ---------------------------------------------------------------------------
# recordings
# ---------------------------------------------------------------------------


def _open_db(cfg: AppConfig) -> Any:
    from .runtime import DB_FILENAME
    from .storage.db import Database

    data_dir = cfg.storage.resolved_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Database(data_dir / DB_FILENAME)


def _lock_data_dir(cfg: AppConfig, action: str, stack: ExitStack) -> bool:
    """Take the data-folder lock a running server holds, for commands that
    change the data folder (it is released when ``stack`` closes). False,
    after printing why, when it is held: a running server could be
    replaying, recording or training on the same files."""
    from .runtime import OperationRefused, data_dir_lock

    data_dir = cfg.storage.resolved_data_dir()
    try:
        stack.enter_context(data_dir_lock(data_dir))
    except OperationRefused as exc:
        if exc.code != "DATA_DIR_LOCKED":
            raise
        _err(f"DATA_DIR_LOCKED: {data_dir} is in use by a running RoomSense server (or another roomsense "
             f"command), so it cannot {action} now. Stop the server (scripts/stop.sh, Windows: "
             "scripts\\stop.ps1) or use the UI.")
        return False
    return True


def cmd_recordings(args: argparse.Namespace) -> int:
    from .storage.exports import ExportError, export_recording
    from .storage.recordings import RecordingRefused, purge_recording

    cfg = _load(args)
    data_dir = cfg.storage.resolved_data_dir()
    if args.action == "list":
        with _open_db(cfg) as db:
            rows = db.list_recordings(limit=None)
        if args.json:
            _print_json([r.model_dump(mode="json") for r in rows])
            return EXIT_OK
        if not rows:
            print("No recordings.")
            return EXIT_OK
        for r in rows:
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.created_at_unix_ns / 1e9))
            tag = "SYNTHETIC" if r.synthetic else r.source_mode.value
            print(f"{r.recording_id}  {created}  {tag:<10} {r.status.value:<16} {r.frames:>8} frames "
                  f"{r.duration_s:8.1f} s  {r.label}")
        return EXIT_OK
    # delete / export change the data folder: never while a server uses it.
    with ExitStack() as stack:
        if not _lock_data_dir(cfg, f"{args.action} recordings", stack):
            return EXIT_FAILED
        db = stack.enter_context(_open_db(cfg))
        try:
            if args.action == "delete":
                result = purge_recording(db, data_dir, args.recording_id)
                if not result.deleted:
                    _err(f"recording {args.recording_id} not found")
                    return EXIT_FAILED
                print(f"deleted {args.recording_id}")
                for mid in result.removed_models:
                    print(f"deleted zone model {mid} (it was trained on this recording)")
                return EXIT_OK
            path = export_recording(db, data_dir, args.recording_id,
                                    max_total_bytes=cfg.storage.max_total_recording_bytes)
            print(path)
            return EXIT_OK
        except RecordingRefused as exc:
            _err(f"{exc.code}: {exc.detail}")
        except ExportError as exc:
            _err(f"{exc.code}: {exc}")
        except (FileNotFoundError, ValueError) as exc:
            _err(str(exc))
        return EXIT_FAILED


# ---------------------------------------------------------------------------
# simulate / replay-info
# ---------------------------------------------------------------------------


def cmd_simulate(args: argparse.Namespace) -> int:
    from .acquisition.synthetic import SIMULATED_BANNER, builtin_scenarios, generate_frames, with_seed
    from .recording_format import RecordingWriter
    from .schemas import SourceMode
    from .storage.models import new_id

    scenarios = builtin_scenarios()
    if args.scenario not in scenarios:
        _err(f"unknown scenario {args.scenario!r}; available: {', '.join(sorted(scenarios))}")
        return EXIT_USAGE
    scn = scenarios[args.scenario]
    if args.seed is not None:
        scn = with_seed(scn, args.seed)
    cfg = _load(args)
    out = Path(args.out).expanduser()
    if not out.parent.is_dir():
        _err(f"directory {out.parent} does not exist")
        return EXIT_USAGE
    session_id = new_id("sim")
    header = {
        "recording_id": new_id("sim"),
        "session_id": session_id,
        "source_mode": SourceMode.SIMULATION.value,
        "original_source_mode": SourceMode.SIMULATION.value,
        "synthetic": True,
        "created_at_unix_ns": time.time_ns(),
        "label": f"SIMULATION: {scn.name}",
        "scenario": scn.name,
        "link_ids": scn.link_ids(),
        "consent_id": None,
        "consent_statement_version": None,
        "config_version": cfg.config_version(),
        "notes": f"{SIMULATED_BANNER}. Written by 'roomsense simulate' (seed {scn.seed}). Not a measurement.",
        "limits": None,
    }
    try:
        writer = RecordingWriter(out, header)
    except FileExistsError:
        _err(f"{out} already exists; refusing to overwrite it")
        return EXIT_USAGE
    links: set[str] = set()
    try:
        for frame in generate_frames(scn, session_id):
            writer.write_frame(frame)
            links.add(frame.link_id)
    except BaseException:
        writer.close({"ended_at_unix_ns": time.time_ns(), "status": "ERROR", "reason": "interrupted",
                      "synthetic": True})
        raise
    writer.close({
        "ended_at_unix_ns": time.time_ns(), "status": "COMPLETE", "reason": "end of simulated scenario",
        "synthetic": True, "duration_s": scn.duration_s, "link_ids_seen": sorted(links), "frames_rejected": 0,
        "events": 0,
    })
    print(f"wrote SIMULATED recording {out} ({writer.frames} frames, scenario {scn.name}, seed {scn.seed})")
    print("This file contains synthetic data. It is labelled as such and can never count as validation evidence.")
    return EXIT_OK


def cmd_replay_info(args: argparse.Namespace) -> int:
    from .acquisition.replay_source import original_is_synthetic
    from .recording_format import RecordingFormatError, read_recording

    path = Path(args.path).expanduser()
    if not path.is_file():
        _err(f"{path} is not a file")
        return EXIT_USAGE
    try:
        header: dict[str, Any] | None = None
        footer: dict[str, Any] | None = None
        frames: Counter[str] = Counter()
        events: Counter[str] = Counter()
        first_t: int | None = None
        last_t: int | None = None
        for rec in read_recording(path, decode_frames=True):
            kind = rec.get("type")
            if header is None:
                header = rec
            elif kind == "frame":
                f = rec["frame"]
                frames[f.link_id] += 1
                t = f.recorded_host_arrival_unix_ns or f.host_arrival_unix_ns
                if t is not None:
                    first_t = t if first_t is None else min(first_t, t)
                    last_t = t if last_t is None else max(last_t, t)
            elif kind == "event":
                ev = rec.get("event") or {}
                events[str(ev.get("kind"))] += 1
            elif kind == "footer":
                footer = rec
        synthetic = original_is_synthetic(path)
    except (RecordingFormatError, OSError, ValueError) as exc:
        _err(f"cannot read {path.name}: {exc}")
        return EXIT_FAILED
    assert header is not None
    info = {
        "file": str(path),
        "format": header.get("format"),
        "schema_version": header.get("schema_version"),
        "recording_id": header.get("recording_id"),
        "session_id": header.get("session_id"),
        "source_mode": header.get("source_mode"),
        "original_source_mode": header.get("original_source_mode"),
        "synthetic": synthetic,
        "label": header.get("label"),
        "scenario": header.get("scenario"),
        "link_ids": header.get("link_ids"),
        "frames_per_link": dict(frames),
        "link_events": dict(events),
        "span_s": None if first_t is None or last_t is None else round((last_t - first_t) / 1e9, 3),
        "complete": footer is not None,
        "footer_status": None if footer is None else footer.get("status"),
        "reader_schema_version": SCHEMA_VERSION,
    }
    _print_json(info)
    if synthetic:
        print("NOTE: this recording contains SIMULATED data.", file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------
# validate-report / zone-train
# ---------------------------------------------------------------------------


def cmd_validate_report(args: argparse.Namespace) -> int:
    from .runtime import THROUGH_WALL_CRITERIA_PATH
    from .validation.report import CriteriaError, build_validation_report, render_markdown

    cfg = _load(args)
    with _open_db(cfg) as db:
        try:
            report = build_validation_report(db, THROUGH_WALL_CRITERIA_PATH)
        except CriteriaError as exc:
            _err(f"criteria file invalid: {exc}")
            return EXIT_FAILED
    if args.markdown:
        sys.stdout.write(render_markdown(report))
    else:
        _print_json(report)
    return EXIT_OK


def _parse_sessions(text: str) -> list[dict[str, Any]]:
    raw = text.strip()
    if not raw.startswith(("[", "{")):
        p = Path(raw).expanduser()
        if p.stat().st_size > 1_000_000:
            raise ValueError("sessions file is larger than 1 MB")
        raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("sessions")
    if not isinstance(data, list) or not data:
        raise ValueError("expected a non-empty list of {recording_id, label, split?} (or {\"sessions\": [...]})")
    for s in data:
        if not isinstance(s, dict) or "recording_id" not in s or "label" not in s:
            raise ValueError("every session needs recording_id and label")
    return data


def cmd_zone_train(args: argparse.Namespace) -> int:
    from .inference.zone.criteria import CriteriaError
    from .inference.zone.dataset import DatasetError, SessionSpec
    from .inference.zone.registry import RegistryError, ZoneModelRegistry
    from .inference.zone.train import TrainingRefused, run_training
    from .room import load_room

    try:
        sessions = _parse_sessions(args.sessions)
    except (OSError, ValueError) as exc:
        _err(f"--sessions: {exc}")
        return EXIT_USAGE
    cfg = _load(args)
    data_dir = cfg.storage.resolved_data_dir()
    # Training reads recordings and writes models: never while a server uses the folder.
    with ExitStack() as stack:
        if not _lock_data_dir(cfg, "train a zone model", stack):
            return EXIT_FAILED
        db = stack.enter_context(_open_db(cfg))
        try:
            specs = [SessionSpec(label=str(s["label"]), recording_id=str(s["recording_id"]), split=s.get("split"))
                     for s in sessions]
            trained, binding = run_training(specs, cfg, load_room(cfg), data_dir=data_dir, db=db,
                                            registry=ZoneModelRegistry(data_dir, db))
        except TrainingRefused as exc:
            _err(f"{exc.code}: {exc.detail}")
            return EXIT_FAILED
        except (DatasetError, CriteriaError, RegistryError) as exc:
            _err(f"{type(exc).__name__}: {exc}")
            return EXIT_FAILED
    _print_json({
        "model_id": None if binding is None else binding.model_id,
        "enabled": trained.enabled,
        "synthetic_data_used": trained.synthetic_data_used,
        "report": trained.report,
    })
    if not trained.enabled:
        print("The model is stored DISABLED: it did not pass every predefined criterion (see report).",
              file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", help="config file (default: configs/roomsense.toml if present)")

    p = argparse.ArgumentParser(prog="roomsense", description="WiFi RoomSense 3D (local, experimental).")
    p.add_argument("--version", action="version", version=f"roomsense {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("serve", parents=[common], help="run the local web app")
    s.add_argument("--host", help="bind address (default from config: 127.0.0.1)")
    s.add_argument("--port", type=int, help="port (default from config: 8765)")
    s.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("inspect-hardware", parents=[common], help="read-only inspection of this computer")
    s.add_argument("--json", action="store_true", help="JSON instead of Markdown")
    s.add_argument("--write", metavar="PATH", help="write the report to PATH instead of stdout")
    s.add_argument("--include-identifiers", action="store_true",
                   help="do not redact MAC addresses, SSIDs, serial numbers or the home directory")
    s.set_defaults(func=cmd_inspect_hardware)

    s = sub.add_parser("ports", parents=[common], help="list serial ports (never opens them)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ports)

    s = sub.add_parser("capture", parents=[common], help="headless LIVE recording (consent required)")
    s.add_argument("--receiver", required=True, metavar="ID", help="receiver_id from [[acquisition.receivers]]")
    s.add_argument("--seconds", required=True, type=float, metavar="N")
    s.add_argument("--label", required=True, metavar="L")
    s.add_argument("--consent-all-participants", action="store_true",
                   help="confirm that every person present was informed and agreed (see the consent statement)")
    s.add_argument("--participants", type=int, metavar="N", help="number of people present (>= 1)")
    s.add_argument("--purpose", metavar="TEXT", help="why this recording is made")
    s.add_argument("--scenario", metavar="TEXT")
    s.add_argument("--notes", metavar="TEXT")
    s.set_defaults(func=cmd_capture)

    s = sub.add_parser("recordings", help="list, delete or export recordings")
    rsub = s.add_subparsers(dest="action", metavar="ACTION")
    rsub.required = True
    r = rsub.add_parser("list", parents=[common])
    r.add_argument("--json", action="store_true")
    r = rsub.add_parser("delete", parents=[common])
    r.add_argument("recording_id")
    r = rsub.add_parser("export", parents=[common])
    r.add_argument("recording_id")
    s.set_defaults(func=cmd_recordings)

    s = sub.add_parser("simulate", parents=[common],
                       help="write a SIMULATED recording file (synthetic; never validation evidence)")
    s.add_argument("--scenario", required=True, metavar="NAME")
    s.add_argument("--out", required=True, metavar="PATH",
                   help="output file (*.jsonl.gz); never overwritten. Written as data/recordings/<id>.jsonl.gz it "
                        "can be replayed by id (it is not added to the recordings list)")
    s.add_argument("--seed", type=int, metavar="N")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("replay-info", parents=[common], help="summarise a recording file")
    s.add_argument("path")
    s.set_defaults(func=cmd_replay_info)

    s = sub.add_parser("validate-report", parents=[common], help="print the through-wall validation report")
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(func=cmd_validate_report)

    s = sub.add_parser("zone-train", parents=[common], help="train/evaluate a zone model on labelled recordings")
    s.add_argument("--sessions", required=True, metavar="JSON",
                   help='JSON list (or a path to a JSON file) of {"recording_id", "label", "split"?}')
    s.set_defaults(func=cmd_zone_train)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if exc.__class__.__name__ == "OperationRefused":
            _err(str(exc))
            return EXIT_FAILED
        raise
