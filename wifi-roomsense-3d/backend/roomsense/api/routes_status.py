"""Health, status, signals, capabilities, serial ports, scenarios, pose and hardware."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..acquisition.serial_source import list_serial_ports
from ..acquisition.synthetic import builtin_scenarios
from ..capabilities import UNSUPPORTED_CAPABILITIES
from ..runtime import AppRuntime, OperationRefused
from ..schemas import CapabilityStatus, PoseStatus, SystemStatus, UnsupportedCapability
from .deps import get_runtime
from .ws import websocket_support_available

router = APIRouter(prefix="/api", tags=["status"])

SIMULATION_PREFIX = "SIMULATION (synthetic data generated in software, not a measurement): "


@router.get("/health")
def health(request: Request, rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    out = rt.health(bind_host=getattr(request.app.state, "bind_host", None))
    # Without a WebSocket library uvicorn cannot serve /api/ws; say so instead of failing silently.
    out["websocket_support"] = websocket_support_available()
    return out


@router.get("/status", response_model=SystemStatus)
def status(rt: AppRuntime = Depends(get_runtime)) -> SystemStatus:
    return rt.build_status()


@router.get("/signal")
def signal(
    link_id: str = Query(min_length=1, max_length=200),
    seconds: float = Query(default=60.0, gt=0, le=600),
    rt: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return rt.signal_snapshot(link_id, seconds)


@router.get("/capabilities", response_model=list[CapabilityStatus])
def capabilities(rt: AppRuntime = Depends(get_runtime)) -> list[CapabilityStatus]:
    return rt.build_status().capabilities


@router.get("/capabilities/unsupported", response_model=list[UnsupportedCapability])
def unsupported_capabilities() -> list[UnsupportedCapability]:
    return [UnsupportedCapability(**c) for c in UNSUPPORTED_CAPABILITIES]


@router.get("/serial/ports")
def serial_ports() -> list[dict[str, Any]]:
    """Enumerate serial ports. Ports are listed, never opened."""
    try:
        return list_serial_ports()
    except Exception as exc:  # pyserial backends can fail on unusual systems
        raise OperationRefused("SERIAL_ENUMERATION_FAILED", type(exc).__name__, 503) from exc


@router.get("/simulation/scenarios")
def simulation_scenarios() -> list[dict[str, Any]]:
    out = []
    for name, scn in sorted(builtin_scenarios().items()):
        out.append({
            "name": name,
            "duration_s": scn.duration_s,
            "description": SIMULATION_PREFIX + scn.description,
            "rate_hz": scn.rate_hz,
            "links": scn.link_ids(),
            "simulated": True,
        })
    return out


@router.get("/pose/status", response_model=PoseStatus)
def pose_status(rt: AppRuntime = Depends(get_runtime)) -> PoseStatus:
    return rt.pose_status()


@router.get("/hardware")
def hardware(refresh: bool = Query(default=False), rt: AppRuntime = Depends(get_runtime)) -> dict[str, Any]:
    """Non-destructive host inspection (sync endpoint: runs in the threadpool)."""
    return rt.hardware_report(refresh=refresh)
