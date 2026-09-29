#!/usr/bin/env python3
"""Inspect this computer for RoomSense hardware (read-only).

Lists serial ports (never opens them), network interfaces, ESP-IDF and
virtualisation hints, then says whether RoomSense's documented CSI acquisition
path (ESP32 boards over USB serial) is possibly present. It never scans for
Wi-Fi networks, never runs anything privileged, and never flashes, erases or
reconfigures a device or router.

Usage (from the repository's backend/ directory):

    uv run python ../scripts/inspect_hardware.py                 # Markdown to stdout
    uv run python ../scripts/inspect_hardware.py --json          # JSON to stdout
    uv run python ../scripts/inspect_hardware.py --write ../HARDWARE_REPORT.local.md

Identifiers (MAC addresses, SSIDs, adapter GUIDs, USB serial numbers, the home
directory) are redacted unless --include-identifiers is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make ``roomsense`` importable whether this runs via ``uv run`` from backend/
# or with a plain interpreter from anywhere. hardware.py needs only the
# standard library (pyserial is optional: without it ports are reported as
# "could not be listed", never as "none").
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from roomsense.hardware import inspect_host, render_markdown  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="inspect_hardware.py",
        description="Read-only inspection of this computer for RoomSense (ESP32 CSI over USB serial).",
    )
    parser.add_argument("--json", action="store_true", help="output JSON instead of Markdown")
    parser.add_argument(
        "--write",
        metavar="PATH",
        help="write the report to PATH (its directory must already exist) instead of printing it",
    )
    parser.add_argument(
        "--include-identifiers",
        action="store_true",
        help="do not redact MAC addresses, SSIDs, adapter GUIDs, USB serial numbers or the home directory "
        "(keep such a report private)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = inspect_host(redact=not args.include_identifiers)
    text = json.dumps(report, indent=2, allow_nan=False) + "\n" if args.json else render_markdown(report)

    if not args.write:
        sys.stdout.write(text)
        return 0

    out = Path(args.write).expanduser()
    if out.is_dir():
        print(f"error: {out} is a directory; give a file name", file=sys.stderr)
        return 2
    if not out.parent.is_dir():
        # Creating directories is out of scope for a read-only inspector.
        print(f"error: directory {out.parent} does not exist", file=sys.stderr)
        return 2
    out.write_text(text, encoding="utf-8")
    assessment = report["assessment"]
    print(
        f"wrote {out} (csi_path_available={assessment['csi_path_available']}, "
        f"hardware_required={assessment['hardware_required']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
