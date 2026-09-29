"""FastAPI dependencies shared by the route modules."""

from __future__ import annotations

from fastapi import Request

from ..runtime import AppRuntime, OperationRefused

__all__ = ["get_runtime"]


def get_runtime(request: Request) -> AppRuntime:
    """The application's :class:`AppRuntime` (503 ``NOT_READY`` while starting or stopping)."""
    rt = getattr(request.app.state, "runtime", None)
    if rt is None or rt.closed:
        raise OperationRefused("NOT_READY", "the runtime is not running", 503)
    return rt
