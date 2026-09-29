"""On-disk recording format ``roomsense-recording-v1`` (shared by the
recorder in :mod:`roomsense.storage.recordings` and the replay source in
:mod:`roomsense.acquisition.replay_source`).

A recording is a gzip-compressed JSON Lines file. Each line is one object with
a ``type`` key:

* ``{"type": "header", "format": "roomsense-recording-v1", "schema_version": ..,
  "parser_version": .., "recording_id": .., "session_id": .., "source_mode": ..,
  "created_at_unix_ns": .., "label": .., "scenario": .., "link_ids": [..],
  "consent_id": .., "config_version": .., "notes": ..}`` — always the first line.
* ``{"type": "frame", "frame": <CsiFrame.to_record()>}``
* ``{"type": "event", "event": {"kind": .., "link_id": .., "receiver_id": ..,
  "detail": .., "host_monotonic_ns": .., "host_unix_ns": ..}}`` — link events
  such as DISCONNECTED, so replays preserve gaps and their causes.
* ``{"type": "footer", "ended_at_unix_ns": .., "status": .., "frames": ..,
  "bytes_uncompressed": .., "reason": ..}`` — present when closed cleanly. A
  missing footer means the recording was interrupted (still replayable).

Readers enforce a maximum line length and reject unknown formats or
incompatible major schema versions. JSON is parsed with :mod:`json` only.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from . import PARSER_VERSION, SCHEMA_VERSION
from .schemas import CsiFrame

RECORDING_FORMAT = "roomsense-recording-v1"
MAX_RECORD_LINE_BYTES = 1_000_000


class RecordingFormatError(ValueError):
    pass


class RecordingWriter:
    """Append-only writer. Thread-safe. Tracks compressed bytes on disk."""

    def __init__(self, path: Path, header: dict[str, Any]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._raw = open(self.path, "xb")  # never overwrite an existing recording
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="wb", compresslevel=6)
        self._lock = threading.Lock()
        self._closed = False
        self.frames = 0
        self.bytes_uncompressed = 0
        hdr = dict(header)
        hdr.update(
            {
                "type": "header",
                "format": RECORDING_FORMAT,
                "schema_version": SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
            }
        )
        self._write_obj(hdr)

    def _write_obj(self, obj: dict[str, Any]) -> int:
        line = (json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if len(line) > MAX_RECORD_LINE_BYTES:
            raise RecordingFormatError(f"record too large ({len(line)} bytes)")
        self._gz.write(line)
        self.bytes_uncompressed += len(line)
        return len(line)

    def write_frame(self, frame: CsiFrame) -> int:
        with self._lock:
            if self._closed:
                raise RecordingFormatError("writer is closed")
            n = self._write_obj({"type": "frame", "frame": frame.to_record()})
            self.frames += 1
            return n

    def write_event(self, event: dict[str, Any]) -> int:
        with self._lock:
            if self._closed:
                raise RecordingFormatError("writer is closed")
            return self._write_obj({"type": "event", "event": event})

    def compressed_bytes(self) -> int:
        """Bytes flushed to disk so far (approximate while open)."""
        try:
            return self._raw.tell()
        except (OSError, ValueError):
            return os.path.getsize(self.path) if self.path.exists() else 0

    def close(self, footer: dict[str, Any]) -> int:
        with self._lock:
            if self._closed:
                return os.path.getsize(self.path)
            ftr = dict(footer)
            ftr.update({"type": "footer", "frames": self.frames, "bytes_uncompressed": self.bytes_uncompressed})
            self._write_obj(ftr)
            self._gz.close()
            self._raw.close()
            self._closed = True
            return os.path.getsize(self.path)

    @property
    def closed(self) -> bool:
        return self._closed


def read_recording(path: Path, *, decode_frames: bool = True) -> Iterator[dict[str, Any]]:
    """Yield records from a recording file.

    The first yielded record is the header. Frame records have their
    ``frame`` value converted to :class:`CsiFrame` when ``decode_frames``.
    Truncated gzip streams (interrupted recordings) end iteration after the
    last complete line.
    """
    with gzip.open(path, "rb") as gz:
        reader = io.BufferedReader(gz)  # type: ignore[arg-type]
        first = True
        while True:
            try:
                line = reader.readline(MAX_RECORD_LINE_BYTES + 1)
            except (EOFError, OSError, gzip.BadGzipFile):
                return  # interrupted recording: stop at the last complete record
            if not line:
                return
            if len(line) > MAX_RECORD_LINE_BYTES:
                raise RecordingFormatError("record line exceeds maximum length")
            if not line.endswith(b"\n"):
                return  # partial last line from an interrupted write
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RecordingFormatError(f"invalid JSON record: {exc}") from exc
            if not isinstance(obj, dict) or "type" not in obj:
                raise RecordingFormatError("record without type")
            if first:
                if obj.get("type") != "header" or obj.get("format") != RECORDING_FORMAT:
                    raise RecordingFormatError("not a roomsense-recording-v1 file")
                major = str(obj.get("schema_version", "")).split(".")[0]
                if major != SCHEMA_VERSION.split(".")[0]:
                    raise RecordingFormatError(f"incompatible schema_version {obj.get('schema_version')!r}")
                first = False
            elif obj["type"] == "frame" and decode_frames:
                obj = {"type": "frame", "frame": CsiFrame.from_record(obj["frame"])}
            yield obj


def read_header(path: Path) -> dict[str, Any]:
    for rec in read_recording(path, decode_frames=False):
        return rec
    raise RecordingFormatError("empty recording")
