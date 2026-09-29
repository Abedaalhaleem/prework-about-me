"""Bind policy, bearer-token authentication and Host checking.

Rules
-----
* **Loopback by default.** :func:`check_bind_allowed` refuses a non-loopback
  bind unless ``server.allow_non_loopback`` is true *and* the
  ``ROOMSENSE_API_TOKEN`` environment variable holds a token (>= 16 chars).
* **Token on every /api request when configured.** If a token is configured
  (on any bind address), every ``/api`` HTTP request must carry
  ``Authorization: Bearer <token>`` and the WebSocket must present it either
  in that header or as the subprotocol ``bearer.<token>`` (browsers cannot set
  headers on a WebSocket). Tokens are compared with
  :func:`hmac.compare_digest`. They are never read from query strings (those
  end up in access logs) and never logged.
* **Host header check on loopback.** While bound to loopback, requests whose
  ``Host`` is not a loopback name are refused. This blocks DNS-rebinding
  attacks, in which a web page re-points its own domain at 127.0.0.1 to reach
  a local service from the browser.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
from typing import Any, Awaitable, Callable, Iterable

from ..config import AppConfig, api_token

__all__ = [
    "WS_SUBPROTOCOL",
    "BEARER_SUBPROTOCOL_PREFIX",
    "LOOPBACK_HOSTNAMES",
    "StartupRefused",
    "check_bind_allowed",
    "token_matches",
    "bearer_from_headers",
    "ws_offered_subprotocols",
    "ws_token",
    "SecurityMiddleware",
]

WS_SUBPROTOCOL = "roomsense.v1"
BEARER_SUBPROTOCOL_PREFIX = "bearer."
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class StartupRefused(RuntimeError):
    """The server must not start with this configuration."""


def check_bind_allowed(cfg: AppConfig, token: str | None = None) -> None:
    """Raise :class:`StartupRefused` unless the bind address is allowed.

    ``token`` defaults to :func:`roomsense.config.api_token` (the environment).
    """
    token = api_token() if token is None else token
    if cfg.server.is_loopback():
        return
    if not cfg.server.allow_non_loopback:
        raise StartupRefused(
            f"refusing to bind to non-loopback address {cfg.server.host!r}: set server.allow_non_loopback = true "
            "and ROOMSENSE_API_TOKEN (at least 16 characters) to expose RoomSense on your LAN. Never expose it "
            "to the internet."
        )
    if not token:
        raise StartupRefused(
            f"refusing to bind to non-loopback address {cfg.server.host!r} without an API token: set the "
            "ROOMSENSE_API_TOKEN environment variable (at least 16 characters)."
        )


def token_matches(given: str | None, expected: str) -> bool:
    if not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _headers(scope: Scope) -> Iterable[tuple[bytes, bytes]]:
    return scope.get("headers") or []


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in _headers(scope):
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


def bearer_from_headers(scope: Scope) -> str | None:
    value = _header(scope, b"authorization")
    if not value:
        return None
    scheme, _, cred = value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    cred = cred.strip()
    return cred or None


def ws_offered_subprotocols(scope: Scope) -> list[str]:
    """Subprotocols offered by the client (``Sec-WebSocket-Protocol``)."""
    subs = scope.get("subprotocols")
    if isinstance(subs, (list, tuple)):
        return [str(s).strip() for s in subs if str(s).strip()]
    raw = _header(scope, b"sec-websocket-protocol") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def ws_token(scope: Scope) -> str | None:
    """Token from ``bearer.<token>`` subprotocol, else from the Authorization header."""
    for p in ws_offered_subprotocols(scope):
        if p.lower().startswith(BEARER_SUBPROTOCOL_PREFIX):
            return p[len(BEARER_SUBPROTOCOL_PREFIX):] or None
    return bearer_from_headers(scope)


def _hostname(host_header: str | None) -> str | None:
    if not host_header:
        return None
    h = host_header.strip()
    if h.startswith("["):  # [::1]:8765
        end = h.find("]")
        return h[1:end].lower() if end > 0 else None
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.lower().rstrip(".")


def _is_loopback_name(name: str | None) -> bool:
    if name is None:
        return False
    if name in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class SecurityMiddleware:
    """Pure ASGI middleware (works for HTTP and WebSocket scopes).

    ``token``: when set, required on ``/api`` HTTP routes and the WebSocket.
    ``enforce_loopback_host``: refuse non-loopback ``Host`` headers.
    """

    def __init__(self, app: ASGIApp, *, token: str | None, enforce_loopback_host: bool) -> None:
        self.app = app
        self.token = token
        self.enforce_loopback_host = enforce_loopback_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if self.enforce_loopback_host and not _is_loopback_name(_hostname(_header(scope, b"host"))):
            await self._reject(scope, send, 421 if kind == "http" else 1008,
                               "HOST_NOT_ALLOWED: this server only answers requests addressed to a loopback "
                               "host name (127.0.0.1, localhost or ::1)")
            return
        path = str(scope.get("path") or "")
        if self.token is not None and (path == "/api" or path.startswith("/api/")):
            given = bearer_from_headers(scope) if kind == "http" else ws_token(scope)
            if not token_matches(given, self.token):
                await self._reject(scope, send, 401 if kind == "http" else 1008,
                                   "UNAUTHORIZED: a valid API token is required (Authorization: Bearer <token>)")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, send: Send, code: int, detail: str) -> None:
        if scope.get("type") == "websocket":
            # Closing before accept makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": code, "reason": detail.split(":", 1)[0]})
            return
        body = json.dumps({"detail": detail, "code": detail.split(":", 1)[0]}).encode("utf-8")
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if code == 401:
            headers.append((b"www-authenticate", b'Bearer realm="roomsense"'))
        await send({"type": "http.response.start", "status": code, "headers": headers})
        await send({"type": "http.response.body", "body": body})
