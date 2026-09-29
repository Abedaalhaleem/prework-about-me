"""FastAPI dependencies shared by the route modules."""

from __future__ import annotations

from fastapi import HTTPException, Request

from ..runtime import AppRuntime

__all__ = ["get_runtime"]


def get_runtime(request: Request) -> AppRuntime:
    """The application's :class:`AppRuntime` (503 while starting or stopping)."""
    rt = getattr(request.app.state, "runtime", None)
    if rt is None or rt.closed:
        raise HTTPException(status_code=503, detail="NOT_READY: the runtime is not running")
    return rt
