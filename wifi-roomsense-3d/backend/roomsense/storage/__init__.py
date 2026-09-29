"""RoomSense local storage: SQLite metadata, bounded consented recordings,
and zip exports. Everything stays in the local data directory."""

from .db import Database, StorageError
from .models import (
    CONSENT_STATEMENT_V1,
    CONSENT_STATEMENT_VERSION,
    ActivityLogEntry,
    ConsentRecord,
    EventKind,
    LabeledEvent,
    RecordingInfo,
    RecordingStatus,
    ValidationRun,
    ValidationRunStatus,
)

__all__ = [
    "Database",
    "StorageError",
    "CONSENT_STATEMENT_V1",
    "CONSENT_STATEMENT_VERSION",
    "ActivityLogEntry",
    "ConsentRecord",
    "EventKind",
    "LabeledEvent",
    "RecordingInfo",
    "RecordingStatus",
    "ValidationRun",
    "ValidationRunStatus",
]
