"""Source selection: live, replay, simulation, stop; the configured receivers.

Only one source is active at a time and the operator always picks it. A
simulation must be explicitly acknowledged as synthetic.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..acquisition.serial_source import list_serial_ports
from ..config import ReceiverConfig
from ..runtime import AppRuntime, OperationRefused
from ..schemas import InputFormat, SystemStatus
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
    # Strict (only JSON true counts) and defaulting to False: the runtime refuses
    # with a clear message unless the client explicitly acknowledged synthetic data.
    acknowledge_simulated: StrictBool = False


class ReceiverInfo(_Body):
    """One configured receiver (``[[acquisition.receivers]]``), as the UI
    shows it. Exactly these fields; nothing else from the config (and the
    config holds no secrets)."""

    receiver_id: str
    port: str
    baud: int
    input_format: InputFormat
    transmitter_id: str
    transmitter_mac: str | None
    declared_chip: str | None
    declared_board: str | None
    ltf_config: Literal["lltf_only", "lltf_htltf_stbc", "c5_default"] | None
    link_id: str

    @classmethod
    def of(cls, r: ReceiverConfig) -> "ReceiverInfo":
        return cls(receiver_id=r.receiver_id, port=r.port, baud=r.baud, input_format=r.input_format,
                   transmitter_id=r.transmitter_id, transmitter_mac=r.transmitter_mac,
                   declared_chip=r.declared_chip, declared_board=r.declared_board, ltf_config=r.ltf_config,
                   link_id=r.link_id)


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
        raise OperationRefused(
            "PORT_NOT_AVAILABLE",
            f"{', '.join(unknown)} is neither configured nor currently listed as a serial port "
            "(see GET /api/serial/ports)",
            422,
        )


@router.get("/receivers", response_model=list[ReceiverInfo])
def configured_receivers(rt: AppRuntime = Depends(get_runtime)) -> list[ReceiverInfo]:
    """The receivers configured in ``[[acquisition.receivers]]`` (what ``POST /live``
    starts without a body). Ports are listed as configured, never opened."""
    return [ReceiverInfo.of(r) for r in rt.cfg.acquisition.receivers]


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
