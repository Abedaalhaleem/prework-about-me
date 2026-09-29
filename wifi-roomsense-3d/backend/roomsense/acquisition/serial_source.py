"""Live acquisition from ESP32 receivers over USB serial.

One daemon reader thread per configured receiver. The source is ``LIVE``
only: when a board disappears it reports ``DISCONNECTED`` / ``RECONNECTING``
and keeps retrying with exponential backoff. There is deliberately no code
path in this module that produces replayed or synthetic frames.

Ports are opened without toggling the reset lines where the OS allows it:
ESP32 development boards wire DTR/RTS through a transistor pair to EN and
GPIO0 (auto-reset / auto-download), so asserting them on open would reboot
the board or put it into the ROM bootloader. The port object is created
unopened, DTR/RTS are set to False, and only then is it opened. (Some
OS/driver combinations still pulse DTR on open; that is outside our control.)

Only the serial port named in the receiver config is ever opened; ports are
never guessed. :func:`list_serial_ports` enumerates ports without opening
any of them.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Any, Callable

import serial
from serial.tools import list_ports

from ..config import AcquisitionConfig, ReceiverConfig, StorageConfig
from ..schemas import SourceMode
from .base import (
    CONNECTED,
    DIAGNOSTIC,
    DISCONNECTED,
    ERROR,
    HELLO,
    PARSE_ERROR,
    RECONNECTING,
    STAT,
    FrameEvent,
    FrameSource,
    LinkEvent,
    Sink,
)
from .parser import (
    LINE_TOO_LONG,
    MAC_NOT_CONFIGURED,
    CsiLineRecord,
    DiagnosticLine,
    FrameBuilder,
    HelloRecord,
    ParseError,
    StatRecord,
    parse_line,
)

__all__ = ["LiveSerialSource", "LineAssembler", "list_serial_ports", "USB_UART_BRIDGE_VIDS"]

log = logging.getLogger(__name__)

# USB vendor IDs of the USB-UART bridges found on common ESP32 boards.
USB_UART_BRIDGE_VIDS: dict[int, str] = {
    0x303A: "Espressif USB-Serial/JTAG",
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH34x",
    0x0403: "FTDI",
}

READ_TIMEOUT_S = 0.2
MAX_READ_CHUNK = 65536  # bounds each read even if the OS buffer holds more
DIAGNOSTIC_MAX_PER_S = 5.0
DIAGNOSTIC_MAX_CHARS = 240
PARSE_ERROR_MAX_PER_S = 20.0
SUMMARY_INTERVAL_S = 5.0


def list_serial_ports() -> list[dict[str, Any]]:
    """Enumerate serial ports (never opens them).

    ``likely_usb_uart_bridge`` only means the USB vendor ID belongs to a
    bridge chip commonly used on ESP32 boards. It does not prove an ESP32
    (or RoomSense firmware) is attached.
    """
    out: list[dict[str, Any]] = []
    for p in list_ports.comports():
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        out.append(
            {
                "device": str(p.device),
                "description": str(getattr(p, "description", "") or ""),
                "hwid": str(getattr(p, "hwid", "") or ""),
                "vid": vid,
                "pid": pid,
                "likely_usb_uart_bridge": vid in USB_UART_BRIDGE_VIDS,
            }
        )
    out.sort(key=lambda d: d["device"])
    return out


class _Overflow:
    """Marker returned by :class:`LineAssembler` for a discarded long line."""

    __slots__ = ()


OVERFLOW = _Overflow()


class LineAssembler:
    """Split a byte stream into lines with a hard size cap.

    If ``max_line_bytes`` (plus room for ``\\r``) is exceeded before a newline
    arrives, the partial line is dropped, :data:`OVERFLOW` is returned once,
    and everything up to the next newline is discarded. Memory is therefore
    bounded no matter what the device sends.
    """

    def __init__(self, max_line_bytes: int) -> None:
        self.max_line_bytes = int(max_line_bytes)
        self._cap = self.max_line_bytes + 1  # allow a trailing '\r'
        self._buf = bytearray()
        self._discarding = False

    def feed(self, chunk: bytes) -> list[bytes | _Overflow]:
        out: list[bytes | _Overflow] = []
        start = 0
        n = len(chunk)
        while start < n:
            nl = chunk.find(b"\n", start)
            end = n if nl < 0 else nl
            if self._discarding:
                if nl >= 0:
                    self._discarding = False
            else:
                piece_len = end - start
                if len(self._buf) + piece_len > self._cap:
                    self._buf.clear()
                    out.append(OVERFLOW)
                    self._discarding = nl < 0
                else:
                    self._buf += chunk[start:end]
                    if nl >= 0:
                        out.append(bytes(self._buf))
                        self._buf.clear()
            if nl < 0:
                break
            start = nl + 1
        return out


class _RateLimiter:
    """Token bucket: allow ``rate`` events/s with a burst of ``burst``."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = burst
        self._t = time.monotonic()
        self.suppressed = 0

    def allow(self) -> bool:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._t) * self.rate)
        self._t = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        self.suppressed += 1
        return False


def _default_serial_factory() -> Any:
    return serial.Serial()  # unopened: port is assigned before open()


class _ReceiverWorker:
    def __init__(
        self,
        receiver: ReceiverConfig,
        acq: AcquisitionConfig,
        storage: StorageConfig,
        session_id: str,
        factory: Callable[..., Any],
        stop_event: threading.Event,
        sink: Sink,
    ) -> None:
        self.rx = receiver
        self.acq = acq
        self.factory = factory
        self.stop_event = stop_event
        self.sink = sink
        self.link_id = receiver.link_id
        self.builder = FrameBuilder(
            receiver,
            session_id=session_id,
            source_mode=SourceMode.LIVE,
            keep_raw_lines=storage.keep_raw_lines,
            max_raw_line_chars=storage.max_raw_line_chars,
        )
        self.assembler = LineAssembler(acq.max_line_bytes)
        self.connected = False
        self.frames = 0
        self.lines = 0
        self.mac_filtered = 0
        self.last_error: str | None = None
        self._ser: Any = None
        self._ser_lock = threading.Lock()
        self._diag_limiter = _RateLimiter(DIAGNOSTIC_MAX_PER_S, DIAGNOSTIC_MAX_PER_S)
        self._perr_limiter = _RateLimiter(PARSE_ERROR_MAX_PER_S, PARSE_ERROR_MAX_PER_S)
        self._suppressed_errors: dict[str, int] = {}
        self._mac_filtered_unreported = 0
        self._last_summary = time.monotonic()
        self.thread = threading.Thread(
            target=self._run, name=f"serial-{receiver.receiver_id}", daemon=True
        )

    # -- events -----------------------------------------------------------

    def _emit(self, kind: str, detail: str = "", data: dict[str, Any] | None = None) -> None:
        ev = LinkEvent(
            link_id=self.link_id,
            receiver_id=self.rx.receiver_id,
            kind=kind,
            detail=detail,
            host_monotonic_ns=time.monotonic_ns(),
            data=data or {},
        )
        try:
            self.sink(ev)
        except Exception:  # the sink must never kill the reader thread
            log.exception("sink failed for %s event on %s", kind, self.link_id)

    def _parse_error(self, code: str, detail: str) -> None:
        if self._perr_limiter.allow():
            self._emit(PARSE_ERROR, f"{code}: {detail}"[:DIAGNOSTIC_MAX_CHARS], {"code": code, "count": 1})
        else:
            # Aggregated so counters stay exact while the event rate is bounded.
            self._suppressed_errors[code] = self._suppressed_errors.get(code, 0) + 1

    def _flush_summaries(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_summary < SUMMARY_INTERVAL_S:
            return
        self._last_summary = now
        if self._mac_filtered_unreported:
            n, self._mac_filtered_unreported = self._mac_filtered_unreported, 0
            self._emit(
                PARSE_ERROR,
                f"{MAC_NOT_CONFIGURED}: {n} line(s) from a transmitter MAC not configured for this link",
                {"code": MAC_NOT_CONFIGURED, "count": n, "summary": True},
            )
        for code, n in list(self._suppressed_errors.items()):
            self._emit(PARSE_ERROR, f"{code}: {n} more line(s) (rate-limited)", {"code": code, "count": n, "summary": True})
        self._suppressed_errors.clear()
        if self._diag_limiter.suppressed:
            n, self._diag_limiter.suppressed = self._diag_limiter.suppressed, 0
            self._emit(DIAGNOSTIC, f"{n} diagnostic line(s) not shown (rate-limited)", {"suppressed": n})

    # -- port handling -------------------------------------------------------

    def _open(self) -> Any:
        ser = self.factory()
        ser.port = self.rx.port
        ser.baudrate = self.rx.baud
        ser.timeout = READ_TIMEOUT_S
        # Keep DTR/RTS deasserted so opening the port does not reset the
        # ESP32 (dev boards use these lines for auto-reset/bootloader entry).
        ser.dtr = False
        ser.rts = False
        ser.open()
        return ser

    def close(self) -> None:
        with self._ser_lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # closing a vanished device can raise anything
                log.debug("error closing %s", self.rx.port, exc_info=True)

    def _run(self) -> None:
        backoff = self.acq.reconnect_initial_s
        announced_down = False
        while not self.stop_event.is_set():
            try:
                ser = self._open()
            except (serial.SerialException, OSError, ValueError) as exc:
                self.last_error = f"cannot open {self.rx.port}: {exc}"[:DIAGNOSTIC_MAX_CHARS]
                if not announced_down:
                    self._emit(DISCONNECTED, self.last_error)
                    announced_down = True
                if self._wait_backoff(backoff):
                    break
                backoff = min(backoff * 2, self.acq.reconnect_max_s)
                continue
            with self._ser_lock:
                self._ser = ser
            if self.stop_event.is_set():
                self.close()
                break
            self.connected = True
            announced_down = False
            backoff = self.acq.reconnect_initial_s
            self._emit(CONNECTED, f"{self.rx.port} @ {self.rx.baud} baud")
            try:
                self._read_loop(ser)
            except Exception as exc:
                # SerialException/OSError are the normal "unplugged" path.
                # Anything else is a bug or a misbehaving driver; it is
                # reported as ERROR and handled like a disconnect so the
                # thread keeps its retry loop instead of dying silently.
                if not isinstance(exc, (serial.SerialException, OSError)):
                    log.exception("unexpected error reading %s", self.rx.port)
                    self._emit(ERROR, f"unexpected {type(exc).__name__}: {exc}"[:DIAGNOSTIC_MAX_CHARS])
                self.last_error = f"{self.rx.port}: {exc}"[:DIAGNOSTIC_MAX_CHARS]
                self.connected = False
                self.close()
                self._flush_summaries(force=True)
                self._emit(DISCONNECTED, self.last_error)
                announced_down = True
                if self._wait_backoff(backoff):
                    break
                backoff = min(backoff * 2, self.acq.reconnect_max_s)
                continue
            finally:
                self.connected = False
            break  # _read_loop only returns normally when stopping
        self.close()
        self._flush_summaries(force=True)

    def _wait_backoff(self, delay_s: float) -> bool:
        """Announce the retry and wait. Returns True if stop was requested."""
        if self.stop_event.is_set():
            return True
        self._emit(RECONNECTING, f"retrying {self.rx.port} in {delay_s:.1f} s", {"delay_s": delay_s})
        return self.stop_event.wait(delay_s)

    def _read_loop(self, ser: Any) -> None:
        while not self.stop_event.is_set():
            waiting = ser.in_waiting
            chunk = ser.read(min(max(1, int(waiting or 0)), MAX_READ_CHUNK))
            if chunk:
                for item in self.assembler.feed(bytes(chunk)):
                    if item is OVERFLOW:
                        self._parse_error(LINE_TOO_LONG, f"line exceeded {self.acq.max_line_bytes} bytes; discarded")
                    else:
                        self._handle_line(item, time.monotonic_ns(), time.time_ns())  # type: ignore[arg-type]
            self._flush_summaries()

    # -- line handling -------------------------------------------------------

    def _handle_line(self, line: bytes, mono_ns: int, unix_ns: int) -> None:
        self.lines += 1
        try:
            rec = parse_line(
                line,
                self.rx.input_format,
                max_line_bytes=self.acq.max_line_bytes,
                max_csi_values=self.acq.max_csi_values,
            )
            if isinstance(rec, CsiLineRecord):
                raw = line.decode("ascii", errors="replace") if self.builder.keep_raw_lines else None
                frame = self.builder.build(rec, host_monotonic_ns=mono_ns, host_unix_ns=unix_ns, raw_line=raw)
                self.frames += 1
                try:
                    self.sink(FrameEvent(frame))
                except Exception:
                    log.exception("sink failed for frame on %s", self.link_id)
            elif isinstance(rec, HelloRecord):
                notices = self.builder.on_hello(rec)
                data = asdict(rec)
                data["notices"] = notices
                data["changed_fields"] = list(self.builder.last_identity_changes)
                detail = f"{rec.fw_name} {rec.fw_version} on {rec.chip}"
                if notices:
                    detail += " [" + ", ".join(notices) + "]"
                self._emit(HELLO, detail, data)
            elif isinstance(rec, StatRecord):
                self._emit(STAT, "", asdict(rec))
            elif isinstance(rec, DiagnosticLine):
                if self._diag_limiter.allow():
                    text = rec.text[:DIAGNOSTIC_MAX_CHARS]
                    data: dict[str, Any] = {}
                    if rec.looks_like:
                        data["looks_like"] = rec.looks_like
                        text = (
                            f"line looks like {rec.looks_like} but receiver is configured for "
                            f"{self.rx.input_format.value}: {text}"
                        )[:DIAGNOSTIC_MAX_CHARS]
                    self._emit(DIAGNOSTIC, text, data)
        except ParseError as exc:
            if exc.code == MAC_NOT_CONFIGURED:
                self.mac_filtered += 1
                self._mac_filtered_unreported += 1
            else:
                self._parse_error(exc.code, exc.detail)
        except Exception as exc:  # a bug must cost one line, not the reader thread
            log.exception("internal error handling a line on %s", self.link_id)
            if self._perr_limiter.allow():
                self._emit(ERROR, f"internal error handling a line: {type(exc).__name__}"[:DIAGNOSTIC_MAX_CHARS])


class LiveSerialSource(FrameSource):
    """``LIVE`` source: one reader thread per configured receiver."""

    mode = SourceMode.LIVE

    def __init__(
        self,
        receivers: list[ReceiverConfig],
        acq: AcquisitionConfig,
        storage: StorageConfig,
        session_id: str,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not receivers:
            raise ValueError("LiveSerialSource needs at least one configured receiver")
        ids = [r.receiver_id for r in receivers]
        if len(set(ids)) != len(ids):
            raise ValueError("receiver_id values must be unique")
        self.receivers = list(receivers)
        self.acq = acq
        self.storage = storage
        self.session_id = session_id
        self._factory = serial_factory or _default_serial_factory
        self._stop = threading.Event()
        self._workers: list[_ReceiverWorker] = []
        self._started = False
        self._lock = threading.Lock()

    def start(self, sink: Sink) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("LiveSerialSource can only be started once")
            self._started = True
            self._workers = [
                _ReceiverWorker(r, self.acq, self.storage, self.session_id, self._factory, self._stop, sink)
                for r in self.receivers
            ]
            for w in self._workers:
                w.thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        deadline = time.monotonic() + max(0.0, timeout_s)
        current = threading.current_thread()
        for w in self._workers:
            if w.thread is not current and w.thread.ident is not None:
                w.thread.join(max(0.0, deadline - time.monotonic()))
        for w in self._workers:
            if w.thread is not current and w.thread.is_alive():
                # Last resort: closing the port unblocks a stuck read.
                w.close()
                log.warning("serial reader %s did not stop within %.1f s", w.rx.receiver_id, timeout_s)

    def link_ids(self) -> list[str]:
        return [r.link_id for r in self.receivers]

    def describe(self) -> dict[str, Any]:
        workers = {w.rx.receiver_id: w for w in self._workers}
        receivers = []
        for r in self.receivers:
            w = workers.get(r.receiver_id)
            receivers.append(
                {
                    "receiver_id": r.receiver_id,
                    "link_id": r.link_id,
                    "port": r.port,
                    "baud": r.baud,
                    "input_format": r.input_format.value,
                    "transmitter_id": r.transmitter_id,
                    "transmitter_mac_filter": r.transmitter_mac,
                    "declared_chip": r.declared_chip,
                    "declared_board": r.declared_board,
                    "ltf_config": r.ltf_config,
                    "allow_undocumented_layout_assumption": r.allow_undocumented_layout_assumption,
                    "connected": bool(w and w.connected),
                    "frames": w.frames if w else 0,
                    "lines": w.lines if w else 0,
                    "mac_filtered_lines": w.mac_filtered if w else 0,
                    "last_error": w.last_error if w else None,
                }
            )
        return {
            "mode": self.mode.value,
            "session_id": self.session_id,
            "simulated": False,
            "receivers": receivers,
        }
