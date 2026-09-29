"""Calibration (quiet baseline, walk test, invalidation) and room geometry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..runtime import AppRuntime
from ..schemas import CalibrationRecord, RoomGeometry, WalkTestReport
from .deps import get_runtime

router = APIRouter(prefix="/api", tags=["calibration"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaselineStartRequest(_Body):
    link_ids: list[str] | None = Field(default=None, max_length=64)
    # A plain bool so the runtime can refuse with an explanation when it is not true.
    confirm_room_empty: bool = False


class WalkTestStartRequest(_Body):
    link_ids: list[str] | None = Field(default=None, max_length=64)


class InvalidateRequest(_Body):
    reason: str = Field(min_length=1, max_length=500)


@router.get("/calibration")
def calibration(rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    ov = rt.calibration_overview()
    return {
        **ov,
        "active": None if ov["active"] is None else ov["active"].model_dump(mode="json"),
        "history": [r.model_dump(mode="json") for r in ov["history"]],
        "last_walk_test": None if ov["last_walk_test"] is None else ov["last_walk_test"].model_dump(mode="json"),
    }


@router.post("/calibration/baseline/start")
def baseline_start(body: BaselineStartRequest, rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return rt.start_baseline(body.link_ids, confirm_room_empty=body.confirm_room_empty)


@router.post("/calibration/baseline/stop", response_model=CalibrationRecord)
def baseline_stop(rt: AppRuntime = Depends(get_runtime)) -> CalibrationRecord:
    return rt.stop_baseline()


@router.post("/calibration/baseline/cancel")
def baseline_cancel(rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return rt.cancel_baseline()


@router.post("/calibration/walk-test/start")
def walk_test_start(body: WalkTestStartRequest | None = Body(default=None),
                    rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return rt.start_walk_test(None if body is None else body.link_ids)


@router.post("/calibration/walk-test/stop", response_model=WalkTestReport)
def walk_test_stop(rt: AppRuntime = Depends(get_runtime)) -> WalkTestReport:
    return rt.stop_walk_test()


@router.post("/calibration/invalidate")
def invalidate(body: InvalidateRequest, rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return rt.invalidate_calibration(body.reason)


@router.get("/room", response_model=RoomGeometry)
def get_room(rt: AppRuntime = Depends(get_runtime)) -> RoomGeometry:
    return rt.room()


@router.put("/room")
def put_room(room: RoomGeometry, rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    out = rt.put_room(room)
    return {"room": out["room"].model_dump(mode="json"), "invalidated": out["invalidated"]}


@router.get("/room/example", response_model=RoomGeometry)
def example_room(rt: AppRuntime = Depends(get_runtime)) -> RoomGeometry:
    return rt.example_room()
