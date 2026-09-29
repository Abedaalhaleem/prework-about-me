"""Typed records persisted by :mod:`roomsense.storage`.

Every identifier that can end up in a file name or a SQL row is restricted to
``^[A-Za-z0-9_-]{1,64}$``. That rules out path separators, ``..``, dots and
glob characters, so an ID can never escape the data directory or match files
belonging to another recording.

Consent
-------
Recording is opt-in. :class:`ConsentRecord` refuses to exist unless the
operator confirms that *every* person present agreed, and it deliberately has
no field for names or other identifying details (``extra="forbid"`` keeps it
that way).
"""

from __future__ import annotations

import enum
import re
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..schemas import ActivityState, CalibrationRecord, SourceMode

__all__ = [
    "ID_PATTERN",
    "ID_RE",
    "validate_id",
    "new_id",
    "CONSENT_STATEMENT_VERSION",
    "CONSENT_STATEMENT_V1",
    "CONSENT_STATEMENTS",
    "MAX_LABEL_CHARS",
    "MAX_NOTES_CHARS",
    "SessionRecord",
    "ConsentRecord",
    "RecordingStatus",
    "RecordingInfo",
    "EventKind",
    "LabeledEvent",
    "ValidationRunStatus",
    "ValidationRun",
    "StoredCalibration",
    "ZoneModelRecord",
    "RoomVersion",
    "ActivityLogEntry",
]

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
ID_RE = re.compile(ID_PATTERN)

MAX_LABEL_CHARS = 200
MAX_NOTES_CHARS = 4000
MAX_PURPOSE_CHARS = 500


def validate_id(value: Any, what: str = "id") -> str:
    """Return ``value`` if it is a safe identifier, else raise ``ValueError``.

    ``re.fullmatch`` is used instead of ``match`` because ``$`` also matches
    before a trailing newline, which would let ``"abc\\n"`` through.
    """
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {what}: must match {ID_PATTERN}")
    return value


def new_id(prefix: str) -> str:
    """Random, non-guessable ID such as ``rec_3f2a...`` (always matches ID_PATTERN)."""
    validate_id(prefix, "id prefix")
    ident = f"{prefix}_{uuid.uuid4().hex[:24]}"
    return validate_id(ident[:64])


# ---------------------------------------------------------------------------
# Consent statement
# ---------------------------------------------------------------------------

CONSENT_STATEMENT_VERSION = "consent-v1"

CONSENT_STATEMENT_V1 = (
    "RoomSense recording consent (consent-v1)\n"
    "\n"
    "1. Recording is opt-in. Nothing is recorded unless the operator starts a "
    "recording and confirms this statement.\n"
    "2. Recording happens only in a space that the operator owns or controls.\n"
    "3. Every person present in the sensed area has been told what is being "
    "recorded (Wi-Fi channel state information from the operator's own ESP32 "
    "boards) and why, and has agreed to it.\n"
    "4. Recording is local: recordings are stored only on this computer. "
    "Nothing is uploaded or shared by the application.\n"
    "5. Any recording can be deleted at any time from the Recordings page.\n"
    "6. No identification is performed. RoomSense does not recognise, name or "
    "count people, and no names are collected with this consent record.\n"
)

CONSENT_STATEMENTS: dict[str, str] = {CONSENT_STATEMENT_VERSION: CONSENT_STATEMENT_V1}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


def _check_ids(v: Any, what: str) -> Any:
    if v is None:
        return v
    return validate_id(v, what)


# ---------------------------------------------------------------------------
# Sessions and consent
# ---------------------------------------------------------------------------


class SessionRecord(_Model):
    """One acquisition session (one selected source from start to stop)."""

    session_id: str = Field(pattern=ID_PATTERN)
    created_at_unix_ns: int = Field(ge=0)
    source_mode: SourceMode
    ended_at_unix_ns: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)


class ConsentRecord(_Model):
    """Recorded, versioned consent. Construction fails unless every
    participant consented, so a ``ConsentRecord`` object is itself proof that
    the confirmation was given."""

    consent_id: str = Field(default_factory=lambda: new_id("consent"), pattern=ID_PATTERN)
    created_at_unix_ns: int = Field(default_factory=time.time_ns, ge=0)
    all_participants_consented: bool
    participant_count: int = Field(ge=1, le=1000)
    purpose: str = Field(min_length=1, max_length=MAX_PURPOSE_CHARS)
    statement_version: str = CONSENT_STATEMENT_VERSION
    statement_text: str = CONSENT_STATEMENT_V1

    @field_validator("all_participants_consented")
    @classmethod
    def _must_consent(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError(
                "recording is refused: every person present must have been informed and "
                "must have agreed (all_participants_consented must be true)"
            )
        return v

    @field_validator("purpose")
    @classmethod
    def _purpose_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("purpose must not be empty")
        return v

    @model_validator(mode="after")
    def _statement_matches(self) -> "ConsentRecord":
        if self.statement_version != CONSENT_STATEMENT_VERSION:
            raise ValueError(
                f"statement_version must be {CONSENT_STATEMENT_VERSION!r} (got {self.statement_version!r})"
            )
        # The stored text must be exactly what the operator was shown, so the
        # record cannot claim agreement to a different (e.g. weaker) statement.
        if self.statement_text != CONSENT_STATEMENTS[self.statement_version]:
            raise ValueError("statement_text does not match the consent statement for this version")
        return self

    def summary(self) -> dict[str, Any]:
        """Consent facts without free text other than the purpose (for exports)."""
        return {
            "consent_id": self.consent_id,
            "created_at_unix_ns": self.created_at_unix_ns,
            "all_participants_consented": self.all_participants_consented,
            "participant_count": self.participant_count,
            "purpose": self.purpose,
            "statement_version": self.statement_version,
        }


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------


class RecordingStatus(str, enum.Enum):
    RECORDING = "RECORDING"
    COMPLETE = "COMPLETE"
    TRUNCATED_LIMIT = "TRUNCATED_LIMIT"  # auto-stopped at a byte/second/quota limit
    ERROR = "ERROR"  # write failure or the app stopped while recording


class RecordingInfo(_Model):
    """Metadata for one raw CSI recording (the frames live in
    ``data/recordings/<recording_id>.jsonl.gz``)."""

    recording_id: str = Field(pattern=ID_PATTERN)
    session_id: str = Field(pattern=ID_PATTERN)
    created_at_unix_ns: int = Field(ge=0)
    ended_at_unix_ns: int | None = Field(default=None, ge=0)
    source_mode: SourceMode
    # Mode of the data as originally captured. For a recording of a replay it
    # is the replayed file's mode; None means it could not be determined.
    original_source_mode: SourceMode | None
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    scenario: str | None = Field(default=None, max_length=MAX_LABEL_CHARS)
    status: RecordingStatus
    frames: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)  # compressed size on disk
    duration_s: float = Field(default=0.0, ge=0)
    link_ids: list[str] = Field(default_factory=list)
    consent_id: str = Field(pattern=ID_PATTERN)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)
    # True when any part of the data is synthetic. Synthetic recordings are
    # allowed for software testing but can never be validation evidence.
    synthetic: bool = False
    config_version: str | None = None
    stop_reason: str | None = Field(default=None, max_length=MAX_LABEL_CHARS)

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("label must not be empty")
        return v

    @model_validator(mode="after")
    def _synthetic_consistent(self) -> "RecordingInfo":
        if SourceMode.SIMULATION in (self.source_mode, self.original_source_mode) and not self.synthetic:
            raise ValueError("a recording of SIMULATION data must have synthetic=True")
        return self

    @property
    def usable_as_validation_evidence(self) -> bool:
        """Only live, non-synthetic captures can ever back a validation claim."""
        return (
            self.source_mode == SourceMode.LIVE
            and self.original_source_mode == SourceMode.LIVE
            and not self.synthetic
        )


# ---------------------------------------------------------------------------
# Labelled events and validation runs
# ---------------------------------------------------------------------------


class EventKind(str, enum.Enum):
    MARK = "MARK"  # a single point in time
    START = "START"  # opens an interval with the same label
    END = "END"  # closes the most recent open interval with the same label


class LabeledEvent(_Model):
    """A ground-truth label entered by the operator (never inferred)."""

    event_id: str = Field(default_factory=lambda: new_id("evt"), pattern=ID_PATTERN)
    session_id: str = Field(pattern=ID_PATTERN)
    recording_id: str | None = Field(default=None, pattern=ID_PATTERN)
    t_unix_ns: int = Field(ge=0)
    kind: EventKind
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("label must not be empty")
        return v


class ValidationRunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"


class ValidationRun(_Model):
    """One execution of a through-wall protocol scenario with its setup metadata."""

    run_id: str = Field(default_factory=lambda: new_id("vrun"), pattern=ID_PATTERN)
    scenario_id: str = Field(pattern=ID_PATTERN)
    session_id: str = Field(pattern=ID_PATTERN)
    recording_id: str | None = Field(default=None, pattern=ID_PATTERN)
    source_mode: SourceMode
    started_at_unix_ns: int = Field(ge=0)
    ended_at_unix_ns: int | None = Field(default=None, ge=0)
    # Free text exactly as the operator describes it; never inferred.
    placement: str = Field(default="", max_length=MAX_NOTES_CHARS)
    wall_description: str = Field(default="", max_length=MAX_NOTES_CHARS)
    channel: int | None = Field(default=None, ge=1, le=233)
    conditions: str = Field(default="", max_length=MAX_NOTES_CHARS)
    notes: str = Field(default="", max_length=MAX_NOTES_CHARS)
    status: ValidationRunStatus = ValidationRunStatus.RUNNING
    # Links that were supposed to be active. A listed link without activity
    # rows counts as unobserved instead of silently vanishing from the
    # any-link combination. Empty => the links present in the activity log.
    link_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _times(self) -> "ValidationRun":
        if self.ended_at_unix_ns is not None and self.ended_at_unix_ns < self.started_at_unix_ns:
            raise ValueError("ended_at_unix_ns must not be before started_at_unix_ns")
        return self

    @property
    def duration_s(self) -> float | None:
        if self.ended_at_unix_ns is None:
            return None
        return (self.ended_at_unix_ns - self.started_at_unix_ns) / 1e9


# ---------------------------------------------------------------------------
# Calibrations, zone models, room versions, activity log
# ---------------------------------------------------------------------------


class StoredCalibration(_Model):
    """A calibration record plus the serialised per-link baselines."""

    record: CalibrationRecord
    baselines: dict[str, Any] = Field(default_factory=dict)


class ZoneModelRecord(_Model):
    model_id: str = Field(pattern=ID_PATTERN)
    created_at_unix_ns: int = Field(ge=0)
    criteria_version: str
    hardware_signature: str
    room_config_hash: str
    config_version: str
    link_ids: list[str] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False
    # Relative to the data directory; never absolute so a DB row cannot point
    # the loader at an arbitrary file.
    artifact_relpath: str | None = None
    synthetic_data_used: bool = False

    @field_validator("artifact_relpath")
    @classmethod
    def _relative(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parts = v.replace("\\", "/").split("/")
        if v.startswith(("/", "\\")) or ":" in v or any(p in ("", ".", "..") for p in parts):
            raise ValueError("artifact_relpath must be a plain relative path inside the data directory")
        return v

    @model_validator(mode="after")
    def _synthetic_never_enabled(self) -> "ZoneModelRecord":
        if self.enabled and self.synthetic_data_used:
            raise ValueError("a zone model trained with synthetic data can never be enabled")
        return self


class RoomVersion(_Model):
    version_id: int | None = None  # assigned by the database
    created_at_unix_ns: int = Field(ge=0)
    config_hash: str
    provenance: str
    geometry: dict[str, Any]


class ActivityLogEntry(_Model):
    """One motion-detector decision for one link (one row per ActivityResult)."""

    session_id: str = Field(pattern=ID_PATTERN)
    link_id: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    t_end_unix_ns: int = Field(ge=0)
    state: str
    score: float | None = None
    quality_level: str | None = None
    calibration_id: str | None = None
    source_mode: SourceMode

    @field_validator("state")
    @classmethod
    def _known_state(cls, v: str) -> str:
        return ActivityState(v).value
