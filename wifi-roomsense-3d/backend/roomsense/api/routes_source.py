"""Source selection: live, replay, simulation, stop.

Only one source is active at a time and the operator always picks it. A
simulation must be explicitly acknowledged as synthetic.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..acquisition.serial_source import list_serial_ports
from ..config import ReceiverConfig
from ..runtime import AppRuntime
from ..schemas import SystemStatus
from ..storage.models import ID_PATTERN
from .deps import get_runtime

router = APIRouter(prefix="/api/source", tags=["source"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiveStartRequest(_Body):
    receivers: list[ReceiverConfig] | None = Field(default=None, max_length=16)


class ReplayStartRequest(_Body):
    recording_id: str = Field(pattern=ID_PATTERN)
    speed: float = Field(default=1.0, gt=0, le=1000)


class SimulationStartRequest(_Body):
    scenario: str = Field(min_length=1, max_length=64)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    # Deliberately a plain bool defaulting to False: the runtime refuses with a
    # clear message unless the client explicitly acknowledged synthetic data.
    acknowledge_simulated: bool = False


def _check_ports(rt: AppRuntime, receivers: list[ReceiverConfig]) -> None:
    """Receivers supplied over HTTP may only name ports that are configured or
    currently enumerated as serial ports, so the API cannot be used to open
    arbitrary device files. Ports are never guessed."""
    allowed = {r.port for r in rt.cfg.acquisition.receivers}
    try:
        allowed |= {p["device"] for p in list_serial_ports()}
    except Exception:  # enumeration failure: only configured ports stay allowed
        pass
    unknown = sorted({r.port for r in receivers} - allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"PORT_NOT_AVAILABLE: {', '.join(unknown)} is neither configured nor currently listed as a "
                   "serial port (see GET /api/serial/ports)",
        )


@router.post("/live", response_model=SystemStatus)
def start_live(body: LiveStartRequest | None = Body(default=None), rt: AppRuntime = Depends(get_runtime)) -> SystemStatus:
    receivers = body.receivers if body is not None else None
    if receivers:
        _check_ports(rt, receivers)
    return rt.start_live(receivers)


@router.post("/replay", response_model=SystemStatus)
def start_replay(body: ReplayStartRequest, rt: AppRuntime = Depends(get_runtime)) -> SystemStatus:
    return rt.start_replay(body.recording_id, body.speed)


@router.post("/simulation", response_model=SystemStatus)
def start_simulation(body: SimulationStartRequest, rt: AppRuntime = Depends(get_runtime)) -> SystemStatus:
    return rt.start_simulation(body.scenario, body.seed, body.acknowledge_simulated)


@router.post("/stop", response_model=SystemStatus)
def stop_source(rt: AppRuntime = Depends(get_runtime)) -> SystemStatus:
    return rt.stop_source()
