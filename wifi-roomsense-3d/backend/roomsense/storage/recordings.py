"""Opt-in, consented, bounded raw CSI recordings.

A recording is ``<data_dir>/recordings/<recording_id>.jsonl.gz`` written with
:class:`roomsense.recording_format.RecordingWriter`, plus one row in the
``recordings`` table.

Bounds (all from :class:`~roomsense.config.StorageConfig`):

* ``max_recording_bytes``: compressed bytes on disk for one recording. The
  size is read from the file while the gzip stream is open, so it lags by the
  compressor's internal buffer (tens of KiB); the file can therefore exceed
  the limit by that much plus one record.
* ``max_recording_seconds`` (and the optional per-recording ``max_seconds``):
  wall-clock duration from start.
* ``max_total_recording_bytes``: counts recording files **and** export zips
  (:func:`quota_used_bytes`). A new recording is refused once they reach it,
  an active recording stops when it would exceed it, and an export that would
  exceed it is refused (:mod:`roomsense.storage.exports`).

Hitting a bound stops the recording with status ``TRUNCATED_LIMIT``; the file
stays valid and replayable. Synthetic data may be recorded (useful for
software tests) but is marked ``synthetic=True`` and can never be used as
validation evidence.

Deleting a recording (:func:`purge_recording`) removes everything derived
from it: the file, its exports (including hidden ``.partial`` leftovers), its
DB rows (recording, label events, and the consent record once no other
recording uses it), every zone model trained on it (files and DB rows), and
the old page images in the SQLite ``-wal`` file (``wal_checkpoint(TRUNCATE)``).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from pydantic import BaseModel, ValidationError

from ..config import StorageConfig
from ..recording_format import RecordingFormatError, RecordingWriter, read_recording
from ..schemas import CsiFrame, InputFormat, QualityFlag, SourceMode
from .db import Database, StorageError
from .models import (
    MAX_LABEL_CHARS,
    ConsentRecord,
    RecordingInfo,
    RecordingStatus,
    new_id,
    validate_id,
)

__all__ = [
    "RECORDINGS_SUBDIR",
    "EXPORTS_SUBDIR",
    "RECORDING_SUFFIX",
    "RecordingRefused",
    "Recorder",
    "recording_path",
    "recording_file_for_read",
    "total_recording_bytes",
    "total_export_bytes",
    "quota_used_bytes",
    "list_recordings",
    "RecordingDeletion",
    "purge_recording",
    "delete_recording",
    "iter_recording",
    "iter_recording_events",
]

log = logging.getLogger(__name__)

RECORDINGS_SUBDIR = "recordings"
EXPORTS_SUBDIR = "exports"
RECORDING_SUFFIX = ".jsonl.gz"

# How often the DB row of an active recording is refreshed, so the UI list and
# crash recovery see roughly current frame/byte counts.
_PROGRESS_INTERVAL_S = 5.0
_MAX_EVENT_DETAIL_CHARS = 1000
_EVENT_KEYS = ("kind", "link_id", "receiver_id", "detail", "host_monotonic_ns", "host_unix_ns")


class RecordingRefused(RuntimeError):
    """A recording operation was refused. ``code`` is machine-readable."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _recordings_dir(data_dir: Path) -> Path:
    return (Path(data_dir) / RECORDINGS_SUBDIR).resolve()


def recording_path(data_dir: Path, recording_id: str) -> Path:
    """Path of a recording file, guaranteed to be directly inside
    ``<data_dir>/recordings``. Raises ``ValueError`` for unsafe IDs.

    The final component is not resolved, so a symlink stays a symlink (the
    caller decides whether to follow it; readers refuse to).
    """
    validate_id(recording_id, "recording_id")
    base = _recordings_dir(data_dir)
    path = base / f"{recording_id}{RECORDING_SUFFIX}"
    if path.parent != base or os.path.commonpath([str(base), str(path)]) != str(base):
        raise ValueError("recording path escapes the recordings directory")
    return path


def recording_file_for_read(data_dir: Path, recording_id: str) -> Path:
    """Existing regular recording file for reading; refuses symlinks so a
    planted link cannot make an export or replay read files outside the data
    directory."""
    path = recording_path(data_dir, recording_id)
    if path.is_symlink():
        raise ValueError(f"recording file for {recording_id} is a symlink; refusing to read it")
    if not path.is_file():
        raise FileNotFoundError(f"recording file for {recording_id} not found")
    return path


def _sum_files(base: Path, suffixes: tuple[str, ...]) -> int:
    if not base.is_dir():
        return 0
    total = 0
    with os.scandir(base) as it:
        for entry in it:
            if entry.name.endswith(suffixes) and entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
    return total


def total_recording_bytes(data_dir: Path) -> int:
    """Total size of recording files on disk (symlinks are not followed)."""
    return _sum_files(_recordings_dir(data_dir), (RECORDING_SUFFIX,))


def total_export_bytes(data_dir: Path) -> int:
    """Total size of export zips on disk, including ``.partial`` files of
    exports being written or left by a crash (symlinks are not followed)."""
    return _sum_files((Path(data_dir) / EXPORTS_SUBDIR).resolve(), (".zip", ".zip.partial"))


def quota_used_bytes(data_dir: Path) -> int:
    """What counts toward ``max_total_recording_bytes``: recordings plus exports."""
    return total_recording_bytes(data_dir) + total_export_bytes(data_dir)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


@dataclass
class _Active:
    info: RecordingInfo
    writer: RecordingWriter
    t0_monotonic_ns: int
    max_bytes: int
    max_bytes_reason: str
    max_seconds: float
    link_ids_seen: set[str] = field(default_factory=set)
    synthetic_seen: bool = False
    frames_rejected: int = 0
    events_written: int = 0
    last_progress_ns: int = 0


class Recorder:
    """Records frames from exactly one session/source into one file at a time.

    Thread-safe: ``write``/``write_event`` are called from the acquisition
    thread while ``start``/``stop`` come from API handlers.

    Create exactly one Recorder per data directory and process: construction
    marks rows left in RECORDING (by a crash) as ERROR / interrupted.
    """

    def __init__(
        self,
        db: Database,
        data_dir: Path,
        storage_cfg: StorageConfig,
        *,
        wall_clock_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        on_auto_stop: Callable[[RecordingInfo], None] | None = None,
    ) -> None:
        self._db = db
        self._data_dir = Path(data_dir)
        self._cfg = storage_cfg
        self._wall = wall_clock_ns
        self._mono = monotonic_ns
        self._on_auto_stop = on_auto_stop
        self._lock = threading.RLock()
        self._active: _Active | None = None
        self._last: RecordingInfo | None = None
        self.recover_interrupted()

    # ------------------------------------------------------------ inspection

    @property
    def active(self) -> RecordingInfo | None:
        with self._lock:
            return None if self._active is None else self._progress_info(self._active)

    @property
    def last_finished(self) -> RecordingInfo | None:
        """The most recently finished recording (e.g. after an auto-stop)."""
        with self._lock:
            return self._last

    def status(self) -> dict[str, Any]:
        with self._lock:
            a = self._active
            if a is None:
                return {"active": False, "recording": None}
            return {
                "active": True,
                "recording": self._progress_info(a).model_dump(mode="json"),
                "frames_rejected": a.frames_rejected,
                "events_written": a.events_written,
                "limit_bytes": a.max_bytes,
                "limit_seconds": a.max_seconds,
            }

    # ----------------------------------------------------------------- start

    def start(
        self,
        *,
        consent: ConsentRecord,
        label: str,
        scenario: str | None = None,
        session_id: str,
        source_mode: SourceMode,
        link_ids: Sequence[str] = (),
        config_version: str | None = None,
        notes: str | None = None,
        max_seconds: float | None = None,
        original_source_mode: SourceMode | None = None,
    ) -> RecordingInfo:
        with self._lock:
            if self._active is not None:
                raise RecordingRefused(
                    "ALREADY_RECORDING", f"recording {self._active.info.recording_id} is still active; stop it first"
                )
            consent = self._validated_consent(consent)
            try:
                validate_id(session_id, "session_id")
                source_mode = SourceMode(source_mode)
                if original_source_mode is not None:
                    original_source_mode = SourceMode(original_source_mode)
            except ValueError as exc:
                raise RecordingRefused("INVALID_ARGUMENT", str(exc)) from exc
            if max_seconds is not None:
                try:
                    max_seconds = float(max_seconds)
                except (TypeError, ValueError):
                    max_seconds = float("nan")
                if not math.isfinite(max_seconds) or max_seconds <= 0:
                    raise RecordingRefused("INVALID_ARGUMENT", "max_seconds must be a positive number")

            used = quota_used_bytes(self._data_dir)
            quota = self._cfg.max_total_recording_bytes
            if used >= quota:
                raise RecordingRefused(
                    "QUOTA_EXCEEDED",
                    f"existing recordings and exports use {used} bytes, at or above "
                    f"max_total_recording_bytes={quota}; delete recordings (their exports go with them) "
                    "before starting a new one",
                )
            remaining = quota - used
            if remaining < self._cfg.max_recording_bytes:
                max_bytes, bytes_reason = remaining, f"total recording quota ({quota} bytes) reached"
            else:
                max_bytes = self._cfg.max_recording_bytes
                bytes_reason = f"max_recording_bytes ({max_bytes}) reached"
            limit_s = self._cfg.max_recording_seconds if max_seconds is None else min(
                float(max_seconds), self._cfg.max_recording_seconds
            )

            # Live and simulated data know their own origin. A replay does not,
            # unless the caller read it from the replayed file's header.
            if original_source_mode is None and source_mode != SourceMode.REPLAY:
                original_source_mode = source_mode
            synthetic = SourceMode.SIMULATION in (source_mode, original_source_mode)

            recording_id = new_id("rec")
            path = recording_path(self._data_dir, recording_id)
            now = self._wall()
            try:
                info = RecordingInfo(
                    recording_id=recording_id,
                    session_id=session_id,
                    created_at_unix_ns=now,
                    source_mode=source_mode,
                    original_source_mode=original_source_mode,
                    label=label,
                    scenario=scenario,
                    status=RecordingStatus.RECORDING,
                    link_ids=sorted({str(x) for x in link_ids}),
                    consent_id=consent.consent_id,
                    notes=notes,
                    synthetic=synthetic,
                    config_version=config_version,
                )
            except ValidationError as exc:
                raise RecordingRefused("INVALID_ARGUMENT", _first_error(exc)) from exc

            self._store_consent(consent)
            try:
                self._db.ensure_session(session_id, source_mode, now)
            except ValueError as exc:
                raise RecordingRefused("INVALID_ARGUMENT", str(exc)) from exc

            header = {
                "recording_id": recording_id,
                "session_id": session_id,
                "source_mode": source_mode.value,
                "original_source_mode": None if original_source_mode is None else original_source_mode.value,
                "synthetic": synthetic,
                "created_at_unix_ns": now,
                "label": info.label,
                "scenario": scenario,
                "link_ids": info.link_ids,
                "consent_id": consent.consent_id,
                "consent_statement_version": consent.statement_version,
                "config_version": config_version,
                "notes": notes,
                "limits": {"max_bytes": max_bytes, "max_seconds": limit_s},
            }
            writer = RecordingWriter(path, header)
            try:
                self._db.add_recording(info)
            except Exception:
                writer.close({"ended_at_unix_ns": self._wall(), "status": RecordingStatus.ERROR.value,
                              "reason": "metadata could not be stored"})
                path.unlink(missing_ok=True)
                raise
            self._active = _Active(
                info=info,
                writer=writer,
                t0_monotonic_ns=self._mono(),
                max_bytes=max_bytes,
                max_bytes_reason=bytes_reason,
                max_seconds=limit_s,
                last_progress_ns=self._mono(),
            )
            log.info("recording %s started (%s, label=%r)", recording_id, source_mode.value, info.label)
            return info

    # ----------------------------------------------------------------- write

    def write(self, frame: CsiFrame) -> bool:
        """Append a frame. Returns False if it was not recorded (no active
        recording, other session/source, or an unrecordable frame)."""
        stopped: RecordingInfo | None = None
        with self._lock:
            a = self._active
            if a is None:
                return False
            # Never mix sources: frames from another session or mode are
            # dropped (a source switch should have stopped the recording).
            if frame.session_id != a.info.session_id or frame.source_mode != a.info.source_mode:
                a.frames_rejected += 1
                return False
            frame = self._bounded_raw_line(frame)
            try:
                a.writer.write_frame(frame)
            except RecordingFormatError:
                a.frames_rejected += 1  # oversized record; nothing was written
                return False
            except (OSError, ValueError) as exc:
                stopped = self._finish(RecordingStatus.ERROR, f"write failed: {exc}")
            else:
                a.link_ids_seen.add(frame.link_id)
                if QualityFlag.SYNTHETIC.value in frame.quality_flags or frame.input_format == InputFormat.SYNTHETIC_V1:
                    a.synthetic_seen = True
                stopped = self._check_limits_locked()
                if stopped is None:
                    self._maybe_checkpoint(a)
        if stopped is not None:
            self._notify(stopped)
            return stopped.status != RecordingStatus.ERROR
        return True

    def write_event(self, event: Mapping[str, Any] | Any) -> bool:
        """Record a link event (DISCONNECTED, RECONNECTING, ...) so replays and
        reports keep the cause of each gap. Only a fixed set of metadata keys
        is kept; free-form event payloads are never written."""
        rec = _sanitize_event(event, self._wall)
        stopped: RecordingInfo | None = None
        with self._lock:
            a = self._active
            if a is None:
                return False
            try:
                a.writer.write_event(rec)
            except RecordingFormatError:
                return False
            except (OSError, ValueError) as exc:
                stopped = self._finish(RecordingStatus.ERROR, f"write failed: {exc}")
            else:
                a.events_written += 1
                stopped = self._check_limits_locked()
        if stopped is not None:
            self._notify(stopped)
            return stopped.status != RecordingStatus.ERROR
        return True

    def check_limits(self) -> RecordingInfo | None:
        """Enforce the duration bound when no frames arrive (call periodically).
        Returns the finished recording if it was stopped."""
        with self._lock:
            stopped = self._check_limits_locked() if self._active is not None else None
        if stopped is not None:
            self._notify(stopped)
        return stopped

    # ------------------------------------------------------------------ stop

    def stop(self, reason: str = "stopped by operator") -> RecordingInfo | None:
        """Finish the active recording (status COMPLETE). None if nothing is
        recording (it may already have stopped at a limit; see
        :attr:`last_finished`)."""
        with self._lock:
            if self._active is None:
                return None
            return self._finish(RecordingStatus.COMPLETE, reason)

    def recover_interrupted(self) -> list[str]:
        """Mark rows left in RECORDING by a crash as ERROR. Counts stay those of
        the last progress checkpoint; the end time stays unknown (None)."""
        with self._lock:
            active_id = None if self._active is None else self._active.info.recording_id
            fixed: list[str] = []
            for info in self._db.list_recordings(status=RecordingStatus.RECORDING, limit=None):
                if info.recording_id == active_id:
                    continue
                try:
                    path = recording_path(self._data_dir, info.recording_id)
                    size = path.stat().st_size if path.is_file() and not path.is_symlink() else info.bytes
                except (OSError, ValueError):
                    size = info.bytes
                self._db.update_recording(
                    info.model_copy(update={
                        "status": RecordingStatus.ERROR,
                        "bytes": size,
                        "stop_reason": "interrupted: the application stopped while recording "
                                       "(counts as of the last checkpoint)",
                    })
                )
                fixed.append(info.recording_id)
            return fixed

    # ------------------------------------------------------------- internals

    @staticmethod
    def _validated_consent(consent: Any) -> ConsentRecord:
        try:
            data = consent.model_dump() if isinstance(consent, BaseModel) else dict(consent)
            # Re-validate even real ConsentRecord objects: model_construct()
            # and attribute assignment both bypass validators.
            return ConsentRecord.model_validate(data)
        except (ValidationError, TypeError, ValueError) as exc:
            detail = _first_error(exc) if isinstance(exc, ValidationError) else str(exc)
            raise RecordingRefused("CONSENT_INVALID", f"recording refused without valid consent: {detail}") from exc

    def _store_consent(self, consent: ConsentRecord) -> None:
        existing = self._db.get_consent(consent.consent_id)
        if existing is None:
            self._db.add_consent(consent)
        elif existing != consent:
            raise RecordingRefused(
                "CONSENT_INVALID", f"consent_id {consent.consent_id} already exists with other content"
            )

    def _bounded_raw_line(self, frame: CsiFrame) -> CsiFrame:
        # Enforced here as well as in the parser so that a recording never
        # holds more of the serial line than the storage config allows.
        raw = frame.raw_line
        if raw is None:
            return frame
        if not self._cfg.keep_raw_lines:
            return dataclasses.replace(frame, raw_line=None)
        if len(raw) > self._cfg.max_raw_line_chars:
            return dataclasses.replace(frame, raw_line=raw[: self._cfg.max_raw_line_chars])
        return frame

    def _elapsed_s(self, a: _Active) -> float:
        return max(0.0, (self._mono() - a.t0_monotonic_ns) / 1e9)

    def _progress_info(self, a: _Active) -> RecordingInfo:
        return a.info.model_copy(update={
            "frames": a.writer.frames,
            "bytes": a.writer.compressed_bytes(),
            "duration_s": self._elapsed_s(a),
            "synthetic": a.info.synthetic or a.synthetic_seen,
        })

    def _maybe_checkpoint(self, a: _Active) -> None:
        now = self._mono()
        if (now - a.last_progress_ns) / 1e9 < _PROGRESS_INTERVAL_S:
            return
        a.last_progress_ns = now
        try:
            self._db.update_recording(self._final_info(a, status=RecordingStatus.RECORDING, ended=None,
                                                       size=a.writer.compressed_bytes(), reason=None))
        except Exception:  # a progress update must never kill the acquisition thread
            log.exception("could not checkpoint recording %s", a.info.recording_id)

    def _check_limits_locked(self) -> RecordingInfo | None:
        a = self._active
        if a is None:
            return None
        if a.writer.compressed_bytes() >= a.max_bytes:
            return self._finish(RecordingStatus.TRUNCATED_LIMIT, a.max_bytes_reason)
        if self._elapsed_s(a) >= a.max_seconds:
            return self._finish(RecordingStatus.TRUNCATED_LIMIT, f"maximum duration ({a.max_seconds:g} s) reached")
        return None

    def _final_info(self, a: _Active, *, status: RecordingStatus, ended: int | None, size: int,
                    reason: str | None) -> RecordingInfo:
        synthetic = a.info.synthetic or a.synthetic_seen
        original = a.info.original_source_mode
        if original is None and a.synthetic_seen:
            original = SourceMode.SIMULATION  # a replay of synthetic frames
        data = a.info.model_dump()
        data.update({
            "status": status,
            "ended_at_unix_ns": ended,
            "frames": a.writer.frames,
            "bytes": size,
            "duration_s": self._elapsed_s(a),
            "link_ids": sorted(set(a.info.link_ids) | a.link_ids_seen),
            "synthetic": synthetic,
            "original_source_mode": original,
            "stop_reason": None if reason is None else reason[:MAX_LABEL_CHARS],
        })
        return RecordingInfo.model_validate(data)

    def _finish(self, status: RecordingStatus, reason: str) -> RecordingInfo:
        a = self._active
        assert a is not None
        self._active = None
        ended = self._wall()
        synthetic = a.info.synthetic or a.synthetic_seen
        footer = {
            "ended_at_unix_ns": ended,
            "status": status.value,
            "reason": reason,
            "synthetic": synthetic,
            "duration_s": self._elapsed_s(a),
            "link_ids_seen": sorted(a.link_ids_seen),
            "frames_rejected": a.frames_rejected,
            "events": a.events_written,
        }
        try:
            size = a.writer.close(footer)
        except (OSError, ValueError) as exc:
            status, reason = RecordingStatus.ERROR, f"{reason}; closing the file failed: {exc}"
            try:
                size = a.writer.path.stat().st_size
            except OSError:
                size = 0
        info = self._final_info(a, status=status, ended=ended, size=size, reason=reason)
        self._last = info
        try:
            self._db.update_recording(info)
        except Exception:
            # The file is already closed and valid. The row stays RECORDING and
            # recover_interrupted() marks it ERROR on the next start.
            log.exception("could not store the final state of recording %s", info.recording_id)
        log.info("recording %s finished: %s (%s)", info.recording_id, status.value, reason)
        return info

    def _notify(self, info: RecordingInfo) -> None:
        if self._on_auto_stop is None:
            return
        try:
            self._on_auto_stop(info)
        except Exception:
            log.exception("on_auto_stop callback failed")


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return str(exc)
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ()))
    return f"{loc}: {e.get('msg')}" if loc else str(e.get("msg"))


def _sanitize_event(event: Mapping[str, Any] | Any, clock: Callable[[], int]) -> dict[str, Any]:
    # LinkEvent objects are duck-typed: importing roomsense.acquisition here
    # would pull the whole acquisition package into the storage layer.
    if isinstance(event, Mapping):
        src: Mapping[str, Any] = event
    elif hasattr(event, "kind") and hasattr(event, "link_id"):
        src = {key: getattr(event, key, None) for key in _EVENT_KEYS}
    else:
        raise TypeError("event must be a mapping or a LinkEvent")
    out: dict[str, Any] = {}
    for key in _EVENT_KEYS:
        val = src.get(key)
        if key in ("host_monotonic_ns", "host_unix_ns"):
            out[key] = val if isinstance(val, int) and not isinstance(val, bool) else None
        elif val is None:
            out[key] = None
        else:
            limit = _MAX_EVENT_DETAIL_CHARS if key == "detail" else MAX_LABEL_CHARS
            out[key] = str(val)[:limit]
    if not out.get("kind"):
        raise ValueError("event needs a kind")
    if out["host_unix_ns"] is None:
        out["host_unix_ns"] = clock()
    return out


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def list_recordings(db: Database, *, session_id: str | None = None, limit: int | None = 1000) -> list[RecordingInfo]:
    return db.list_recordings(session_id=session_id, limit=limit)


@dataclass(frozen=True)
class RecordingDeletion:
    """What :func:`purge_recording` removed."""

    recording_id: str
    deleted: bool  # anything at all was removed
    removed_models: tuple[str, ...] = ()  # zone models trained on this recording (files + DB rows)
    wal_checkpoint_complete: bool = True  # False: another connection kept the -wal file from being truncated


_CHECKPOINT_ATTEMPTS = 3
_CHECKPOINT_RETRY_S = 0.05


def _truncate_wal(db: Database, recording_id: str) -> bool:
    """``wal_checkpoint(TRUNCATE)`` so the deleted rows' old page images do
    not linger in the ``-wal`` file. Retried briefly while another
    connection's read transaction blocks it; SQLite checkpoints again when the
    last connection closes."""
    for attempt in range(_CHECKPOINT_ATTEMPTS):
        try:
            busy, _, _ = db.checkpoint("TRUNCATE")
        except (sqlite3.Error, StorageError):
            log.exception("WAL checkpoint after deleting recording %s failed", recording_id)
            return False
        if not busy:
            return True
        if attempt + 1 < _CHECKPOINT_ATTEMPTS:
            time.sleep(_CHECKPOINT_RETRY_S)
    log.warning("WAL checkpoint after deleting recording %s was blocked by another connection; it completes "
                "when the database is closed", recording_id)
    return False


def purge_recording(db: Database, data_dir: Path, recording_id: str, *, registry: Any | None = None
                    ) -> RecordingDeletion:
    """Delete a recording and everything derived from it (see the module
    docstring). ``deleted`` is True if anything was removed.

    ``registry`` is the :class:`~roomsense.inference.zone.registry.ZoneModelRegistry`
    to clean (default: one on ``data_dir`` and ``db``). A zone model trained
    on the recording must not survive its deletion, so such models are
    removed first, before the recording itself.

    Raises ``ValueError`` for an unsafe ID and :class:`RecordingRefused` while
    the recording is still being written.
    """
    validate_id(recording_id, "recording_id")
    info = db.get_recording(recording_id)
    if info is not None and info.status == RecordingStatus.RECORDING:
        raise RecordingRefused("RECORDING_ACTIVE", "stop the recording before deleting it")
    if registry is None:
        # Imported here: the zone package itself builds on the storage layer.
        from ..inference.zone.registry import ZoneModelRegistry

        registry = ZoneModelRegistry(data_dir, db)
    removed_models = tuple(registry.delete_models_trained_on(recording_id))
    removed = bool(removed_models)
    path = recording_path(data_dir, recording_id)
    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            raise ValueError(f"{path.name} is a directory, not a recording file")
        path.unlink()  # removes a symlink itself, never its target
        removed = True
    exports = (Path(data_dir) / EXPORTS_SUBDIR).resolve()
    if exports.is_dir():
        # '.' cannot occur in an ID, so these patterns cannot match another
        # recording's exports. Hidden ".partial" files are exports that were
        # being written (or were left by a crash).
        for pattern in (f"{recording_id}.export.*.zip", f".{recording_id}.export.*.zip.partial"):
            for f in exports.glob(pattern):
                if f.parent == exports and (f.is_file() or f.is_symlink()):
                    f.unlink()
                    removed = True
    if db.delete_recording(recording_id):
        removed = True
    complete = _truncate_wal(db, recording_id) if removed else True
    return RecordingDeletion(recording_id=recording_id, deleted=removed, removed_models=removed_models,
                             wal_checkpoint_complete=complete)


def delete_recording(db: Database, data_dir: Path, recording_id: str) -> bool:
    """:func:`purge_recording` (file, exports, DB rows, consent, zone models
    trained on it, WAL); returns True if anything was deleted."""
    return purge_recording(db, data_dir, recording_id).deleted


def iter_recording(path: Path) -> Iterator[CsiFrame]:
    """Frames of a recording file, in recorded order."""
    for rec in read_recording(Path(path), decode_frames=True):
        if rec.get("type") == "frame":
            yield rec["frame"]


def iter_recording_events(path: Path) -> Iterator[dict[str, Any]]:
    """Link events (DISCONNECTED etc.) stored in a recording file."""
    for rec in read_recording(Path(path), decode_frames=False):
        if rec.get("type") == "event" and isinstance(rec.get("event"), dict):
            yield rec["event"]
