"""Source abstraction shared by live, replay and synthetic adapters.

A source pushes :data:`SourceEvent` objects into a sink callable supplied by
the :class:`~roomsense.acquisition.manager.AcquisitionManager`. Sources never
switch mode: a live source that loses hardware reports ``DISCONNECTED`` and
keeps trying to reconnect; it never emits replayed or synthetic frames.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Union

from ..schemas import CsiFrame, SourceMode

# LinkEvent.kind values
CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"
RECONNECTING = "RECONNECTING"
ERROR = "ERROR"
HELLO = "HELLO"
STAT = "STAT"
PARSE_ERROR = "PARSE_ERROR"
DIAGNOSTIC = "DIAGNOSTIC"
LINK_EVENT_KINDS = frozenset({CONNECTED, DISCONNECTED, RECONNECTING, ERROR, HELLO, STAT, PARSE_ERROR, DIAGNOSTIC})


@dataclass(frozen=True, slots=True)
class FrameEvent:
    frame: CsiFrame


@dataclass(frozen=True, slots=True)
class LinkEvent:
    link_id: str
    receiver_id: str
    kind: str
    detail: str = ""
    host_monotonic_ns: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in LINK_EVENT_KINDS:
            raise ValueError(f"unknown LinkEvent kind {self.kind!r}")


@dataclass(frozen=True, slots=True)
class EndOfStream:
    reason: str


SourceEvent = Union[FrameEvent, LinkEvent, EndOfStream]
Sink = Callable[[SourceEvent], None]


class FrameSource(abc.ABC):
    """Base class for every acquisition source."""

    mode: SourceMode
    session_id: str

    @abc.abstractmethod
    def start(self, sink: Sink) -> None:
        """Begin producing events into ``sink`` (non-blocking)."""

    @abc.abstractmethod
    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop producing events and release resources (idempotent)."""

    @abc.abstractmethod
    def link_ids(self) -> list[str]:
        """Links this source can produce."""

    @abc.abstractmethod
    def describe(self) -> dict[str, Any]:
        """Human-readable details for the UI (never includes secrets)."""
