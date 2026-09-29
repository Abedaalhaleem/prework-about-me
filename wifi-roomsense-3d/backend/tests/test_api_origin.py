"""Cross-site protection: other web pages in the user's browser must not drive
or read the local server (cross-site WebSocket hijacking and CSRF).

The loopback Host check stops DNS rebinding, but a page on another origin can
still address http://127.0.0.1 with a correct Host header. CORS does not apply
to WebSockets and does not stop "simple" cross-site POSTs from executing, so
the server itself refuses foreign ``Origin`` headers on the WebSocket and on
state-changing methods. Clients that send no Origin (curl, CLI) are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from roomsense.api.security import origin_allowed
from roomsense.config import API_TOKEN_ENV
from tests.api_helpers import (  # noqa: F401  (autouse fixture)
    BASE_URL,
    TOKEN,
    WS_URL,
    _no_api_token_in_env,
    api_client,
    make_cfg,
)

FOREIGN = "https://evil.example"
SIM_BODY = {"scenario": "quiet_only", "acknowledge_simulated": True}


def test_origin_allowed_rules() -> None:
    dev = ["http://127.0.0.1:5173", "http://localhost:5173"]
    assert origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765")
    assert origin_allowed("http://LOCALHOST:8765/", "localhost:8765")
    assert origin_allowed("http://[::1]:8765", "[::1]:8765")
    assert origin_allowed("http://127.0.0.1:5173", "127.0.0.1:8765", dev)
    assert not origin_allowed(FOREIGN, "127.0.0.1:8765", dev)
    assert not origin_allowed("http://127.0.0.1:9999", "127.0.0.1:8765", dev)  # another local app
    assert not origin_allowed("null", "127.0.0.1:8765", dev)  # sandboxed frame / file:// page
    assert not origin_allowed("file://", "127.0.0.1:8765", dev)
    assert not origin_allowed("http://127.0.0.1:8765", None, dev)


def test_websocket_from_foreign_origin_is_refused_without_token(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        with pytest.raises(WebSocketDisconnect) as ei:
            with c.websocket_connect(WS_URL, headers={"Origin": FOREIGN}):
                pass
        assert ei.value.code == 1008
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(WS_URL, headers={"Origin": "null"}):
                pass
        # The UI itself (same origin), the Vite dev server and non-browser clients still work.
        for headers in ({"Origin": BASE_URL}, {"Origin": "http://127.0.0.1:5173"}, {}):
            with c.websocket_connect(WS_URL, headers=headers) as ws:
                assert ws.receive_json()["type"] == "status", headers


def test_cross_site_state_changes_are_refused_and_have_no_effect(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        # A "simple" cross-site POST (no preflight in browsers) must not start or stop anything.
        r = c.post("/api/source/live", content=b"", headers={"Origin": FOREIGN, "Content-Type": "text/plain"})
        assert r.status_code == 403 and r.json()["code"] == "ORIGIN_NOT_ALLOWED"
        r = c.post("/api/source/simulation", json=SIM_BODY, headers={"Origin": FOREIGN})
        assert r.status_code == 403
        assert c.get("/api/status").json()["source_state"] == "NO_SOURCE"

        assert c.post("/api/source/simulation", json=SIM_BODY, headers={"Origin": BASE_URL}).status_code == 200
        assert c.post("/api/source/stop", headers={"Origin": FOREIGN}).status_code == 403
        assert c.delete("/api/recordings/rec_x", headers={"Origin": FOREIGN}).status_code == 403
        assert c.get("/api/status").json()["source_mode"] == "SIMULATION"  # the foreign stop had no effect

        # Same origin, dev origin and clients without Origin are unaffected.
        assert c.post("/api/source/stop", headers={"Origin": "http://localhost:5173"}).status_code == 200
        assert c.post("/api/source/stop").status_code == 200
        # Reads are unchanged (CORS already keeps foreign pages from reading them).
        assert c.get("/api/health", headers={"Origin": FOREIGN}).status_code == 200


def test_foreign_origin_refused_even_with_a_valid_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with api_client(make_cfg(tmp_path)) as c:
        assert c.post("/api/source/stop", headers={**auth, "Origin": FOREIGN}).status_code == 403
        assert c.post("/api/source/stop", headers={**auth, "Origin": BASE_URL}).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(WS_URL, subprotocols=["roomsense.v1", f"bearer.{TOKEN}"],
                                     headers={"Origin": FOREIGN}):
                pass
        with c.websocket_connect(WS_URL, subprotocols=["roomsense.v1", f"bearer.{TOKEN}"],
                                 headers={"Origin": BASE_URL}) as ws:
            assert ws.accepted_subprotocol == "roomsense.v1"
            assert ws.receive_json()["type"] == "status"
