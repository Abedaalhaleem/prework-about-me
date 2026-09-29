"""Structured JSON logging with redaction.

Every log record becomes one JSON object per line on stderr (or a given
stream). Three rules keep logs safe to share:

* **Secrets are never written.** The API token and anything that looks like
  an ``Authorization`` header, a ``Bearer`` credential or a WebSocket
  ``bearer.<token>`` subprotocol is replaced by ``[REDACTED]`` before the
  record is formatted. Secrets registered with :func:`register_secret` are
  replaced wherever they appear, including inside exception tracebacks.
* **No request bodies.** The HTTP layer logs method, path, status and
  duration only (see :mod:`roomsense.api.app`). Query strings are not logged.
* **Bounded size.** A single message is truncated to ``MAX_MESSAGE_CHARS`` so a
  misbehaving device cannot flood the log with one huge line.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from typing import IO, Any, Iterable

__all__ = [
    "REDACTED",
    "MAX_MESSAGE_CHARS",
    "JsonFormatter",
    "RedactionFilter",
    "configure_logging",
    "redact",
    "register_secret",
]

REDACTED = "[REDACTED]"
MAX_MESSAGE_CHARS = 4000

# Patterns for credentials that may appear in free text. They are applied in
# addition to exact matches of registered secrets.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Authorization: Bearer xyz" / "authorization=xyz" / JSON "authorization": "xyz"
    re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?)(bearer\s+)?[^\s\",;]+"),
    # "Bearer xyz" anywhere
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    # WebSocket subprotocol form used by the UI: "bearer.xyz"
    re.compile(r"(?i)\b(bearer\.)[A-Za-z0-9._~-]+"),
    # ROOMSENSE_API_TOKEN=xyz (e.g. an echoed environment)
    re.compile(r"(ROOMSENSE_API_TOKEN\s*[=:]\s*)\S+"),
)

_secrets_lock = threading.Lock()
_secrets: set[str] = set()

# Attributes every LogRecord has; anything else was passed via ``extra=`` and
# is emitted as a structured field.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()
) | {"message", "asctime", "taskName"}


def register_secret(value: str | None) -> None:
    """Redact ``value`` from every future log line (no-op for short/empty values)."""
    if value and len(value) >= 4:
        with _secrets_lock:
            _secrets.add(value)


def redact(text: str) -> str:
    """Return ``text`` with registered secrets and credential patterns removed."""
    if not text:
        return text
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) or "") + REDACTED, text)
    return text


def _redact_value(v: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[depth-limit]"
    if isinstance(v, str):
        return redact(v)[:MAX_MESSAGE_CHARS]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, val in list(v.items())[:64]:
            key = str(k)
            if key.lower() in ("authorization", "token", "api_token", "password", "secret"):
                out[key] = REDACTED
            else:
                out[key] = _redact_value(val, depth + 1)
        return out
    if isinstance(v, (list, tuple)):
        return [_redact_value(x, depth + 1) for x in list(v)[:64]]
    return redact(str(v))[:MAX_MESSAGE_CHARS]


class RedactionFilter(logging.Filter):
    """Redacts the message and args of every record in place.

    Attached to handlers (not loggers) so records from every library that
    propagates to the root logger are covered.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # a bad format string must not drop the record
            msg = f"{record.msg!r} (unformattable args)"
        record.msg = redact(msg)[:MAX_MESSAGE_CHARS]
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record: ts, level, logger, msg, extra fields, exc."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage())[:MAX_MESSAGE_CHARS],
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _redact_value(value)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))[: MAX_MESSAGE_CHARS * 2]
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str | int = "INFO",
    *,
    stream: IO[str] | None = None,
    secrets: Iterable[str | None] = (),
) -> logging.Handler:
    """Install one JSON handler on the root logger (idempotent).

    Uvicorn's loggers are routed through the same handler; its access log is
    left to the application's own request logging, which never records
    headers, bodies or query strings.
    """
    for s in secrets:
        register_secret(s)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_roomsense", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    handler._roomsense = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    return handler
