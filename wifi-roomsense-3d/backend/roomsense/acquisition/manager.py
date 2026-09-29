"""The single point where the active acquisition source is chosen.

Rules this class enforces:

* Exactly one source is active, and it is only ever the source the caller
  passed to :meth:`AcquisitionManager.start`. There is no automatic fallback
  anywhere: a live source whose boards disappear stays selected and reports
  ``DISCONNECTED``; the manager never swaps in a replay or a simulation.
* Events from a previously active source that arrive after a switch are
  dropped, so frames of two sessions are never mixed.
* Per-link status (counts, measured rate, staleness, identity, clock drift)
  is derived only from events actually received.
* :meth:`AcquisitionManager.live_layout_links` names the links of a LIVE
  source that delivered a measured frame with a documented CSI layout within
  ``acquisition.stale_after_s`` (and have not reported a disconnect since).
  ``SystemStatus.hardware_required`` is derived from it.
* Consumers are isolated: an exception in one consumer is logged and never
  propagates into a source thread. Consumers are called synchronously on the
  source's thread, one event at a time (never concurrently, even with several
  receivers), so a slow consumer slows acquisition rather than silently
  dropping data; any resulting loss shows up in device counters.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

from ..config import AppConfig
from ..schemas import CsiFrame, InputFormat, LinkStatus, QualityFlag, SourceMode, SourceState
from .alignment import ClockModel
from .base import (
    CONNECTED,
    DISCONNECTED,
    PARSE_ERROR,
    STAT,
    EndOfStream,
    FrameEvent,
    FrameSource,
    LinkEvent,
    SourceEvent,
)

__all__ = ["AcquisitionManager", "RATE_WINDOW_S", "MAX_TRACKED_LINKS"]

log = logging.getLogger(__name__)

RATE_WINDOW_S = 5.0
MAX_TRACKED_LINKS = 64
# Enough arrival stamps for 5 s at well above any ESP32 CSI rate, including
# fast replays; older stamps fall off the deque.
_MAX_ARRIVALS = 16384
_MIN_RATE_SPAN_S = 0.5

Consumer = Callable[[SourceEvent], None]


def _is_measured_live(frame: CsiFrame) -> bool:
    """A LIVE frame from a receiver: not simulated, not replayed."""
    return (frame.source_mode == SourceMode.LIVE
            and QualityFlag.SYNTHETIC.value not in frame.quality_flags
            and QualityFlag.REPLAYED.value not in frame.quality_flags
            and frame.input_format != InputFormat.SYNTHETIC_V1
            and frame.device.identity_source != "synthetic")


class _LinkTracker:
    def __init__(self, link_id: str, receiver_id: str | None = None, transmitter_id: str | None = None) -> None:
        tx, _, rx = link_id.partition("->")
        self.link_id = link_id
        self.receiver_id = receiver_id or rx or link_id
        self.transmitter_id = transmitter_id or (tx if rx else "")
        self.frames_total = 0
        self.frames_rejected = 0
        self.parse_errors = 0
        self.firmware_drops: int | None = None
        self.layout_id: str | None = None
        self.channel: int | None = None
        self.device: dict[str, Any] | None = None
        self.first_arrival_ns: int | None = None
        self.last_arrival_ns: int | None = None
        # Newest measured LIVE frame whose CSI layout is documented.
        self.last_layout_arrival_ns: int | None = None
        self.arrivals: deque[int] = deque(maxlen=_MAX_ARRIVALS)
        # "unknown" until the source reports; "down" after DISCONNECTED until
        # a CONNECTED event or a newer frame arrives.
        self.transport = "unknown"
        self.clock = ClockModel()

    def on_frame(self, frame: CsiFrame, arrival_ns: int, model_clock: bool) -> None:
        self.frames_total += 1
        if frame.layout_id is None:
            self.frames_rejected += 1
        self.receiver_id = frame.receiver_id
        self.transmitter_id = frame.transmitter_id
        if frame.firmware_drop_count is not None:
            self.firmware_drops = frame.firmware_drop_count
        self.layout_id = frame.layout_id
        self.channel = frame.channel
        self.device = frame.device.to_record()
        if self.first_arrival_ns is None:
            self.first_arrival_ns = arrival_ns
        if self.last_arrival_ns is None or arrival_ns >= self.last_arrival_ns:
            self.last_arrival_ns = arrival_ns
        if frame.layout_id is not None and _is_measured_live(frame) and (
                self.last_layout_arrival_ns is None or arrival_ns >= self.last_layout_arrival_ns):
            self.last_layout_arrival_ns = arrival_ns
        self.arrivals.append(arrival_ns)
        self.transport = "up"
        if model_clock:
            if QualityFlag.TIMESTAMP_NON_MONOTONIC.value in frame.quality_flags:
                self.clock.reset()
            if frame.device_timestamp_unwrapped_us is not None and frame.host_arrival_monotonic_ns is not None:
                self.clock.update(frame.device_timestamp_unwrapped_us, frame.host_arrival_monotonic_ns)

    def rate_hz(self, now_ns: int) -> float | None:
        if self.first_arrival_ns is None:
            return None
        span_s = min(RATE_WINDOW_S, (now_ns - self.first_arrival_ns) / 1e9)
        if span_s < _MIN_RATE_SPAN_S:
            return None
        cutoff = now_ns - int(RATE_WINDOW_S * 1e9)
        n = sum(1 for t in self.arrivals if cutoff < t <= now_ns)
        return n / span_s

    def status(self, now_ns: int, stale_after_s: float) -> LinkStatus:
        age = None if self.last_arrival_ns is None else max(0.0, (now_ns - self.last_arrival_ns) / 1e9)
        connected = self.transport == "up" and age is not None and age <= stale_after_s
        return LinkStatus(
            link_id=self.link_id,
            receiver_id=self.receiver_id,
            transmitter_id=self.transmitter_id,
            connected=connected,
            last_frame_age_s=age,
            acquisition_rate_hz=self.rate_hz(now_ns),
            frames_total=self.frames_total,
            frames_rejected=self.frames_rejected,
            parse_errors=self.parse_errors,
            firmware_drops=self.firmware_drops,
            layout_id=self.layout_id,
            channel=self.channel,
            device=self.device,
            clock_offset_ms=self.clock.offset_ms(),
            clock_drift_ppm=self.clock.drift_ppm(),
        )


class AcquisitionManager:
    """Owns the active :class:`FrameSource` and fans its events out."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._lock = threading.RLock()
        # Serialises event handling across source threads (one reader thread
        # per receiver): consumers never run concurrently and see events in
        # the same order as the link bookkeeping. Status reads only take
        # ``_lock`` so they are never blocked by a slow consumer.
        self._deliver_lock = threading.RLock()
        self._consumers: list[Consumer] = []
        self._consumer_errors: dict[int, int] = {}
        self._source: FrameSource | None = None
        self._generation = 0
        self._state = SourceState.NO_SOURCE
        self._detail: str | None = None
        self._links: dict[str, _LinkTracker] = {}
        self._dropped_stale_events = 0

    # -- consumers --------------------------------------------------------

    def add_consumer(self, fn: Consumer) -> None:
        with self._lock:
            if fn not in self._consumers:
                self._consumers.append(fn)

    def remove_consumer(self, fn: Consumer) -> None:
        with self._lock:
            if fn in self._consumers:
                self._consumers.remove(fn)

    # -- source lifecycle -------------------------------------------------

    def start(self, source: FrameSource) -> None:
        """Stop the current source (if any), then start ``source``.

        ``source`` is used as given; nothing is ever substituted for it.
        """
        with self._lock:
            old = self._source
            self._generation += 1
            generation = self._generation
            self._source = None
            self._state = SourceState.NO_SOURCE
        if old is not None and old is not source:
            self._stop_source(old)
        with self._lock:
            if generation != self._generation:
                raise RuntimeError("source selection changed concurrently; not starting this source")
            self._source = source
            self._detail = None
            self._links = {}
            for lid in source.link_ids()[:MAX_TRACKED_LINKS]:
                self._links[lid] = _LinkTracker(lid)
            self._state = SourceState.CONNECTING if source.mode == SourceMode.LIVE else SourceState.RUNNING
        try:
            source.start(self._make_sink(generation))
        except Exception as exc:
            with self._lock:
                if generation == self._generation:
                    self._state = SourceState.ERROR
                    self._detail = f"failed to start: {type(exc).__name__}: {exc}"[:300]
            raise

    def stop(self) -> None:
        with self._lock:
            old = self._source
            self._generation += 1
            self._source = None
            self._state = SourceState.NO_SOURCE
            self._detail = None
            self._links = {}
        if old is not None:
            self._stop_source(old)

    @staticmethod
    def _stop_source(src: FrameSource) -> None:
        try:
            src.stop()
        except Exception:
            log.exception("error while stopping %s source", getattr(src.mode, "value", src.mode))

    @property
    def active(self) -> FrameSource | None:
        with self._lock:
            return self._source

    @property
    def mode(self) -> SourceMode | None:
        with self._lock:
            return None if self._source is None else self._source.mode

    @property
    def detail(self) -> str | None:
        with self._lock:
            return self._detail

    def source_state(self) -> SourceState:
        with self._lock:
            return self._state

    def link_statuses(self, now_monotonic_ns: int | None = None) -> list[LinkStatus]:
        now = time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        with self._lock:
            stale = self.cfg.acquisition.stale_after_s
            return [self._links[k].status(now, stale) for k in sorted(self._links)]

    def live_layout_links(self, now_monotonic_ns: int | None = None) -> list[str]:
        """Links of the active LIVE source whose newest measured frame with a
        documented CSI layout arrived at most ``acquisition.stale_after_s``
        ago and that have not reported a disconnect since. Empty when no LIVE
        source is active: replay and simulation never count."""
        now = time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        with self._lock:
            if self._source is None or self._source.mode != SourceMode.LIVE:
                return []
            limit_ns = int(self.cfg.acquisition.stale_after_s * 1e9)
            return sorted(
                lid for lid, tr in self._links.items()
                if tr.transport != "down" and tr.last_layout_arrival_ns is not None
                and now - tr.last_layout_arrival_ns <= limit_ns
            )

    # -- event path -------------------------------------------------------

    def _make_sink(self, generation: int) -> Callable[[SourceEvent], None]:
        def sink(ev: SourceEvent) -> None:
            self._on_event(generation, ev)

        return sink

    def _tracker(self, link_id: str, receiver_id: str | None = None) -> _LinkTracker | None:
        tr = self._links.get(link_id)
        if tr is None and link_id and len(self._links) < MAX_TRACKED_LINKS:
            tr = _LinkTracker(link_id, receiver_id=receiver_id)
            self._links[link_id] = tr
        return tr

    def _on_event(self, generation: int, ev: SourceEvent) -> None:
        with self._deliver_lock:
            self._handle_event(generation, ev)

    def _handle_event(self, generation: int, ev: SourceEvent) -> None:
        with self._lock:
            if generation != self._generation or self._source is None:
                self._dropped_stale_events += 1
                return
            source = self._source
            if isinstance(ev, FrameEvent):
                fr = ev.frame
                tr = self._tracker(fr.link_id, fr.receiver_id)
                if tr is not None:
                    arrival = fr.host_arrival_monotonic_ns
                    if arrival is None:
                        arrival = time.monotonic_ns()
                    # Clock drift is only a measurement for live boards; replay
                    # and simulation timestamps are re-stamped or invented.
                    tr.on_frame(fr, arrival, model_clock=source.mode == SourceMode.LIVE)
                if self._state in (SourceState.CONNECTING, SourceState.DISCONNECTED):
                    self._state = SourceState.RUNNING
            elif isinstance(ev, LinkEvent):
                self._on_link_event(ev, source)
            elif isinstance(ev, EndOfStream):
                if ev.reason.startswith("ERROR"):
                    self._state = SourceState.ERROR
                else:
                    self._state = SourceState.FINISHED
                self._detail = ev.reason[:300]
            consumers = list(self._consumers)
        for fn in consumers:
            try:
                fn(ev)
            except Exception:
                key = id(fn)
                n = self._consumer_errors.get(key, 0) + 1
                self._consumer_errors[key] = n
                # Log the first failure and then every 1000th so a broken
                # consumer cannot flood the log at the frame rate.
                if n == 1 or n % 1000 == 0:
                    log.exception("acquisition consumer %r failed (%d times)", fn, n)

    def _on_link_event(self, ev: LinkEvent, source: FrameSource) -> None:
        tr = self._tracker(ev.link_id, ev.receiver_id)
        if tr is not None:
            if ev.kind == CONNECTED:
                tr.transport = "up"
            elif ev.kind == DISCONNECTED:
                tr.transport = "down"
            elif ev.kind == PARSE_ERROR:
                count = ev.data.get("count", 1) if isinstance(ev.data, dict) else 1
                tr.parse_errors += count if isinstance(count, int) and count > 0 else 1
            elif ev.kind == STAT and tr.firmware_drops is None:
                q = ev.data.get("dropped_queue_full")
                o = ev.data.get("dropped_oversize")
                if isinstance(q, int) and isinstance(o, int):
                    tr.firmware_drops = q + o
        if source.mode != SourceMode.LIVE or self._state in (SourceState.FINISHED, SourceState.ERROR):
            return
        states = [t.transport for t in self._links.values()]
        if states and all(s == "down" for s in states):
            self._state = SourceState.DISCONNECTED
            self._detail = "all receivers disconnected; not falling back to any other source"
        elif any(s == "up" for s in states):
            self._state = SourceState.RUNNING
            self._detail = None
        else:
            self._state = SourceState.CONNECTING
