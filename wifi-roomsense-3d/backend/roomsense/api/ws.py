"""``/api/ws``: pushes SystemStatus and per-link SignalSnapshots.

Messages (JSON text frames):

* ``{"type": "status", "data": SystemStatus}`` at ``server.websocket_push_hz``
* ``{"type": "signal", "link_id": ..., "data": SignalSnapshot}`` for each link
  that has data in the current session, after every status message.

Authentication happens in :class:`~roomsense.api.security.SecurityMiddleware`
before the handshake is accepted. When the client offers the
``roomsense.v1`` subprotocol it is selected; the ``bearer.<token>`` entry is
never echoed back. Status building touches locks and the database, so it runs
in a worker thread and never blocks the event loop. The number of concurrent
connections is bounded.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..runtime import AppRuntime, OperationRefused
from .security import WS_SUBPROTOCOL, ws_offered_subprotocols

__all__ = ["router", "MAX_WS_CLIENTS", "SIGNAL_SECONDS", "websocket_support_available"]

log = logging.getLogger(__name__)

router = APIRouter()

MAX_WS_CLIENTS = 16
SIGNAL_SECONDS = 60.0
_MAX_INBOUND_BYTES = 4096


def websocket_support_available() -> bool:
    """True if uvicorn has a WebSocket implementation (``websockets`` or ``wsproto``)."""
    import importlib.util

    return any(importlib.util.find_spec(m) is not None for m in ("websockets", "wsproto"))


async def _drain_client(ws: WebSocket) -> None:
    """Consume (and ignore) client messages; returns when the client goes away."""
    while True:
        msg = await ws.receive()
        if msg.get("type") == "websocket.disconnect":
            return
        text = msg.get("text") or ""
        data = msg.get("bytes") or b""
        if len(text) > _MAX_INBOUND_BYTES or len(data) > _MAX_INBOUND_BYTES:
            return  # the protocol is server-push only; oversized input ends the session


def _snapshots(rt: AppRuntime, link_ids: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for lid in link_ids:
        try:
            snap = rt.signal_snapshot(lid, SIGNAL_SECONDS)
        except OperationRefused:
            continue  # no data for this link yet
        try:
            payload = json.dumps({"type": "signal", "link_id": lid, "data": snap}, allow_nan=False,
                                 separators=(",", ":"))
        except ValueError:
            log.warning("signal snapshot for %s contained non-finite values; not sent", lid)
            continue
        out.append((lid, payload))
    return out


def _status_payload(rt: AppRuntime) -> tuple[str, list[str]]:
    status = rt.build_status()
    text = '{"type":"status","data":' + status.model_dump_json() + "}"
    return text, [ls.link_id for ls in status.links]


@router.websocket("/api/ws")
async def status_socket(websocket: WebSocket) -> None:
    app: Any = websocket.app
    rt: AppRuntime | None = getattr(app.state, "runtime", None)
    if rt is None or rt.closed:
        await websocket.close(code=1013)
        return
    if app.state.ws_clients >= MAX_WS_CLIENTS:
        await websocket.close(code=1013)
        return
    offered = ws_offered_subprotocols(websocket.scope)
    subprotocol = WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None
    app.state.ws_clients += 1
    period = 1.0 / float(rt.cfg.server.websocket_push_hz)
    drain: asyncio.Task[None] | None = None
    try:
        await websocket.accept(subprotocol=subprotocol)
        drain = asyncio.create_task(_drain_client(websocket))
        while not drain.done() and not rt.closed:
            try:
                status_text, link_ids = await anyio.to_thread.run_sync(_status_payload, rt)
                signals = await anyio.to_thread.run_sync(_snapshots, rt, link_ids)
            except OperationRefused:
                break  # runtime shutting down
            await websocket.send_text(status_text)
            for _, payload in signals:
                await websocket.send_text(payload)
            await asyncio.wait({drain}, timeout=period)
    except (WebSocketDisconnect, ConnectionError):
        pass
    except RuntimeError as exc:
        # Starlette raises RuntimeError when sending on a socket the client closed.
        log.debug("websocket ended: %s", exc)
    except Exception:
        log.exception("websocket push failed")
    finally:
        app.state.ws_clients -= 1
        if drain is not None:
            if not drain.done():
                drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
                pass  # retrieved so asyncio never reports it as unhandled
        try:
            await websocket.close(code=1001)
        except Exception:
            pass
