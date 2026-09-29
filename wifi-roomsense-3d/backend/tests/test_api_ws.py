"""WebSocket /api/ws: status and signal pushes, disconnects, connection bound."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

import roomsense.api.ws as ws_mod
from roomsense.schemas import SystemStatus
from tests.api_helpers import (  # noqa: F401  (autouse fixture)
    LINK,
    WS_URL,
    _no_api_token_in_env,
    api_client,
    make_cfg,
    wait_until,
)


def test_websocket_pushes_status(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        with c.websocket_connect(WS_URL) as ws:
            assert ws.accepted_subprotocol is None  # nothing offered, nothing selected
            msg = ws.receive_json()
            assert msg["type"] == "status"
            st = SystemStatus.model_validate(msg["data"])
            assert st.source_banner == "NO SOURCE" and st.zone.state.value == "DISABLED"
            assert ws.receive_json()["type"] == "status"  # pushed repeatedly


def test_websocket_pushes_signal_snapshots_per_link(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        c.post("/api/source/simulation", json={"scenario": "quiet_only", "acknowledge_simulated": True})
        assert wait_until(lambda: len(c.get("/api/status").json()["activity"]) > 0, 5)
        with c.websocket_connect(WS_URL, subprotocols=["roomsense.v1"]) as ws:
            assert ws.accepted_subprotocol == "roomsense.v1"
            got_status = got_signal = False
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "status":
                    got_status = True
                    assert msg["data"]["simulated"] is True
                elif msg["type"] == "signal":
                    got_signal = True
                    assert msg["link_id"] == LINK
                    assert msg["data"]["link_id"] == LINK and msg["data"]["source_mode"] == "SIMULATION"
                if got_status and got_signal:
                    break
            assert got_status and got_signal


def test_websocket_disconnect_is_cleaned_up(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        app = c.app
        with c.websocket_connect(WS_URL) as ws:
            ws.receive_json()
            assert app.state.ws_clients == 1  # type: ignore[attr-defined]
        assert wait_until(lambda: app.state.ws_clients == 0, 3)  # type: ignore[attr-defined]
        with c.websocket_connect(WS_URL) as ws:  # the server still accepts new clients
            assert ws.receive_json()["type"] == "status"


def test_websocket_connection_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_mod, "MAX_WS_CLIENTS", 1)
    with api_client(make_cfg(tmp_path)) as c:
        with c.websocket_connect(WS_URL) as first:
            first.receive_json()
            with pytest.raises(WebSocketDisconnect) as ei:
                with c.websocket_connect(WS_URL) as second:
                    second.receive_json()
            assert ei.value.code == 1013
