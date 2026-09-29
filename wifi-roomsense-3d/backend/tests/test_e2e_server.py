"""The app under a real uvicorn server on an ephemeral loopback port.

TestClient does not exercise uvicorn's WebSocket implementation or the
lifespan shutdown path, so this runs the real server in a thread and talks
to it with a real WebSocket client. Data is SIMULATED (acknowledged).
"""

from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from roomsense.api.app import create_app
from roomsense.config import API_TOKEN_ENV
from roomsense.runtime import AppRuntime
from tests.api_helpers import TOKEN, _no_api_token_in_env, make_cfg, wait_until  # noqa: F401  (autouse fixture)

websockets_sync = pytest.importorskip("websockets.sync.client")
uvicorn = pytest.importorskip("uvicorn")


@contextmanager
def _server(app: Any) -> Iterator[int]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    try:
        assert wait_until(lambda: server.started, 15), "uvicorn did not start"
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(15)
        assert not thread.is_alive(), "uvicorn did not shut down"


def _get(url: str, token: str | None = None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_real_server_http_websocket_and_graceful_shutdown(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    rt = AppRuntime(cfg)
    app = create_app(cfg, rt)
    with _server(app) as port:
        base = f"http://127.0.0.1:{port}"
        code, health = _get(f"{base}/api/health")
        assert code == 200 and health["websocket_support"] is True
        req = urllib.request.Request(
            f"{base}/api/source/simulation", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"scenario": "quiet_only", "acknowledge_simulated": True}).encode())
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["simulated"] is True
        assert wait_until(lambda: _get(f"{base}/api/status")[1]["activity"] != [], 5)

        with websockets_sync.connect(f"ws://127.0.0.1:{port}/api/ws", subprotocols=["roomsense.v1"],
                                     open_timeout=5) as ws:
            assert ws.subprotocol == "roomsense.v1"
            kinds: set[str] = set()
            for _ in range(20):
                msg = json.loads(ws.recv(timeout=5))
                kinds.add(msg["type"])
                if msg["type"] == "status":
                    assert msg["data"]["source_banner"] == "SIMULATION" and msg["data"]["simulated"] is True
                if kinds == {"status", "signal"}:
                    break
            assert kinds == {"status", "signal"}
    # Leaving the context stopped uvicorn, which ran the lifespan shutdown.
    assert rt.closed
    assert rt.db.closed


def test_real_server_websocket_token_via_subprotocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from websockets.exceptions import InvalidStatus

    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    app = create_app(make_cfg(tmp_path))
    with _server(app) as port:
        url = f"ws://127.0.0.1:{port}/api/ws"
        assert _get(f"http://127.0.0.1:{port}/api/health")[0] == 401
        assert _get(f"http://127.0.0.1:{port}/api/health", TOKEN)[0] == 200
        with pytest.raises(InvalidStatus) as ei:
            with websockets_sync.connect(url, open_timeout=5):
                pass
        assert ei.value.response.status_code == 403
        with pytest.raises(InvalidStatus):
            with websockets_sync.connect(url, subprotocols=["roomsense.v1", "bearer.wrong-token-0123456789"],
                                         open_timeout=5):
                pass
        with websockets_sync.connect(url, subprotocols=["roomsense.v1", f"bearer.{TOKEN}"], open_timeout=5) as ws:
            assert ws.subprotocol == "roomsense.v1"  # the token is never echoed back
            assert json.loads(ws.recv(timeout=5))["type"] == "status"
