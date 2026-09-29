"""SQLite metadata store (stdlib :mod:`sqlite3`).

Design notes
------------
* One connection per :class:`Database`, opened with ``check_same_thread=False``
  and guarded by an ``RLock``: FastAPI handlers, the acquisition thread and the
  recorder all share it, and SQLite connections are not safe to use
  concurrently from several threads.
* ``isolation_level=None`` (autocommit) plus explicit ``BEGIN IMMEDIATE`` for
  writes, so every multi-statement change is one transaction and nothing is
  left half-committed by Python's implicit transaction handling.
* WAL journal and ``foreign_keys=ON``. Foreign keys protect consent (a
  recording can never reference a consent record that does not exist) and
  cascade label events when their recording is deleted.
* Schema changes are numbered migrations recorded in ``schema_migrations``.
  Re-running them is a no-op; a database written by a newer version is refused
  instead of being modified.
* Only parameterised SQL. Table/column names are never built from input.

``activity_log`` expects one row per :class:`~roomsense.schemas.ActivityResult`
(i.e. at the processing hop rate, including UNKNOWN / SENSOR_OFFLINE results).
The validation metrics treat a link with no row for longer than the configured
hold time as unobserved, so logging only state changes would under-report
observation time rather than invent it.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..schemas import ActivityResult, ActivityState, CalibrationRecord, RoomGeometry, SourceMode
from .models import (
    ActivityLogEntry,
    ConsentRecord,
    EventKind,
    LabeledEvent,
    RecordingInfo,
    RecordingStatus,
    RoomVersion,
    SessionRecord,
    StoredCalibration,
    ValidationRun,
    ValidationRunStatus,
    ZoneModelRecord,
    validate_id,
)

__all__ = ["Database", "StorageError", "Migration", "MIGRATIONS", "activity_entry_from_result"]

_MAX_LIST = 100_000  # hard ceiling on rows returned by any list_* call


class StorageError(RuntimeError):
    """The database cannot be used safely (closed, newer schema, ...)."""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


def _in(values: Iterable[str]) -> str:
    # Builds a CHECK constraint literal list from *enum values defined in code*
    # (never from user input), so string formatting here is safe.
    return ", ".join(f"'{v}'" for v in values)


_MODES = _in(m.value for m in SourceMode)
_REC_STATUS = _in(s.value for s in RecordingStatus)
_EVENT_KINDS = _in(k.value for k in EventKind)
_ACT_STATES = _in(s.value for s in ActivityState)
_RUN_STATUS = _in(s.value for s in ValidationRunStatus)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "initial schema",
        (
            f"""CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at_unix_ns INTEGER NOT NULL,
                source_mode TEXT NOT NULL CHECK (source_mode IN ({_MODES})),
                ended_at_unix_ns INTEGER,
                notes TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS consents (
                consent_id TEXT PRIMARY KEY,
                created_at_unix_ns INTEGER NOT NULL,
                all_participants_consented INTEGER NOT NULL CHECK (all_participants_consented = 1),
                participant_count INTEGER NOT NULL CHECK (participant_count >= 1),
                purpose TEXT NOT NULL,
                statement_version TEXT NOT NULL,
                statement_text TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS recordings (
                recording_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at_unix_ns INTEGER NOT NULL,
                ended_at_unix_ns INTEGER,
                source_mode TEXT NOT NULL CHECK (source_mode IN ({_MODES})),
                original_source_mode TEXT CHECK (original_source_mode IS NULL OR original_source_mode IN ({_MODES})),
                label TEXT NOT NULL,
                scenario TEXT,
                status TEXT NOT NULL CHECK (status IN ({_REC_STATUS})),
                frames INTEGER NOT NULL DEFAULT 0,
                bytes INTEGER NOT NULL DEFAULT 0,
                duration_s REAL NOT NULL DEFAULT 0,
                link_ids_json TEXT NOT NULL DEFAULT '[]',
                consent_id TEXT NOT NULL REFERENCES consents(consent_id) ON DELETE RESTRICT,
                notes TEXT,
                synthetic INTEGER NOT NULL DEFAULT 0,
                config_version TEXT,
                stop_reason TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_recordings_session ON recordings(session_id)",
            f"""CREATE TABLE IF NOT EXISTS calibrations (
                calibration_id TEXT PRIMARY KEY,
                created_at_unix_ns INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_mode TEXT NOT NULL CHECK (source_mode IN ({_MODES})),
                hardware_signature TEXT NOT NULL,
                room_config_hash TEXT,
                config_version TEXT NOT NULL,
                valid INTEGER NOT NULL,
                invalidated_reason TEXT,
                record_json TEXT NOT NULL,
                baselines_json TEXT NOT NULL DEFAULT '{{}}'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_calibrations_created ON calibrations(created_at_unix_ns)",
            f"""CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                recording_id TEXT REFERENCES recordings(recording_id) ON DELETE CASCADE,
                t_unix_ns INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ({_EVENT_KINDS})),
                label TEXT NOT NULL,
                notes TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_events_session_t ON events(session_id, t_unix_ns)",
            "CREATE INDEX IF NOT EXISTS idx_events_recording ON events(recording_id)",
            f"""CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                link_id TEXT NOT NULL,
                t_end_unix_ns INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({_ACT_STATES})),
                score REAL,
                quality_level TEXT,
                calibration_id TEXT,
                source_mode TEXT NOT NULL CHECK (source_mode IN ({_MODES}))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_activity_session_t ON activity_log(session_id, t_end_unix_ns)",
            "CREATE INDEX IF NOT EXISTS idx_activity_t ON activity_log(t_end_unix_ns)",
            f"""CREATE TABLE IF NOT EXISTS validation_runs (
                run_id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                recording_id TEXT REFERENCES recordings(recording_id) ON DELETE SET NULL,
                source_mode TEXT NOT NULL CHECK (source_mode IN ({_MODES})),
                started_at_unix_ns INTEGER NOT NULL,
                ended_at_unix_ns INTEGER,
                placement TEXT NOT NULL DEFAULT '',
                wall_description TEXT NOT NULL DEFAULT '',
                channel INTEGER,
                conditions TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ({_RUN_STATUS})),
                link_ids_json TEXT NOT NULL DEFAULT '[]'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_vruns_session ON validation_runs(session_id)",
            """CREATE TABLE IF NOT EXISTS zone_models (
                model_id TEXT PRIMARY KEY,
                created_at_unix_ns INTEGER NOT NULL,
                criteria_version TEXT NOT NULL,
                hardware_signature TEXT NOT NULL,
                room_config_hash TEXT NOT NULL,
                config_version TEXT NOT NULL,
                link_ids_json TEXT NOT NULL DEFAULT '[]',
                report_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 0,
                artifact_relpath TEXT,
                synthetic_data_used INTEGER NOT NULL DEFAULT 0,
                CHECK (NOT (enabled = 1 AND synthetic_data_used = 1))
            )""",
            """CREATE TABLE IF NOT EXISTS room_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at_unix_ns INTEGER NOT NULL,
                config_hash TEXT NOT NULL,
                provenance TEXT NOT NULL,
                geometry_json TEXT NOT NULL
            )""",
        ),
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def _clean_json(obj: Any) -> Any:
    """Replace non-finite floats with None (= not measured) so stored JSON is
    strict JSON and never smuggles NaN/Infinity into a report."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(_clean_json(obj), sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _loads(text: str | None, default: Any) -> Any:
    if text is None:
        return default
    return json.loads(text)


def _limit(limit: int | None) -> int:
    if limit is None:
        return _MAX_LIST
    return max(0, min(int(limit), _MAX_LIST))


def activity_entry_from_result(result: ActivityResult) -> ActivityLogEntry:
    """Map a detector output to an activity-log row.

    The timestamp is the end of the analysed window; results without a window
    (e.g. staleness ticks) use the time they were computed.
    """
    prov = result.provenance
    t_end = prov.window_end_unix_ns if prov.window_end_unix_ns is not None else prov.computed_at_unix_ns
    score = result.activity_score
    if score is not None and not math.isfinite(score):
        score = None
    return ActivityLogEntry(
        session_id=prov.session_id,
        link_id=result.link_id,
        t_end_unix_ns=t_end,
        state=result.state.value,
        score=score,
        quality_level=result.quality.level.value,
        calibration_id=prov.calibration_id,
        source_mode=prov.source_mode,
    )


class Database:
    """Typed access to the RoomSense metadata database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 10000")
            self.journal_mode = str(self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._migrate()

    # ------------------------------------------------------------------ core

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._conn is None

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("database is closed")
        return self._conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._c()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                # Also covers a failed COMMIT (e.g. disk full): without the
                # rollback the connection would stay inside the transaction
                # and every later write would fail.
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._c().execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._c().execute(sql, params).fetchone()

    def _migrate(self) -> None:
        conn = self._c()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at_unix_ns INTEGER NOT NULL
            )"""
        )
        current = self.schema_version()
        if current > LATEST_SCHEMA_VERSION:
            raise StorageError(
                f"database schema version {current} is newer than this RoomSense "
                f"({LATEST_SCHEMA_VERSION}); refusing to modify it"
            )
        for mig in MIGRATIONS:
            if mig.version <= current:
                continue
            with self._write() as c:
                for stmt in mig.statements:
                    c.execute(stmt)
                c.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at_unix_ns) VALUES (?, ?, ?)",
                    (mig.version, mig.description, time.time_ns()),
                )

    def schema_version(self) -> int:
        row = self._query_one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"]) if row is not None and row["v"] is not None else 0

    def applied_migrations(self) -> list[tuple[int, str]]:
        return [(int(r["version"]), str(r["description"])) for r in self._query(
            "SELECT version, description FROM schema_migrations ORDER BY version"
        )]

    # -------------------------------------------------------------- sessions

    def add_session(self, session: SessionRecord) -> SessionRecord:
        with self._write() as c:
            c.execute(
                "INSERT INTO sessions (session_id, created_at_unix_ns, source_mode, ended_at_unix_ns, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (session.session_id, session.created_at_unix_ns, session.source_mode.value,
                 session.ended_at_unix_ns, session.notes),
            )
        return session

    def ensure_session(self, session_id: str, source_mode: SourceMode,
                       created_at_unix_ns: int | None = None) -> SessionRecord:
        """Insert the session if it is new. A session has exactly one source
        mode, so a mismatch is an error rather than a silent relabel."""
        validate_id(session_id, "session_id")
        with self._write() as c:
            row = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                rec = SessionRecord(
                    session_id=session_id,
                    created_at_unix_ns=created_at_unix_ns if created_at_unix_ns is not None else time.time_ns(),
                    source_mode=source_mode,
                )
                c.execute(
                    "INSERT INTO sessions (session_id, created_at_unix_ns, source_mode) VALUES (?, ?, ?)",
                    (rec.session_id, rec.created_at_unix_ns, rec.source_mode.value),
                )
                return rec
        existing = self._row_to_session(row)
        if existing.source_mode != source_mode:
            raise ValueError(
                f"session {session_id} is {existing.source_mode.value}, not {source_mode.value}"
            )
        return existing

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        return None if row is None else self._row_to_session(row)

    def list_sessions(self, limit: int | None = 1000) -> list[SessionRecord]:
        rows = self._query("SELECT * FROM sessions ORDER BY created_at_unix_ns DESC LIMIT ?", (_limit(limit),))
        return [self._row_to_session(r) for r in rows]

    def end_session(self, session_id: str, ended_at_unix_ns: int | None = None) -> bool:
        with self._write() as c:
            cur = c.execute(
                "UPDATE sessions SET ended_at_unix_ns = ? WHERE session_id = ? AND ended_at_unix_ns IS NULL",
                (ended_at_unix_ns if ended_at_unix_ns is not None else time.time_ns(), session_id),
            )
            return cur.rowcount > 0

    def delete_session(self, session_id: str) -> bool:
        with self._write() as c:
            return c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,)).rowcount > 0

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            created_at_unix_ns=row["created_at_unix_ns"],
            source_mode=SourceMode(row["source_mode"]),
            ended_at_unix_ns=row["ended_at_unix_ns"],
            notes=row["notes"],
        )

    # -------------------------------------------------------------- consents

    def add_consent(self, consent: ConsentRecord) -> ConsentRecord:
        # Re-validate: a model built with model_construct() skips validators,
        # and consent is the one record that must never be taken on trust.
        consent = ConsentRecord.model_validate(consent.model_dump())
        with self._write() as c:
            c.execute(
                "INSERT INTO consents (consent_id, created_at_unix_ns, all_participants_consented, "
                "participant_count, purpose, statement_version, statement_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (consent.consent_id, consent.created_at_unix_ns, 1, consent.participant_count,
                 consent.purpose, consent.statement_version, consent.statement_text),
            )
        return consent

    def get_consent(self, consent_id: str) -> ConsentRecord | None:
        row = self._query_one("SELECT * FROM consents WHERE consent_id = ?", (consent_id,))
        return None if row is None else self._row_to_consent(row)

    def list_consents(self, limit: int | None = 1000) -> list[ConsentRecord]:
        rows = self._query("SELECT * FROM consents ORDER BY created_at_unix_ns DESC LIMIT ?", (_limit(limit),))
        return [self._row_to_consent(r) for r in rows]

    @staticmethod
    def _row_to_consent(row: sqlite3.Row) -> ConsentRecord:
        return ConsentRecord(
            consent_id=row["consent_id"],
            created_at_unix_ns=row["created_at_unix_ns"],
            all_participants_consented=bool(row["all_participants_consented"]),
            participant_count=row["participant_count"],
            purpose=row["purpose"],
            statement_version=row["statement_version"],
            statement_text=row["statement_text"],
        )

    def delete_consent(self, consent_id: str) -> bool:
        """Delete a consent record. Refused (IntegrityError) while a recording
        still references it: the recording must be deleted first."""
        with self._write() as c:
            return c.execute("DELETE FROM consents WHERE consent_id = ?", (consent_id,)).rowcount > 0

    # ------------------------------------------------------------ recordings

    _REC_COLS = (
        "recording_id, session_id, created_at_unix_ns, ended_at_unix_ns, source_mode, original_source_mode, "
        "label, scenario, status, frames, bytes, duration_s, link_ids_json, consent_id, notes, synthetic, "
        "config_version, stop_reason"
    )

    @staticmethod
    def _rec_params(info: RecordingInfo) -> tuple[Any, ...]:
        return (
            info.recording_id, info.session_id, info.created_at_unix_ns, info.ended_at_unix_ns,
            info.source_mode.value,
            None if info.original_source_mode is None else info.original_source_mode.value,
            info.label, info.scenario, info.status.value, info.frames, info.bytes, float(info.duration_s),
            _dumps(list(info.link_ids)), info.consent_id, info.notes, 1 if info.synthetic else 0,
            info.config_version, info.stop_reason,
        )

    def add_recording(self, info: RecordingInfo) -> RecordingInfo:
        with self._write() as c:
            c.execute(f"INSERT INTO recordings ({self._REC_COLS}) VALUES ({', '.join('?' * 18)})",
                      self._rec_params(info))
        return info

    def update_recording(self, info: RecordingInfo) -> bool:
        """Replace the mutable fields of an existing recording row."""
        with self._write() as c:
            cur = c.execute(
                "UPDATE recordings SET ended_at_unix_ns = ?, original_source_mode = ?, label = ?, scenario = ?, "
                "status = ?, frames = ?, bytes = ?, duration_s = ?, link_ids_json = ?, notes = ?, synthetic = ?, "
                "config_version = ?, stop_reason = ? WHERE recording_id = ?",
                (
                    info.ended_at_unix_ns,
                    None if info.original_source_mode is None else info.original_source_mode.value,
                    info.label, info.scenario, info.status.value, info.frames, info.bytes, float(info.duration_s),
                    _dumps(list(info.link_ids)), info.notes, 1 if info.synthetic else 0,
                    info.config_version, info.stop_reason, info.recording_id,
                ),
            )
            return cur.rowcount > 0

    def get_recording(self, recording_id: str) -> RecordingInfo | None:
        row = self._query_one(f"SELECT {self._REC_COLS} FROM recordings WHERE recording_id = ?", (recording_id,))
        return None if row is None else self._row_to_recording(row)

    def list_recordings(self, *, session_id: str | None = None, status: RecordingStatus | None = None,
                        limit: int | None = 1000) -> list[RecordingInfo]:
        where, params = [], []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        sql = f"SELECT {self._REC_COLS} FROM recordings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at_unix_ns DESC LIMIT ?"
        params.append(_limit(limit))
        return [self._row_to_recording(r) for r in self._query(sql, params)]

    def delete_recording(self, recording_id: str) -> bool:
        """Delete the recording row and its label events (one transaction).
        Validation runs keep existing but lose the link to the recording."""
        with self._write() as c:
            c.execute("DELETE FROM events WHERE recording_id = ?", (recording_id,))
            c.execute("UPDATE validation_runs SET recording_id = NULL WHERE recording_id = ?", (recording_id,))
            return c.execute("DELETE FROM recordings WHERE recording_id = ?", (recording_id,)).rowcount > 0

    @staticmethod
    def _row_to_recording(row: sqlite3.Row) -> RecordingInfo:
        osm = row["original_source_mode"]
        return RecordingInfo(
            recording_id=row["recording_id"],
            session_id=row["session_id"],
            created_at_unix_ns=row["created_at_unix_ns"],
            ended_at_unix_ns=row["ended_at_unix_ns"],
            source_mode=SourceMode(row["source_mode"]),
            original_source_mode=None if osm is None else SourceMode(osm),
            label=row["label"],
            scenario=row["scenario"],
            status=RecordingStatus(row["status"]),
            frames=row["frames"],
            bytes=row["bytes"],
            duration_s=row["duration_s"],
            link_ids=list(_loads(row["link_ids_json"], [])),
            consent_id=row["consent_id"],
            notes=row["notes"],
            synthetic=bool(row["synthetic"]),
            config_version=row["config_version"],
            stop_reason=row["stop_reason"],
        )

    # ---------------------------------------------------------- calibrations

    def add_calibration(self, record: CalibrationRecord, baselines: dict[str, Any] | None = None) -> StoredCalibration:
        stored = StoredCalibration(record=record, baselines=baselines or {})
        with self._write() as c:
            c.execute(
                "INSERT INTO calibrations (calibration_id, created_at_unix_ns, session_id, kind, source_mode, "
                "hardware_signature, room_config_hash, config_version, valid, invalidated_reason, record_json, "
                "baselines_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.calibration_id, record.created_at_unix_ns, record.session_id, record.kind.value,
                    record.source_mode.value, record.hardware_signature, record.room_config_hash,
                    record.processing_config_version, 1 if record.valid else 0, record.invalidated_reason,
                    _dumps(record.model_dump(mode="json")), _dumps(stored.baselines),
                ),
            )
        return stored

    def get_calibration(self, calibration_id: str) -> StoredCalibration | None:
        row = self._query_one("SELECT * FROM calibrations WHERE calibration_id = ?", (calibration_id,))
        return None if row is None else self._row_to_calibration(row)

    def list_calibrations(self, *, valid_only: bool = False, limit: int | None = 100) -> list[StoredCalibration]:
        sql = "SELECT * FROM calibrations"
        if valid_only:
            sql += " WHERE valid = 1"
        sql += " ORDER BY created_at_unix_ns DESC LIMIT ?"
        return [self._row_to_calibration(r) for r in self._query(sql, (_limit(limit),))]

    def latest_valid_calibration(self, *, hardware_signature: str | None = None,
                                 config_version: str | None = None) -> StoredCalibration | None:
        where, params = ["valid = 1"], []
        if hardware_signature is not None:
            where.append("hardware_signature = ?")
            params.append(hardware_signature)
        if config_version is not None:
            where.append("config_version = ?")
            params.append(config_version)
        row = self._query_one(
            "SELECT * FROM calibrations WHERE " + " AND ".join(where) + " ORDER BY created_at_unix_ns DESC LIMIT 1",
            params,
        )
        return None if row is None else self._row_to_calibration(row)

    def update_calibration(self, record: CalibrationRecord, baselines: dict[str, Any] | None = None) -> bool:
        with self._write() as c:
            if baselines is None:
                cur = c.execute(
                    "UPDATE calibrations SET valid = ?, invalidated_reason = ?, record_json = ? "
                    "WHERE calibration_id = ?",
                    (1 if record.valid else 0, record.invalidated_reason, _dumps(record.model_dump(mode="json")),
                     record.calibration_id),
                )
            else:
                cur = c.execute(
                    "UPDATE calibrations SET valid = ?, invalidated_reason = ?, record_json = ?, baselines_json = ? "
                    "WHERE calibration_id = ?",
                    (1 if record.valid else 0, record.invalidated_reason, _dumps(record.model_dump(mode="json")),
                     _dumps(baselines), record.calibration_id),
                )
            return cur.rowcount > 0

    def invalidate_calibration(self, calibration_id: str, reason: str) -> bool:
        """Mark a calibration invalid (kept for the audit trail, never reused)."""
        reason = reason.strip()[:500] or "invalidated"
        stored = self.get_calibration(calibration_id)
        if stored is None:
            return False
        record = stored.record.model_copy(update={"valid": False, "invalidated_reason": reason})
        return self.update_calibration(record)

    def invalidate_all_calibrations(self, reason: str) -> int:
        reason = reason.strip()[:500] or "invalidated"
        ids = [r["calibration_id"] for r in self._query("SELECT calibration_id FROM calibrations WHERE valid = 1")]
        return sum(1 for cid in ids if self.invalidate_calibration(cid, reason))

    def delete_calibration(self, calibration_id: str) -> bool:
        with self._write() as c:
            return c.execute("DELETE FROM calibrations WHERE calibration_id = ?", (calibration_id,)).rowcount > 0

    @staticmethod
    def _row_to_calibration(row: sqlite3.Row) -> StoredCalibration:
        rec = CalibrationRecord.model_validate(_loads(row["record_json"], {}))
        # The columns are authoritative for validity (invalidation updates both,
        # but the column is what queries filter on).
        rec = rec.model_copy(update={"valid": bool(row["valid"]), "invalidated_reason": row["invalidated_reason"]})
        return StoredCalibration(record=rec, baselines=_loads(row["baselines_json"], {}))

    # ---------------------------------------------------------------- events

    def add_event(self, event: LabeledEvent) -> LabeledEvent:
        with self._write() as c:
            c.execute(
                "INSERT INTO events (event_id, session_id, recording_id, t_unix_ns, kind, label, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.event_id, event.session_id, event.recording_id, event.t_unix_ns, event.kind.value,
                 event.label, event.notes),
            )
        return event

    def get_event(self, event_id: str) -> LabeledEvent | None:
        row = self._query_one("SELECT * FROM events WHERE event_id = ?", (event_id,))
        return None if row is None else self._row_to_event(row)

    def list_events(self, *, session_id: str | None = None, recording_id: str | None = None,
                    start_unix_ns: int | None = None, end_unix_ns: int | None = None,
                    limit: int | None = 10_000) -> list[LabeledEvent]:
        """Events in time order. ``start``/``end`` are inclusive bounds."""
        where, params = [], []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if recording_id is not None:
            where.append("recording_id = ?")
            params.append(recording_id)
        if start_unix_ns is not None:
            where.append("t_unix_ns >= ?")
            params.append(int(start_unix_ns))
        if end_unix_ns is not None:
            where.append("t_unix_ns <= ?")
            params.append(int(end_unix_ns))
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t_unix_ns ASC, rowid ASC LIMIT ?"
        params.append(_limit(limit))
        return [self._row_to_event(r) for r in self._query(sql, params)]

    def update_event(self, event: LabeledEvent) -> bool:
        with self._write() as c:
            cur = c.execute(
                "UPDATE events SET t_unix_ns = ?, kind = ?, label = ?, notes = ? WHERE event_id = ?",
                (event.t_unix_ns, event.kind.value, event.label, event.notes, event.event_id),
            )
            return cur.rowcount > 0

    def delete_event(self, event_id: str) -> bool:
        with self._write() as c:
            return c.execute("DELETE FROM events WHERE event_id = ?", (event_id,)).rowcount > 0

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LabeledEvent:
        return LabeledEvent(
            event_id=row["event_id"],
            session_id=row["session_id"],
            recording_id=row["recording_id"],
            t_unix_ns=row["t_unix_ns"],
            kind=EventKind(row["kind"]),
            label=row["label"],
            notes=row["notes"],
        )

    # ---------------------------------------------------------- activity log

    def add_activity(self, entry: ActivityLogEntry) -> None:
        self.add_activity_many([entry])

    def add_activity_result(self, result: ActivityResult) -> None:
        self.add_activity(activity_entry_from_result(result))

    def add_activity_many(self, entries: Sequence[ActivityLogEntry]) -> int:
        if not entries:
            return 0
        with self._write() as c:
            c.executemany(
                "INSERT INTO activity_log (session_id, link_id, t_end_unix_ns, state, score, quality_level, "
                "calibration_id, source_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (e.session_id, e.link_id, e.t_end_unix_ns, e.state,
                     e.score if e.score is None or math.isfinite(e.score) else None,
                     e.quality_level, e.calibration_id, e.source_mode.value)
                    for e in entries
                ],
            )
        return len(entries)

    def list_activity(self, *, session_id: str | None = None, link_id: str | None = None,
                      start_unix_ns: int | None = None, end_unix_ns: int | None = None,
                      limit: int | None = None) -> list[ActivityLogEntry]:
        """Activity rows in time order; ``start``/``end`` are inclusive bounds."""
        where, params = [], []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if link_id is not None:
            where.append("link_id = ?")
            params.append(link_id)
        if start_unix_ns is not None:
            where.append("t_end_unix_ns >= ?")
            params.append(int(start_unix_ns))
        if end_unix_ns is not None:
            where.append("t_end_unix_ns <= ?")
            params.append(int(end_unix_ns))
        sql = "SELECT * FROM activity_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t_end_unix_ns ASC, id ASC LIMIT ?"
        params.append(_limit(limit))
        return [
            ActivityLogEntry(
                session_id=r["session_id"], link_id=r["link_id"], t_end_unix_ns=r["t_end_unix_ns"],
                state=r["state"], score=r["score"], quality_level=r["quality_level"],
                calibration_id=r["calibration_id"], source_mode=SourceMode(r["source_mode"]),
            )
            for r in self._query(sql, params)
        ]

    def count_activity(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM activity_log")
        return int(row["n"]) if row is not None else 0

    def prune_activity_log(self, max_rows: int, *, protect_validation_runs: bool = True) -> int:
        """Delete the oldest rows so at most ``max_rows`` remain; returns the
        number deleted.

        With ``protect_validation_runs`` the rows inside a validation run's time
        span (same session) are kept even if that leaves more than ``max_rows``:
        they are the evidence behind the validation report, and the user
        removes them by deleting the run, not by accident through rotation.
        """
        if max_rows < 0:
            raise ValueError("max_rows must be >= 0")
        with self._write() as c:
            total = int(c.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0])
            excess = total - max_rows
            if excess <= 0:
                return 0
            if protect_validation_runs:
                cur = c.execute(
                    """DELETE FROM activity_log WHERE id IN (
                        SELECT a.id FROM activity_log a
                        WHERE NOT EXISTS (
                            SELECT 1 FROM validation_runs v
                            WHERE v.session_id = a.session_id
                              AND a.t_end_unix_ns >= v.started_at_unix_ns
                              AND (v.ended_at_unix_ns IS NULL OR a.t_end_unix_ns <= v.ended_at_unix_ns)
                        )
                        ORDER BY a.t_end_unix_ns ASC, a.id ASC LIMIT ?
                    )""",
                    (excess,),
                )
            else:
                cur = c.execute(
                    "DELETE FROM activity_log WHERE id IN "
                    "(SELECT id FROM activity_log ORDER BY t_end_unix_ns ASC, id ASC LIMIT ?)",
                    (excess,),
                )
            return cur.rowcount

    def delete_activity(self, session_id: str) -> int:
        with self._write() as c:
            return c.execute("DELETE FROM activity_log WHERE session_id = ?", (session_id,)).rowcount

    # ------------------------------------------------------- validation runs

    _RUN_COLS = (
        "run_id, scenario_id, session_id, recording_id, source_mode, started_at_unix_ns, ended_at_unix_ns, "
        "placement, wall_description, channel, conditions, notes, status, link_ids_json"
    )

    def add_validation_run(self, run: ValidationRun) -> ValidationRun:
        with self._write() as c:
            c.execute(
                f"INSERT INTO validation_runs ({self._RUN_COLS}) VALUES ({', '.join('?' * 14)})",
                (run.run_id, run.scenario_id, run.session_id, run.recording_id, run.source_mode.value,
                 run.started_at_unix_ns, run.ended_at_unix_ns, run.placement, run.wall_description, run.channel,
                 run.conditions, run.notes, run.status.value, _dumps(list(run.link_ids))),
            )
        return run

    def update_validation_run(self, run: ValidationRun) -> bool:
        with self._write() as c:
            cur = c.execute(
                "UPDATE validation_runs SET scenario_id = ?, recording_id = ?, ended_at_unix_ns = ?, placement = ?, "
                "wall_description = ?, channel = ?, conditions = ?, notes = ?, status = ?, link_ids_json = ? "
                "WHERE run_id = ?",
                (run.scenario_id, run.recording_id, run.ended_at_unix_ns, run.placement, run.wall_description,
                 run.channel, run.conditions, run.notes, run.status.value, _dumps(list(run.link_ids)), run.run_id),
            )
            return cur.rowcount > 0

    def end_validation_run(self, run_id: str, *, ended_at_unix_ns: int | None = None,
                           status: ValidationRunStatus = ValidationRunStatus.COMPLETE) -> ValidationRun | None:
        run = self.get_validation_run(run_id)
        if run is None:
            return None
        ended = ended_at_unix_ns if ended_at_unix_ns is not None else time.time_ns()
        run = ValidationRun.model_validate({**run.model_dump(), "ended_at_unix_ns": ended, "status": status})
        self.update_validation_run(run)
        return run

    def get_validation_run(self, run_id: str) -> ValidationRun | None:
        row = self._query_one(f"SELECT {self._RUN_COLS} FROM validation_runs WHERE run_id = ?", (run_id,))
        return None if row is None else self._row_to_run(row)

    def list_validation_runs(self, *, session_id: str | None = None, scenario_id: str | None = None,
                             limit: int | None = 10_000) -> list[ValidationRun]:
        where, params = [], []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if scenario_id is not None:
            where.append("scenario_id = ?")
            params.append(scenario_id)
        sql = f"SELECT {self._RUN_COLS} FROM validation_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at_unix_ns ASC, run_id ASC LIMIT ?"
        params.append(_limit(limit))
        return [self._row_to_run(r) for r in self._query(sql, params)]

    def delete_validation_run(self, run_id: str) -> bool:
        with self._write() as c:
            return c.execute("DELETE FROM validation_runs WHERE run_id = ?", (run_id,)).rowcount > 0

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> ValidationRun:
        return ValidationRun(
            run_id=row["run_id"], scenario_id=row["scenario_id"], session_id=row["session_id"],
            recording_id=row["recording_id"], source_mode=SourceMode(row["source_mode"]),
            started_at_unix_ns=row["started_at_unix_ns"], ended_at_unix_ns=row["ended_at_unix_ns"],
            placement=row["placement"], wall_description=row["wall_description"], channel=row["channel"],
            conditions=row["conditions"], notes=row["notes"], status=ValidationRunStatus(row["status"]),
            link_ids=list(_loads(row["link_ids_json"], [])),
        )

    # ------------------------------------------------------------ zone models

    def add_zone_model(self, model: ZoneModelRecord) -> ZoneModelRecord:
        with self._write() as c:
            c.execute(
                "INSERT INTO zone_models (model_id, created_at_unix_ns, criteria_version, hardware_signature, "
                "room_config_hash, config_version, link_ids_json, report_json, enabled, artifact_relpath, "
                "synthetic_data_used) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model.model_id, model.created_at_unix_ns, model.criteria_version, model.hardware_signature,
                 model.room_config_hash, model.config_version, _dumps(list(model.link_ids)), _dumps(model.report),
                 1 if model.enabled else 0, model.artifact_relpath, 1 if model.synthetic_data_used else 0),
            )
        return model

    def get_zone_model(self, model_id: str) -> ZoneModelRecord | None:
        row = self._query_one("SELECT * FROM zone_models WHERE model_id = ?", (model_id,))
        return None if row is None else self._row_to_zone_model(row)

    def list_zone_models(self, *, enabled_only: bool = False, limit: int | None = 100) -> list[ZoneModelRecord]:
        sql = "SELECT * FROM zone_models"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at_unix_ns DESC LIMIT ?"
        return [self._row_to_zone_model(r) for r in self._query(sql, (_limit(limit),))]

    def update_zone_model(self, model: ZoneModelRecord) -> bool:
        # Re-validate so enabling a model trained on synthetic data is refused
        # even when the caller mutated a model in place.
        model = ZoneModelRecord.model_validate(model.model_dump())
        with self._write() as c:
            cur = c.execute(
                "UPDATE zone_models SET criteria_version = ?, hardware_signature = ?, room_config_hash = ?, "
                "config_version = ?, link_ids_json = ?, report_json = ?, enabled = ?, artifact_relpath = ?, "
                "synthetic_data_used = ? WHERE model_id = ?",
                (model.criteria_version, model.hardware_signature, model.room_config_hash, model.config_version,
                 _dumps(list(model.link_ids)), _dumps(model.report), 1 if model.enabled else 0,
                 model.artifact_relpath, 1 if model.synthetic_data_used else 0, model.model_id),
            )
            return cur.rowcount > 0

    def set_zone_model_enabled(self, model_id: str, enabled: bool) -> bool:
        model = self.get_zone_model(model_id)
        if model is None:
            return False
        return self.update_zone_model(model.model_copy(update={"enabled": enabled}))

    def delete_zone_model(self, model_id: str) -> bool:
        with self._write() as c:
            return c.execute("DELETE FROM zone_models WHERE model_id = ?", (model_id,)).rowcount > 0

    @staticmethod
    def _row_to_zone_model(row: sqlite3.Row) -> ZoneModelRecord:
        return ZoneModelRecord(
            model_id=row["model_id"], created_at_unix_ns=row["created_at_unix_ns"],
            criteria_version=row["criteria_version"], hardware_signature=row["hardware_signature"],
            room_config_hash=row["room_config_hash"], config_version=row["config_version"],
            link_ids=list(_loads(row["link_ids_json"], [])), report=_loads(row["report_json"], {}),
            enabled=bool(row["enabled"]), artifact_relpath=row["artifact_relpath"],
            synthetic_data_used=bool(row["synthetic_data_used"]),
        )

    # ---------------------------------------------------------- room versions

    def add_room_version(self, room: RoomGeometry, created_at_unix_ns: int | None = None) -> RoomVersion:
        created = created_at_unix_ns if created_at_unix_ns is not None else time.time_ns()
        geometry = room.model_dump(mode="json")
        with self._write() as c:
            cur = c.execute(
                "INSERT INTO room_versions (created_at_unix_ns, config_hash, provenance, geometry_json) "
                "VALUES (?, ?, ?, ?)",
                (created, room.config_hash(), room.provenance.value, _dumps(geometry)),
            )
            version_id = int(cur.lastrowid or 0)
        return RoomVersion(version_id=version_id, created_at_unix_ns=created, config_hash=room.config_hash(),
                           provenance=room.provenance.value, geometry=geometry)

    def get_room_version(self, version_id: int) -> RoomVersion | None:
        row = self._query_one("SELECT * FROM room_versions WHERE version_id = ?", (int(version_id),))
        return None if row is None else self._row_to_room(row)

    def latest_room_version(self) -> RoomVersion | None:
        row = self._query_one("SELECT * FROM room_versions ORDER BY version_id DESC LIMIT 1")
        return None if row is None else self._row_to_room(row)

    def list_room_versions(self, limit: int | None = 100) -> list[RoomVersion]:
        rows = self._query("SELECT * FROM room_versions ORDER BY version_id DESC LIMIT ?", (_limit(limit),))
        return [self._row_to_room(r) for r in rows]

    def delete_room_version(self, version_id: int) -> bool:
        with self._write() as c:
            return c.execute("DELETE FROM room_versions WHERE version_id = ?", (int(version_id),)).rowcount > 0

    @staticmethod
    def _row_to_room(row: sqlite3.Row) -> RoomVersion:
        return RoomVersion(
            version_id=row["version_id"], created_at_unix_ns=row["created_at_unix_ns"],
            config_hash=row["config_hash"], provenance=row["provenance"],
            geometry=_loads(row["geometry_json"], {}),
        )
