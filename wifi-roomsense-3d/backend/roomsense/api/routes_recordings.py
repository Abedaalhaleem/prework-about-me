"""Consented recordings (start/stop/list/delete/export) and labelled events."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..runtime import AppRuntime
from ..storage.models import (
    CONSENT_STATEMENT_VERSION,
    CONSENT_STATEMENTS,
    ID_PATTERN,
    MAX_LABEL_CHARS,
    MAX_NOTES_CHARS,
    EventKind,
    LabeledEvent,
    RecordingInfo,
)
from .deps import get_runtime

router = APIRouter(prefix="/api", tags=["recordings"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentIn(_Body):
    """Consent as sent by the UI. The statement text is filled in by the server
    from its own table, so the client cannot claim agreement to another text.
    ``all_participants_consented`` is strict (only JSON true counts); false is
    refused by the runtime with an explanation."""

    all_participants_consented: StrictBool
    participant_count: int = Field(ge=1, le=1000)
    purpose: str = Field(min_length=1, max_length=500)
    statement_version: str = CONSENT_STATEMENT_VERSION


class RecordingStartRequest(_Body):
    consent: ConsentIn
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    scenario: str | None = Field(default=None, max_length=MAX_LABEL_CHARS)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)
    max_seconds: float | None = Field(default=None, gt=0)


class EventRequest(_Body):
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    kind: EventKind
    t_unix_ns: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)


@router.get("/recordings", response_model=list[RecordingInfo])
def list_recordings(rt: AppRuntime = Depends(get_runtime)) -> list[RecordingInfo]:
    return rt.list_recordings()


@router.get("/recordings/consent-statement")
def consent_statement() -> dict[str, str]:
    return {"version": CONSENT_STATEMENT_VERSION, "text": CONSENT_STATEMENTS[CONSENT_STATEMENT_VERSION]}


@router.post("/recordings/start", response_model=RecordingInfo)
def start_recording(body: RecordingStartRequest, rt: AppRuntime = Depends(get_runtime)) -> RecordingInfo:
    return rt.start_recording(consent=body.consent.model_dump(), label=body.label, scenario=body.scenario,
                              notes=body.notes, max_seconds=body.max_seconds)


@router.post("/recordings/stop", response_model=RecordingInfo)
def stop_recording(rt: AppRuntime = Depends(get_runtime)) -> RecordingInfo:
    return rt.stop_recording()


@router.delete("/recordings/{recording_id}")
def delete_recording(recording_id: str, rt: AppRuntime = Depends(get_runtime)) -> dict[str, bool]:
    return {"deleted": rt.delete_recording(recording_id)}


@router.get("/recordings/{recording_id}/export")
def export_recording(recording_id: str, rt: AppRuntime = Depends(get_runtime)) -> FileResponse:
    path = rt.export_recording(recording_id)
    return FileResponse(path, media_type="application/zip", filename=f"roomsense-{recording_id}.zip")


@router.get("/events", response_model=list[LabeledEvent])
def list_events(
    session_id: str | None = Query(default=None, pattern=ID_PATTERN),
    limit: int = Query(default=500, ge=1, le=10_000),
    rt: AppRuntime = Depends(get_runtime),
) -> list[LabeledEvent]:
    return rt.list_events(session_id=session_id, limit=limit)


@router.post("/events", response_model=LabeledEvent)
def add_event(body: EventRequest, rt: AppRuntime = Depends(get_runtime)) -> LabeledEvent:
    return rt.add_event(label=body.label, kind=body.kind, t_unix_ns=body.t_unix_ns, notes=body.notes)
