"""FastAPI application factory.

``create_app(cfg, runtime=None)`` builds the app:

* refuses (raises :class:`~roomsense.api.security.StartupRefused`) a
  non-loopback bind without ``allow_non_loopback`` and an API token, and any
  bind while ``ROOMSENSE_API_TOKEN`` is set but unusable;
* requires ``Authorization: Bearer <token>`` on every ``/api`` route and the
  WebSocket when a token is configured; checks the ``Host`` header on loopback;
  refuses WebSocket handshakes and state-changing requests whose ``Origin`` is
  another web page (see :mod:`roomsense.api.security`);
* allows CORS only for ``server.cors_dev_origins``;
* answers every error with ``{"detail": "CODE: text", "code": "CODE"}``;
  request-validation errors (422) use ``code = "VALIDATION_ERROR"``, a detail
  that names the offending field paths, and an ``errors`` list of
  ``{type, loc, msg}`` that never echoes the rejected input values;
* logs one structured line per request (method, path, status, duration) and
  never logs headers, bodies, query strings or tokens;
* serves ``frontend/dist`` (with SPA fallback) at ``/`` when it exists, else a
  small page explaining how to build the frontend;
* owns the :class:`~roomsense.runtime.AppRuntime` lifecycle (start on startup,
  safe shutdown on shutdown).

The interactive API docs are disabled on purpose: they load scripts from a
CDN, and this app makes no external requests. ``/api/openapi.json`` is served.
"""

from __future__ import annotations

import http
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..config import REPO_ROOT, ApiTokenInvalid, AppConfig, api_token, load_config
from ..logging_setup import register_secret
from ..runtime import AppRuntime, OperationRefused
from . import routes_calibration, routes_recordings, routes_source, routes_status, routes_validation, ws
from .security import SecurityMiddleware, StartupRefused, check_bind_allowed

__all__ = ["create_app", "FRONTEND_DIST", "CSP", "error_body", "validation_error_body"]

log = logging.getLogger("roomsense.api")

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

# Everything the UI needs is served from this origin; nothing is loaded from elsewhere.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)

_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
]

_NO_FRONTEND_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>WiFi RoomSense 3D - frontend not built</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,sans-serif;max-width:46rem;margin:3rem auto;padding:0 1rem;line-height:1.5}
code,pre{background:#eee;padding:.1rem .3rem;border-radius:3px}</style></head>
<body><h1>WiFi RoomSense 3D backend is running</h1>
<p>The web interface has not been built yet, so there is nothing to show here.</p>
<p>Build it once (needs Node.js 22.12 or newer), then reload this page:</p>
<pre>cd frontend
npm ci
npm run build</pre>
<p>or run <code>scripts/setup.sh</code> (Windows: <code>scripts\\setup.ps1</code>).</p>
<p>The HTTP API is available under <code>/api</code> (for example <code>/api/health</code> and
<code>/api/status</code>). No source is started automatically: live hardware, recorded replay and clearly
labelled simulation are selected explicitly.</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# Uniform error bodies: {"detail": "CODE: text", "code": "CODE"}
# ---------------------------------------------------------------------------

_CODE_PREFIX = re.compile(r"^([A-Z][A-Z0-9_]*): ")
_MAX_VALIDATION_ERRORS = 50
_SUMMARY_ERRORS = 3
_MAX_MSG_CHARS = 200


def _status_code_name(status: int) -> str:
    """``404`` -> ``NOT_FOUND``, ``405`` -> ``METHOD_NOT_ALLOWED`` ..."""
    try:
        phrase = http.HTTPStatus(status).phrase
    except ValueError:
        return f"HTTP_{status}"
    return re.sub(r"[^A-Z0-9]+", "_", phrase.upper()).strip("_") or f"HTTP_{status}"


def error_body(status: int, detail: Any, code: str | None = None) -> dict[str, str]:
    """The documented error body. ``detail`` is always ``"CODE: text"``: a
    detail that already starts with an upper-case code keeps it, anything
    else (e.g. Starlette's ``"Not Found"``) gets the code of the status."""
    text = detail if isinstance(detail, str) else str(detail)
    if code is None:
        m = _CODE_PREFIX.match(text)
        if m is not None:
            return {"detail": text, "code": m.group(1)}
        code = _status_code_name(status)
    if not text.startswith(f"{code}: "):
        text = f"{code}: {text}" if text else code
    return {"detail": text, "code": code}


def _loc_text(loc: Any) -> str:
    parts = [str(p) for p in (loc if isinstance(loc, (list, tuple)) else [loc])]
    return ".".join(parts) or "request"


def validation_error_body(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """422 body for request-validation errors.

    ``errors`` keeps only ``type``, ``loc`` and ``msg`` of each pydantic
    error: the rejected input values (``input``, ``ctx``) are never echoed
    back, because a client may have sent something private by mistake.
    """
    cleaned: list[dict[str, Any]] = []
    for err in errors[:_MAX_VALIDATION_ERRORS]:
        loc = err.get("loc", ())
        cleaned.append({
            "type": str(err.get("type", "value_error")),
            "loc": [p if isinstance(p, (int, str)) else str(p)
                    for p in (loc if isinstance(loc, (list, tuple)) else [loc])],
            "msg": str(err.get("msg", "invalid value"))[:_MAX_MSG_CHARS],
        })
    parts = [f"{_loc_text(e['loc'])}: {e['msg']}" for e in cleaned[:_SUMMARY_ERRORS]]
    more = len(errors) - _SUMMARY_ERRORS
    summary = "; ".join(parts) if parts else "the request is invalid"
    if more > 0:
        summary += f" (+{more} more)"
    return {"detail": f"VALIDATION_ERROR: {summary}", "code": "VALIDATION_ERROR", "errors": cleaned}


class JsonCORSMiddleware(CORSMiddleware):
    """``CORSMiddleware`` whose refused preflight answers with the uniform
    JSON error body instead of plain text (same status and CORS headers)."""

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code < 400:
            return response
        text = bytes(response.body).decode("utf-8", "replace")
        headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")}
        return JSONResponse(error_body(response.status_code, f"{text} (preflight refused)", "CORS_REJECTED"),
                            status_code=response.status_code, headers=headers)


class RequestLogMiddleware:
    """Pure ASGI: security headers on every HTTP response + one log line per request.

    Only method, path (no query string), status and duration are logged.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_holder = {"status": 0}
        is_api = str(scope.get("path", "")).startswith("/api")

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in _SECURITY_HEADERS if k not in present]
                if is_api and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            status = status_holder["status"]
            method = str(scope.get("method", ""))
            if status >= 500 or status == 0 or status in (401, 403, 421):
                level = logging.WARNING
            elif method in ("GET", "HEAD", "OPTIONS") and status < 400:
                level = logging.DEBUG  # the UI polls; routine reads would flood the log
            else:
                level = logging.INFO
            log.log(level, "http request", extra={
                "method": method,
                "path": str(scope.get("path", ""))[:200],
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            })


def _install_frontend(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    root = dist.resolve()

    def _html(path: Path) -> FileResponse:
        return FileResponse(path, media_type="text/html",
                            headers={"Content-Security-Policy": CSP, "Cache-Control": "no-cache"})

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise OperationRefused("NOT_FOUND", "unknown API route", 404)
        if full_path:
            candidate = (root / full_path).resolve()
            # Serve only regular files that really live inside dist/ (no traversal, no symlink escape).
            if candidate.is_file() and root in candidate.parents:
                if candidate.suffix == ".html":
                    return _html(candidate)
                return FileResponse(candidate)
        return _html(index)


def _install_placeholder(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def placeholder() -> HTMLResponse:
        return HTMLResponse(_NO_FRONTEND_PAGE, headers={"Content-Security-Policy": CSP})


def create_app(
    cfg: AppConfig | None = None,
    runtime: AppRuntime | None = None,
    *,
    frontend_dist: Path | None = None,
) -> FastAPI:
    """Build the app. Raises ``StartupRefused`` for a disallowed bind or for a
    ``ROOMSENSE_API_TOKEN`` that is set but unusable (on any bind address)."""
    cfg = cfg if cfg is not None else (runtime.cfg if runtime is not None else load_config())
    try:
        token = api_token()
    except ApiTokenInvalid as exc:
        raise StartupRefused(str(exc)) from None
    check_bind_allowed(cfg, token)
    if token:
        register_secret(token)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = app.state.runtime
        if rt is None:
            rt = await anyio.to_thread.run_sync(AppRuntime, cfg)
            app.state.runtime = rt
        rt.start()
        if not ws.websocket_support_available():
            log.warning("no WebSocket implementation (websockets/wsproto) is installed: /api/ws cannot be served "
                        "by uvicorn and the UI's live view will not connect")
        log.info("roomsense started", extra={"bind_host": cfg.server.host, "port": cfg.server.port,
                                             "token_required": bool(token), "version": __version__})
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(rt.shutdown)

    app = FastAPI(
        title="WiFi RoomSense 3D",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.runtime = runtime
    app.state.bind_host = cfg.server.host
    app.state.ws_clients = 0

    # Every error answers {"detail": "CODE: text", "code": "CODE"}; request
    # validation errors add "errors" (type/loc/msg, never the input values).
    @app.exception_handler(OperationRefused)
    async def _refused(_: Request, exc: OperationRefused) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status,
                            content={"detail": f"{exc.code}: {exc.detail}", "code": exc.code})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> Response:
        # Framework errors (unknown route 404, 405 with its Allow header, ...).
        headers = getattr(exc, "headers", None)
        if exc.status_code < 200 or exc.status_code in (204, 205, 304):
            return Response(status_code=exc.status_code, headers=headers)
        return JSONResponse(error_body(exc.status_code, exc.detail), status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(validation_error_body(list(exc.errors())), status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500,
                            content={"detail": f"INTERNAL_ERROR: {type(exc).__name__}", "code": "INTERNAL_ERROR"})

    for r in (routes_status, routes_source, routes_calibration, routes_recordings, routes_validation, ws):
        app.include_router(r.router)

    dist = frontend_dist if frontend_dist is not None else FRONTEND_DIST
    if cfg.server.serve_frontend and (dist / "index.html").is_file():
        _install_frontend(app, dist)
    else:
        _install_placeholder(app)

    # Starlette runs the last-added middleware first: logging -> CORS -> security -> app.
    app.add_middleware(SecurityMiddleware, token=token, enforce_loopback_host=cfg.server.is_loopback(),
                       allowed_origins=list(cfg.server.cors_dev_origins))
    app.add_middleware(
        JsonCORSMiddleware,
        allow_origins=list(cfg.server.cors_dev_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(RequestLogMiddleware)
    return app
