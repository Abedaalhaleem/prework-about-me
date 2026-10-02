"""Signal strength of THIS computer's own Wi-Fi connection ("My Wi-Fi signal").

What this is: the received signal strength (RSSI, dBm) and, where the OS
reports it, the noise level of the Wi-Fi link between this computer and its
access point, sampled about twice a second and shown as a live line.

What this is NOT: it is not CSI (a laptop's Wi-Fi chip does not expose
per-subcarrier channel data to applications), not motion detection, not
through-wall sensing and not a position. It is never fed into the processing
engine, the activity states, the 3-D room, recordings or validation. RSSI is
one coarse number that changes mostly when the computer itself, or people right
next to it, move, when other devices use the network, or when the router adapts.

Privacy and safety: read-only. Nothing here scans for networks, joins or changes
a network, or needs administrator rights. The network name (SSID), BSSID and MAC
addresses are never read into the samples, returned by the API or stored; samples
live in memory only (bounded) and disappear when the server stops.

Readers, tried in order for the current OS:
* macOS: CoreWLAN through PyObjC (``rssiValue``, ``noiseMeasurement``,
  ``transmitRate``, ``wlanChannel``). Fallback: the ``Signal / Noise`` line of
  ``system_profiler SPAirPortDataType`` (slow, so sampled less often).
* Linux: ``/proc/net/wireless`` (signal level in dBm).
* Windows: ``netsh wlan show interfaces`` (signal as a percentage, not dBm).
"""

from __future__ import annotations

import logging
import platform
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

NOTE = (
    "Signal strength (RSSI) of this computer's own Wi-Fi connection: one coarse number, "
    "not CSI. It is not motion sensing, not through-wall sensing and not a position, and "
    "it is never used by the motion detector or the 3-D room."
)

MAX_HISTORY_S = 600.0
DEFAULT_INTERVAL_S = 0.5
SLOW_INTERVAL_S = 3.0  # system_profiler takes about a second or more per call
COMMAND_TIMEOUT_S = 8.0

# RSSI outside this range is not a plausible reading (0 means "not associated" on macOS).
_RSSI_RANGE = (-110.0, -1.0)
_NOISE_RANGE = (-130.0, -1.0)


class ReaderUnavailable(RuntimeError):
    """The reader cannot work on this machine (missing API, no Wi-Fi interface)."""


@dataclass(frozen=True, slots=True)
class WifiSample:
    t_unix_ns: int
    rssi_dbm: float | None = None
    noise_dbm: float | None = None
    signal_percent: float | None = None  # Windows reports a percentage, not dBm
    tx_rate_mbps: float | None = None
    channel: int | None = None
    connected: bool = True


class Reader(Protocol):
    name: str
    interval_s: float

    def read(self) -> WifiSample: ...


def _in_range(value: float | None, lo_hi: tuple[float, float]) -> float | None:
    if value is None:
        return None
    lo, hi = lo_hi
    return float(value) if lo <= value <= hi else None


# ---------------------------------------------------------------------------
# Parsers (pure functions, unit-tested with captured output)
# ---------------------------------------------------------------------------

_SIG_NOISE = re.compile(r"Signal\s*/\s*Noise:\s*(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")
_TX_RATE = re.compile(r"Transmit Rate:\s*(\d+(?:\.\d+)?)")
_CHANNEL = re.compile(r"Channel:\s*(\d+)")


def parse_system_profiler(text: str, t_unix_ns: int) -> WifiSample:
    """Parse ``system_profiler SPAirPortDataType`` output. Only the block under
    "Current Network Information" is used; other listed networks are ignored."""
    idx = text.find("Current Network Information:")
    if idx < 0:
        return WifiSample(t_unix_ns=t_unix_ns, connected=False)
    block = text[idx:]
    end = block.find("Other Local Wi-Fi Networks:")
    if end > 0:
        block = block[:end]
    m = _SIG_NOISE.search(block)
    if not m:
        return WifiSample(t_unix_ns=t_unix_ns, connected=False)
    rate = _TX_RATE.search(block)
    ch = _CHANNEL.search(block)
    return WifiSample(
        t_unix_ns=t_unix_ns,
        rssi_dbm=_in_range(float(m.group(1)), _RSSI_RANGE),
        noise_dbm=_in_range(float(m.group(2)), _NOISE_RANGE),
        tx_rate_mbps=float(rate.group(1)) if rate else None,
        channel=int(ch.group(1)) if ch else None,
    )


def parse_proc_net_wireless(text: str, t_unix_ns: int) -> WifiSample:
    """Parse Linux ``/proc/net/wireless``; uses the first interface listed."""
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        _, rest = line.split(":", 1)
        parts = rest.split()
        if len(parts) < 4:
            continue
        try:
            level = float(parts[2].rstrip("."))
            noise = float(parts[3].rstrip("."))
        except ValueError:
            continue
        return WifiSample(
            t_unix_ns=t_unix_ns,
            rssi_dbm=_in_range(level, _RSSI_RANGE),
            noise_dbm=_in_range(noise, _NOISE_RANGE),
        )
    return WifiSample(t_unix_ns=t_unix_ns, connected=False)


_NETSH_SIGNAL = re.compile(r"^\s*Signal\s*:\s*(\d+)\s*%", re.MULTILINE)
_NETSH_RATE = re.compile(r"^\s*Receive rate \(Mbps\)\s*:\s*(\d+(?:\.\d+)?)", re.MULTILINE)
_NETSH_CHANNEL = re.compile(r"^\s*Channel\s*:\s*(\d+)", re.MULTILINE)


def parse_netsh(text: str, t_unix_ns: int) -> WifiSample:
    """Parse English ``netsh wlan show interfaces`` output (first interface)."""
    m = _NETSH_SIGNAL.search(text)
    if not m:
        return WifiSample(t_unix_ns=t_unix_ns, connected=False)
    rate = _NETSH_RATE.search(text)
    ch = _NETSH_CHANNEL.search(text)
    pct = float(m.group(1))
    return WifiSample(
        t_unix_ns=t_unix_ns,
        signal_percent=pct if 0.0 <= pct <= 100.0 else None,
        tx_rate_mbps=float(rate.group(1)) if rate else None,
        channel=int(ch.group(1)) if ch else None,
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


class CoreWlanReader:
    name = "macOS CoreWLAN"
    interval_s = DEFAULT_INTERVAL_S

    def __init__(self) -> None:
        try:
            from CoreWLAN import CWWiFiClient  # type: ignore[import-not-found]
        except Exception as exc:  # pyobjc not installed or not macOS
            raise ReaderUnavailable(f"CoreWLAN not available ({type(exc).__name__})") from None
        iface = CWWiFiClient.sharedWiFiClient().interface()
        if iface is None:
            raise ReaderUnavailable("no Wi-Fi interface found")
        self._iface = iface

    def read(self) -> WifiSample:
        now = time.time_ns()
        rssi = float(self._iface.rssiValue())
        if rssi == 0:  # CoreWLAN reports 0 when the interface is not associated
            return WifiSample(t_unix_ns=now, connected=False)
        noise = float(self._iface.noiseMeasurement())
        rate = float(self._iface.transmitRate())
        ch_obj = self._iface.wlanChannel()
        ch = int(ch_obj.channelNumber()) if ch_obj is not None else None
        return WifiSample(
            t_unix_ns=now,
            rssi_dbm=_in_range(rssi, _RSSI_RANGE),
            noise_dbm=_in_range(noise, _NOISE_RANGE),
            tx_rate_mbps=rate if rate > 0 else None,
            channel=ch,
        )


def _run(argv: list[str]) -> str:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
    return out.stdout


class SystemProfilerReader:
    name = "macOS system_profiler"
    interval_s = SLOW_INTERVAL_S

    def __init__(self, run: Callable[[list[str]], str] = _run) -> None:
        self._run = run

    def read(self) -> WifiSample:
        now = time.time_ns()
        return parse_system_profiler(self._run(["system_profiler", "SPAirPortDataType"]), now)


class ProcNetWirelessReader:
    name = "Linux /proc/net/wireless"
    interval_s = DEFAULT_INTERVAL_S

    def __init__(self, path: Path = Path("/proc/net/wireless")) -> None:
        if not path.exists():
            raise ReaderUnavailable(f"{path} does not exist (no Wi-Fi interface or not Linux)")
        self._path = path

    def read(self) -> WifiSample:
        return parse_proc_net_wireless(self._path.read_text(), time.time_ns())


class NetshReader:
    name = "Windows netsh"
    interval_s = 2.0

    def __init__(self, run: Callable[[list[str]], str] = _run) -> None:
        self._run = run

    def read(self) -> WifiSample:
        return parse_netsh(self._run(["netsh", "wlan", "show", "interfaces"]), time.time_ns())


class MacReader:
    """CoreWLAN first; if it keeps reporting no signal (``rssiValue() == 0``)
    while ``system_profiler`` does show one, switch to ``system_profiler`` for
    good. A missing reading is never turned into a number."""

    SWITCH_AFTER_EMPTY = 3

    def __init__(self, fast: Reader | None, slow: Reader) -> None:
        self._fast, self._slow = fast, slow
        self._active: Reader = fast if fast is not None else slow
        self._empty = 0

    @property
    def name(self) -> str:
        return self._active.name

    @property
    def interval_s(self) -> float:
        return self._active.interval_s

    def read(self) -> WifiSample:
        sample = self._active.read()
        if self._active is self._fast:
            self._empty = 0 if sample.connected else self._empty + 1
            if self._empty >= self.SWITCH_AFTER_EMPTY:
                self._empty = 0
                slow_sample = self._slow.read()
                if slow_sample.connected and slow_sample.rssi_dbm is not None:
                    log.info("CoreWLAN reports no signal but system_profiler does; switching readers")
                    self._active = self._slow
                    return slow_sample
        return sample


def detect_reader() -> tuple[Reader | None, str]:
    """Pick a reader for this OS. Returns ``(reader, reason_if_none)``."""
    if sys.platform == "darwin":
        try:
            fast: Reader | None = CoreWlanReader()
        except ReaderUnavailable as exc:
            log.info("CoreWLAN unavailable (%s); using system_profiler", exc)
            fast = None
        return MacReader(fast, SystemProfilerReader()), ""
    if sys.platform.startswith("linux"):
        try:
            return ProcNetWirelessReader(), ""
        except ReaderUnavailable as exc:
            return None, str(exc)
    if sys.platform.startswith("win"):
        return NetshReader(), ""
    return None, f"no Wi-Fi signal reader for platform {sys.platform!r}"


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class HostWifiMonitor:
    """Samples the reader on a background thread while started. Memory only,
    bounded to :data:`MAX_HISTORY_S` of samples."""

    def __init__(self, reader_factory: Callable[[], tuple[Reader | None, str]] = detect_reader,
                 max_history_s: float = MAX_HISTORY_S) -> None:
        self._factory = reader_factory
        self._max_history_s = max_history_s
        self._lock = threading.Lock()
        self._samples: deque[WifiSample] = deque(maxlen=int(max_history_s / 0.2) + 16)
        self._reader: Reader | None = None
        self._reason = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._errors = 0
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self.running:
                reader, reason = self._factory()
                self._reader, self._reason = reader, reason
                if reader is not None:
                    self._stop.clear()
                    self._thread = threading.Thread(target=self._loop, name="host-wifi", daemon=True)
                    self._thread.start()
        return self.snapshot()

    def stop(self, timeout_s: float = 5.0) -> dict[str, Any]:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout_s)
        return self.snapshot()

    def _loop(self) -> None:
        reader = self._reader
        assert reader is not None
        while not self._stop.is_set():
            interval = max(float(reader.interval_s), 0.2)  # may change (MacReader fallback)
            started = time.monotonic()
            try:
                sample = reader.read()
            except Exception as exc:  # keep sampling; a failed read is a gap, never a made-up value
                sample = WifiSample(t_unix_ns=time.time_ns(), connected=False)
                with self._lock:
                    self._errors += 1
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            with self._lock:
                self._samples.append(sample)
                cutoff = sample.t_unix_ns - int(self._max_history_s * 1e9)
                while self._samples and self._samples[0].t_unix_ns < cutoff:
                    self._samples.popleft()
            self._stop.wait(max(0.0, interval - (time.monotonic() - started)))

    def snapshot(self, seconds: float = 120.0) -> dict[str, Any]:
        with self._lock:
            now = time.time_ns()
            cutoff = now - int(min(seconds, self._max_history_s) * 1e9)
            samples = [s for s in self._samples if s.t_unix_ns >= cutoff]
            reader = self._reader
            latest = samples[-1] if samples else None
            return {
                "note": NOTE,
                "running": self.running,
                "platform": f"{platform.system()} {platform.release()}".strip(),
                "method": reader.name if reader is not None else None,
                "available": reader is not None,
                "reason": None if reader is not None else (self._reason or "not started"),
                "interval_s": float(reader.interval_s) if reader is not None else None,
                "server_time_unix_ms": now // 1_000_000,
                "errors": self._errors,
                "last_error": self._last_error,
                "latest": None if latest is None else {
                    "t": latest.t_unix_ns // 1_000_000,
                    "rssi_dbm": latest.rssi_dbm,
                    "noise_dbm": latest.noise_dbm,
                    "signal_percent": latest.signal_percent,
                    "tx_rate_mbps": latest.tx_rate_mbps,
                    "channel": latest.channel,
                    "connected": latest.connected,
                },
                "series": {
                    "t": [s.t_unix_ns // 1_000_000 for s in samples],
                    "rssi_dbm": [s.rssi_dbm for s in samples],
                    "noise_dbm": [s.noise_dbm for s in samples],
                    "signal_percent": [s.signal_percent for s in samples],
                },
            }
