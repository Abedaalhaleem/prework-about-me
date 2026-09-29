"""Replay of a ``roomsense-recording-v1`` file (``REPLAY`` mode).

The replay keeps the recording's original timing: the time between two
records is the original time divided by ``speed``, so gaps and disconnects
stay visible as gaps. Every emitted frame is a copy with:

* ``source_mode = REPLAY`` and ``session_id`` = the replay session,
* host arrival times re-stamped to *replay* time (so staleness and windowing
  work exactly as for live data),
* ``recorded_host_arrival_unix_ns`` = the original wall-clock arrival,
* the ``REPLAYED`` quality flag added, while every original flag (for example
  ``SYNTHETIC`` on a recorded simulation) is kept.

A replay of a simulated recording therefore still says it is simulated:
:meth:`ReplaySource.describe` exposes the original source mode and
:func:`original_is_synthetic` checks both the header and the frames.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ..recording_format import RecordingFormatError, read_header, read_recording
from ..schemas import CsiFrame, InputFormat, QualityFlag, SourceMode
from .base import LINK_EVENT_KINDS, EndOfStream, FrameEvent, FrameSource, LinkEvent, Sink

__all__ = ["ReplaySource", "original_is_synthetic", "MAX_REPLAY_SPEED"]

log = logging.getLogger(__name__)

MAX_REPLAY_SPEED = 1000.0
END_OF_RECORDING = "END_OF_RECORDING"


def _frame_is_synthetic(frame: CsiFrame) -> bool:
    return (
        QualityFlag.SYNTHETIC.value in frame.quality_flags
        or frame.input_format == InputFormat.SYNTHETIC_V1
        or frame.source_mode == SourceMode.SIMULATION
    )


def original_is_synthetic(path: Path) -> bool:
    """True if the recording holds simulated data.

    Checks the header's ``source_mode`` and, because a recording of a replay
    has ``source_mode = REPLAY`` in its header, also the first frame's flags
    (a single source is active per recording, so the first frame is
    representative).
    """
    try:
        with closing(read_recording(Path(path))) as records:
            header: dict[str, Any] | None = None
            for rec in records:
                if header is None:
                    header = rec
                    if str(header.get("source_mode", "")) == SourceMode.SIMULATION.value:
                        return True
                    continue
                if rec.get("type") == "frame":
                    return _frame_is_synthetic(rec["frame"])
    except (KeyError, TypeError) as exc:  # malformed frame record
        raise RecordingFormatError(f"malformed record: {type(exc).__name__}: {exc}") from exc
    return False


class _Timeline:
    """Reconstructs the original position of each record on one timeline.

    Each record is placed using the best time base it shares with an earlier
    record: host monotonic, then host wall clock, then the device timestamp
    of the *same receiver* (device clocks of different boards are unrelated).
    Every base is anchored at the timeline position where it was last seen,
    so records that alternate between bases are not double counted. The
    timeline never moves backwards (clock steps are clamped).
    """

    def __init__(self) -> None:
        self.t_ns = 0
        self._anchors: dict[Any, tuple[int, int]] = {}  # base key -> (value_ns, t_ns)

    def advance(
        self, mono: int | None, unix: int | None, dev_us: int | None = None, receiver_id: str | None = None
    ) -> int:
        bases: list[tuple[Any, int | None]] = [
            ("mono", mono),
            ("unix", unix),
            (("dev", receiver_id), None if dev_us is None else dev_us * 1000),
        ]
        for key, value in bases:
            if value is not None and key in self._anchors:
                last_value, last_t = self._anchors[key]
                self.t_ns = max(self.t_ns, last_t + (value - last_value))
                break
        for key, value in bases:
            if value is not None:
                self._anchors[key] = (value, self.t_ns)
        return self.t_ns


class ReplaySource(FrameSource):
    """Re-emit a recording with its original timing divided by ``speed``."""

    mode = SourceMode.REPLAY

    def __init__(self, recording_path: Path, *, session_id: str, speed: float = 1.0) -> None:
        if not (0 < speed <= MAX_REPLAY_SPEED):
            raise ValueError(f"speed must be in (0, {MAX_REPLAY_SPEED}]")
        self.path = Path(recording_path)
        self.session_id = session_id
        self.speed = float(speed)
        # Validates format and schema version up front, so a bad file is
        # reported when the user selects it, not halfway through.
        self.header = read_header(self.path)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.frames_emitted = 0
        self.events_emitted = 0
        self.records_skipped = 0
        self.finished = False
        self.error: str | None = None
        # Known before the first frame is emitted so the UI can show the
        # SIMULATED DATA banner from the start of a replay.
        self._contains_synthetic = original_is_synthetic(self.path)

    def start(self, sink: Sink) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("ReplaySource can only be started once")
            self._thread = threading.Thread(target=self._run, args=(sink,), name="replay", daemon=True)
            self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(max(0.0, timeout_s))
            if t.is_alive():
                log.warning("replay thread did not stop within %.1f s", timeout_s)

    def link_ids(self) -> list[str]:
        ids = self.header.get("link_ids") or []
        return [str(x) for x in ids if isinstance(x, str)]

    def describe(self) -> dict[str, Any]:
        original_mode = self.header.get("source_mode")
        return {
            "mode": self.mode.value,
            "session_id": self.session_id,
            "recording_file": self.path.name,
            "recording_id": self.header.get("recording_id"),
            "original_session_id": self.header.get("session_id"),
            "original_source_mode": original_mode,
            # True if the header or the frames say simulated; the UI must then
            # show SIMULATED DATA even though the mode is REPLAY.
            "simulated": bool(self._contains_synthetic),
            "label": self.header.get("label"),
            "scenario": self.header.get("scenario"),
            "created_at_unix_ns": self.header.get("created_at_unix_ns"),
            "config_version": self.header.get("config_version"),
            "link_ids": self.link_ids(),
            "speed": self.speed,
            "frames_emitted": self.frames_emitted,
            "events_emitted": self.events_emitted,
            "finished": self.finished,
            "error": self.error,
        }

    # -- internals --------------------------------------------------------

    def _wait_until(self, target_mono_ns: int) -> bool:
        """Sleep until ``target``; returns True if stop was requested."""
        remaining = (target_mono_ns - time.monotonic_ns()) / 1e9
        if remaining > 0:
            return self._stop.wait(remaining)
        return self._stop.is_set()

    def _restamp(self, frame: CsiFrame, mono: int, unix: int) -> CsiFrame:
        flags = list(frame.quality_flags)
        if frame.host_arrival_monotonic_ns is None and frame.host_arrival_unix_ns is None:
            flags.append(QualityFlag.HOST_TIMESTAMP_UNAVAILABLE.value)
        flags.append(QualityFlag.REPLAYED.value)
        original_unix = (
            frame.recorded_host_arrival_unix_ns
            if frame.recorded_host_arrival_unix_ns is not None
            else frame.host_arrival_unix_ns
        )
        return dataclasses.replace(
            frame,
            source_mode=SourceMode.REPLAY,
            session_id=self.session_id,
            host_arrival_monotonic_ns=mono,
            host_arrival_unix_ns=unix,
            recorded_host_arrival_unix_ns=original_unix,
            quality_flags=tuple(dict.fromkeys(flags)),
        )

    def _run(self, sink: Sink) -> None:
        start_mono = time.monotonic_ns()
        start_unix = time.time_ns()
        timeline = _Timeline()
        first = True
        records = read_recording(self.path)
        try:
            for rec in records:
                if self._stop.is_set():
                    return
                if first:
                    first = False  # header, validated in __init__
                    continue
                rtype = rec.get("type")
                if rtype == "frame":
                    frame: CsiFrame = rec["frame"]
                    t = timeline.advance(
                        frame.host_arrival_monotonic_ns,
                        frame.host_arrival_unix_ns,
                        frame.device_timestamp_unwrapped_us,
                        frame.receiver_id,
                    )
                    offset = int(t / self.speed)
                    if self._wait_until(start_mono + offset):
                        return
                    out = self._restamp(frame, start_mono + offset, start_unix + offset)
                    if not self._contains_synthetic and _frame_is_synthetic(frame):
                        self._contains_synthetic = True
                    self.frames_emitted += 1
                    self._deliver(sink, FrameEvent(out))
                elif rtype == "event":
                    ev = rec.get("event")
                    if not isinstance(ev, dict) or ev.get("kind") not in LINK_EVENT_KINDS:
                        self.records_skipped += 1
                        continue
                    t = timeline.advance(_opt_int(ev.get("host_monotonic_ns")), _opt_int(ev.get("host_unix_ns")))
                    offset = int(t / self.speed)
                    if self._wait_until(start_mono + offset):
                        return
                    self.events_emitted += 1
                    self._deliver(
                        sink,
                        LinkEvent(
                            link_id=str(ev.get("link_id") or ""),
                            receiver_id=str(ev.get("receiver_id") or ""),
                            kind=str(ev["kind"]),
                            detail=str(ev.get("detail") or "")[:500],
                            host_monotonic_ns=start_mono + offset,
                            data={"replayed": True, "recorded_host_unix_ns": _opt_int(ev.get("host_unix_ns"))},
                        ),
                    )
                elif rtype in ("header", "footer"):
                    continue
                else:
                    self.records_skipped += 1
        except (RecordingFormatError, ValueError, KeyError, TypeError, OSError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("replay of %s failed: %s", self.path.name, self.error)
            self._deliver(sink, EndOfStream(f"ERROR: {self.error}"))
            return
        finally:
            records.close()
        if not self._stop.is_set():
            self.finished = True
            self._deliver(sink, EndOfStream(END_OF_RECORDING))

    @staticmethod
    def _deliver(sink: Sink, ev: Any) -> None:
        try:
            sink(ev)
        except Exception:  # a failing consumer must not end the replay
            log.exception("replay sink failed")


def _opt_int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None
