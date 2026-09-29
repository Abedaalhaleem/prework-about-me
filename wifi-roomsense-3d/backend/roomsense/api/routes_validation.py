"""Through-wall validation protocol, runs and report; zone status and training."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from ..runtime import AppRuntime
from ..storage.models import ID_PATTERN, MAX_LABEL_CHARS, MAX_NOTES_CHARS, ValidationRun
from ..validation.protocol import protocol_as_dict
from ..validation.report import render_markdown
from .deps import get_runtime

router = APIRouter(prefix="/api", tags=["validation"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationRunRequest(_Body):
    scenario_id: str = Field(pattern=ID_PATTERN)
    placement: str = Field(default="", max_length=MAX_NOTES_CHARS)
    wall_description: str = Field(default="", max_length=MAX_NOTES_CHARS)
    channel: int | None = Field(default=None, ge=1, le=233)
    conditions: str = Field(default="", max_length=MAX_NOTES_CHARS)
    notes: str = Field(default="", max_length=MAX_NOTES_CHARS)


class ZoneSessionIn(_Body):
    recording_id: str = Field(pattern=ID_PATTERN)
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    split: Literal["train", "validation", "test"] | None = None


class ZoneTrainRequest(_Body):
    sessions: list[ZoneSessionIn] = Field(min_length=1, max_length=1000)


@router.get("/validation/protocol")
def validation_protocol() -> dict[str, Any]:
    return protocol_as_dict()


@router.get("/validation/runs", response_model=list[ValidationRun])
def list_validation_runs(rt: AppRuntime = Depends(get_runtime)) -> list[ValidationRun]:
    return rt.list_validation_runs()


@router.post("/validation/runs", response_model=ValidationRun)
def start_validation_run(body: ValidationRunRequest, rt: AppRuntime = Depends(get_runtime)) -> ValidationRun:
    return rt.start_validation_run(**body.model_dump())


@router.post("/validation/runs/{run_id}/stop", response_model=ValidationRun)
def stop_validation_run(run_id: str, rt: AppRuntime = Depends(get_runtime)) -> ValidationRun:
    return rt.stop_validation_run(run_id)


@router.get("/validation/report")
def validation_report(rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return rt.validation_report()


@router.get("/validation/report.md", response_class=PlainTextResponse)
def validation_report_md(rt: AppRuntime = Depends(get_runtime)) -> PlainTextResponse:
    text = render_markdown(rt.validation_report())
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="roomsense-validation-report.md"'},
    )


@router.get("/zone/status")
def zone_status(rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return rt.zone_status()


@router.post("/zone/train")
def zone_train(body: ZoneTrainRequest, rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    """CPU bound; FastAPI runs this sync endpoint in its threadpool."""
    return rt.train_zone_model([s.model_dump() for s in body.sessions])
