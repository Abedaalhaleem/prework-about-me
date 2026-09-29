"""Bind policy, bearer tokens, Host checks, CORS and log redaction."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from roomsense.api.app import create_app
from roomsense.api.security import StartupRefused, check_bind_allowed
from roomsense.config import API_TOKEN_ENV
from roomsense.logging_setup import REDACTED, JsonFormatter, RedactionFilter, redact, register_secret
from tests.api_helpers import (  # noqa: F401  (autouse fixture)
    BASE_URL,
    TOKEN,
    WS_URL,
    _no_api_token_in_env,
    api_client,
    make_cfg,
)

LAN = {"server": {"host": "0.0.0.0", "allow_non_loopback": True}}


def test_non_loopback_bind_refused_without_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    cfg = make_cfg(tmp_path, server={"host": "0.0.0.0"})
    with pytest.raises(StartupRefused, match="allow_non_loopback"):
        create_app(cfg)


def test_non_loopback_bind_refused_without_token(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, **LAN)
    with pytest.raises(StartupRefused, match="ROOMSENSE_API_TOKEN"):
        create_app(cfg)
    with pytest.raises(StartupRefused):
        check_bind_allowed(cfg, token=None)


def test_short_token_does_not_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, "short")
    with pytest.raises(StartupRefused):
        create_app(make_cfg(tmp_path, **LAN))


def test_token_required_on_every_api_route_when_exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    app = create_app(make_cfg(tmp_path, **LAN))
    with TestClient(app) as c:  # non-loopback bind: any Host, but a token is mandatory
        for path in ("/api/health", "/api/status", "/api/recordings", "/api/room"):
            assert c.get(path).status_code == 401
            assert c.get(path, headers={"Authorization": "Bearer wrong-token-0123456789"}).status_code == 401
            assert c.get(path, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
            assert c.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        r = c.get("/api/health")
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert r.json()["code"] == "UNAUTHORIZED"
        # Tokens are never accepted from the query string (URLs end up in logs).
        assert c.get(f"/api/health?token={TOKEN}").status_code == 401
        assert c.post("/api/source/stop").status_code == 401
        # The static UI itself loads without a token; it then asks for one.
        assert c.get("/").status_code == 200


def test_token_also_enforced_on_loopback_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    with api_client(make_cfg(tmp_path)) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_websocket_requires_token_via_subprotocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    with api_client(make_cfg(tmp_path)) as c:
        with pytest.raises(WebSocketDisconnect) as ei:
            with c.websocket_connect(WS_URL):
                pass
        assert ei.value.code == 1008
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(WS_URL, subprotocols=["roomsense.v1", "bearer.wrong-token-0123456789"]):
                pass
        with c.websocket_connect(WS_URL, subprotocols=["roomsense.v1", f"bearer.{TOKEN}"]) as ws:
            assert ws.accepted_subprotocol == "roomsense.v1"  # the bearer entry is never echoed back
            assert ws.receive_json()["type"] == "status"
        with c.websocket_connect(WS_URL, headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            assert ws.receive_json()["type"] == "status"


def test_loopback_bind_rejects_foreign_host_headers(tmp_path: Path) -> None:
    """DNS-rebinding protection: a page on evil.example resolving to 127.0.0.1 gets nothing."""
    with api_client(make_cfg(tmp_path)) as c:
        assert c.get("/api/health", headers={"Host": "evil.example"}).status_code == 421
        assert c.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 421
        assert c.get("/", headers={"Host": "evil.example"}).status_code == 421
        for ok in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765", "localhost"):
            assert c.get("/api/health", headers={"Host": ok}).status_code == 200, ok
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("ws://evil.example/api/ws"):
                pass


def test_cors_only_for_dev_origins(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
        ok = c.options("/api/source/stop", headers={"Origin": "http://127.0.0.1:5173", **pre})
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
        bad = c.options("/api/source/stop", headers={"Origin": "https://evil.example", **pre})
        assert bad.status_code == 400
        assert "access-control-allow-origin" not in bad.headers
        r = c.get("/api/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers


def test_tokens_never_reach_the_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        with api_client(make_cfg(tmp_path)) as c:
            c.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
            c.get("/api/status", headers={"Authorization": "Bearer wrong-token-0123456789"})
            with c.websocket_connect(WS_URL, subprotocols=["roomsense.v1", f"bearer.{TOKEN}"]) as ws:
                ws.receive_json()
            logging.getLogger("roomsense.test").warning("client sent Authorization: Bearer %s", TOKEN)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    text = stream.getvalue()
    assert TOKEN not in text
    assert "wrong-token-0123456789" not in text
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    requests = [ln for ln in lines if ln.get("msg") == "http request"]
    assert requests and all(set(ln) >= {"method", "path", "status", "duration_ms"} for ln in requests)
    assert all("authorization" not in json.dumps(ln).lower() or REDACTED in json.dumps(ln) for ln in lines)


def test_redaction_patterns() -> None:
    register_secret("s3cr3t-value-for-redaction-test")
    assert "s3cr3t" not in redact("token s3cr3t-value-for-redaction-test in text")
    assert redact("Authorization: Bearer abc.def-ghi") == f"Authorization: {REDACTED}"
    assert "xyz123" not in redact('{"authorization": "Bearer xyz123"}')
    assert "tok456" not in redact("Sec-WebSocket-Protocol: roomsense.v1, bearer.tok456")
    assert "zzz999" not in redact("ROOMSENSE_API_TOKEN=zzz999")


def test_base_url_constant_is_loopback() -> None:
    assert BASE_URL.startswith("http://127.0.0.1")
