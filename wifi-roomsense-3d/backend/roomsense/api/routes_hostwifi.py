"""My Wi-Fi signal: RSSI of this computer's own Wi-Fi connection.

Kept apart from the CSI pipeline on purpose: nothing here reaches the
processing engine, activity states, the 3-D room, recordings or validation.
See roomsense/hostwifi.py for what the numbers are and are not.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ..hostwifi import HostWifiMonitor

router = APIRouter(prefix="/api/host-wifi", tags=["host-wifi"])


def _monitor(request: Request) -> HostWifiMonitor:
    mon = getattr(request.app.state, "host_wifi", None)
    if mon is None:
        mon = HostWifiMonitor()
        request.app.state.host_wifi = mon
    return mon


@router.get("")
def host_wifi(request: Request, seconds: float = Query(default=120.0, gt=0, le=600)) -> dict[str, Any]:
    return _monitor(request).snapshot(seconds)


@router.post("/start")
def host_wifi_start(request: Request) -> dict[str, Any]:
    return _monitor(request).start()


@router.post("/stop")
def host_wifi_stop(request: Request) -> dict[str, Any]:
    return _monitor(request).stop()
