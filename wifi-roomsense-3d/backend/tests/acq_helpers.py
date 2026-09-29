"""Shared helpers for the acquisition tests.

Everything here builds SYNTHETIC inputs (hand-made serial lines, fake serial
ports, fake sources). None of it is a measurement; the tests only check
software behaviour.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

import serial

from roomsense.acquisition.base import FrameSource, Sink, SourceEvent
from roomsense.acquisition.parser import compute_crc_hex
from roomsense.config import AcquisitionConfig, ReceiverConfig, StorageConfig
from roomsense.schemas import CsiFrame, DeviceIdentity, InputFormat, SourceMode

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLASSIC_FIXTURE = FIXTURES / "upstream_classic_csi_recv_router_readme.txt"
CLASSIC_HEADER_FIXTURE = FIXTURES / "upstream_classic_header.txt"
C5C6_FIXTURE = FIXTURES / "upstream_c5c6_get_started_readme.txt"
GOLDEN_RSCSI = Path(__file__).resolve().parents[2] / "firmware" / "esp32" / "host_tests" / "golden_rscsi_lines.txt"

TX_MAC = "1a:00:00:00:00:01"
OTHER_MAC = "1a:00:00:00:00:99"
STA_MAC = "aa:bb:cc:dd:ee:01"


def fixture_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="ascii").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Line builders
# ---------------------------------------------------------------------------


def with_crc(body: str) -> str:
    return f"{body}*{compute_crc_hex(body)}"


def int8_hex(values: Iterable[int]) -> str:
    return "".join(f"{v & 0xFF:02x}" for v in values)


def default_values(n: int = 128) -> list[int]:
    """Deterministic, non-saturated int8 pattern (made up)."""
    return [((i * 7) % 61) - 30 for i in range(n)]


RSCSI_FIELDS = (
    "tag", "version", "rec_seq", "tx_seq", "drops", "src_mac", "rssi", "rate", "sig_mode", "mcs", "cwb",
    "smoothing", "not_sounding", "aggregation", "stbc", "fec_coding", "sgi", "noise_floor", "ampdu_cnt",
    "channel", "secondary_channel", "rx_timestamp_us", "ant", "sig_len", "rx_state", "bb_format",
    "agc_gain", "fft_gain", "first_word_invalid", "len", "csi_hex",
)


def rscsi_body(values: list[int] | None = None, **overrides: Any) -> str:
    vals = default_values() if values is None else values
    f: dict[str, Any] = {
        "tag": "RSCSI", "version": 1, "rec_seq": 100, "tx_seq": 5000, "drops": 0, "src_mac": TX_MAC,
        "rssi": -42, "rate": 11, "sig_mode": 1, "mcs": 0, "cwb": 0, "smoothing": 1, "not_sounding": 1,
        "aggregation": 0, "stbc": 0, "fec_coding": 0, "sgi": 0, "noise_floor": -95, "ampdu_cnt": 0,
        "channel": 6, "secondary_channel": 0, "rx_timestamp_us": 123456789, "ant": 0, "sig_len": 60,
        "rx_state": 0, "bb_format": "NA", "agc_gain": "NA", "fft_gain": "NA", "first_word_invalid": 0,
        "len": len(vals), "csi_hex": int8_hex(vals),
    }
    f.update(overrides)
    return ",".join(str(f[name]) for name in RSCSI_FIELDS)


def rscsi_line(values: list[int] | None = None, **overrides: Any) -> str:
    return with_crc(rscsi_body(values, **overrides))


HELLO_FIELDS = (
    "tag", "version", "fw_name", "fw_version", "idf_version", "chip", "chip_revision", "sta_mac", "mode",
    "ltf_config", "channel", "secondary_channel", "tx_mac_filter", "rate_hz", "queue_depth", "baud", "build_id",
)


def hello_line(**overrides: Any) -> str:
    f: dict[str, Any] = {
        "tag": "RSHELLO", "version": 1, "fw_name": "roomsense_csi_rx", "fw_version": "0.1.0",
        "idf_version": "v5.5.5", "chip": "esp32s3", "chip_revision": "0.2", "sta_mac": STA_MAC,
        "mode": "ESPNOW_RX", "ltf_config": "lltf_only", "channel": 6, "secondary_channel": 0,
        "tx_mac_filter": TX_MAC, "rate_hz": 25, "queue_depth": 32, "baud": 921600, "build_id": "a1b2c3d",
    }
    f.update(overrides)
    return with_crc(",".join(str(f[n]) for n in HELLO_FIELDS))


def stat_line(**overrides: Any) -> str:
    names = ("tag", "version", "uptime_ms", "cb_total", "cb_filtered", "enqueued", "dropped_queue_full",
             "dropped_oversize", "printed", "tx_ok", "tx_fail", "free_heap", "min_free_heap")
    f: dict[str, Any] = {
        "tag": "RSSTAT", "version": 1, "uptime_ms": 123456, "cb_total": 3000, "cb_filtered": 10,
        "enqueued": 2990, "dropped_queue_full": 2, "dropped_oversize": 0, "printed": 2988, "tx_ok": "NA",
        "tx_fail": "NA", "free_heap": 150000, "min_free_heap": 120000,
    }
    f.update(overrides)
    return with_crc(",".join(str(f[n]) for n in names))


def classic_line(values: list[int] | None = None, **overrides: Any) -> str:
    """A 25-column upstream classic line (made-up values)."""
    vals = default_values() if values is None else values
    cols: dict[str, Any] = {
        "type": "CSI_DATA", "id": 0, "mac": TX_MAC, "rssi": -40, "rate": 11, "sig_mode": 1, "mcs": 0,
        "bandwidth": 0, "smoothing": 0, "not_sounding": 1, "aggregation": 0, "stbc": 0, "fec_coding": 0,
        "sgi": 0, "noise_floor": -93, "ampdu_cnt": 0, "channel": 6, "secondary_channel": 0,
        "local_timestamp": 1000, "ant": 0, "sig_len": 67, "rx_state": 1, "len": len(vals), "first_word": 0,
        "data": '"[' + ",".join(str(v) for v in vals) + ']"',
    }
    cols.update(overrides)
    order = ("type", "id", "mac", "rssi", "rate", "sig_mode", "mcs", "bandwidth", "smoothing", "not_sounding",
             "aggregation", "stbc", "fec_coding", "sgi", "noise_floor", "ampdu_cnt", "channel",
             "secondary_channel", "local_timestamp", "ant", "sig_len", "rx_state", "len", "first_word", "data")
    return ",".join(str(cols[c]) for c in order)


def c5c6_line(values: list[int] | None = None, **overrides: Any) -> str:
    vals = default_values(106) if values is None else values
    cols: dict[str, Any] = {
        "type": "CSI_DATA", "seq": 3, "mac": TX_MAC, "rssi": -40, "rate": 11, "noise_floor": -96,
        "fft_gain": 32, "agc_gain": 4, "channel": 6, "local_timestamp": 1000, "sig_len": 47, "rx_format": 0,
        "len": len(vals), "first_word": 0, "data": '"[' + ",".join(str(v) for v in vals) + ']"',
    }
    cols.update(overrides)
    order = ("type", "seq", "mac", "rssi", "rate", "noise_floor", "fft_gain", "agc_gain", "channel",
             "local_timestamp", "sig_len", "rx_format", "len", "first_word", "data")
    return ",".join(str(cols[c]) for c in order)


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def rs_receiver(receiver_id: str = "rx1", **kw: Any) -> ReceiverConfig:
    base: dict[str, Any] = {"receiver_id": receiver_id, "port": f"/dev/fake-{receiver_id}",
                            "input_format": InputFormat.ROOMSENSE_RSCSI_V1}
    base.update(kw)
    return ReceiverConfig(**base)


def classic_receiver(chip: str | None = "esp32s3", **kw: Any) -> ReceiverConfig:
    base: dict[str, Any] = {"receiver_id": "rx1", "port": "/dev/fake-rx1",
                            "input_format": InputFormat.UPSTREAM_CLASSIC_V1, "declared_chip": chip,
                            "ltf_config": "lltf_only"}
    base.update(kw)
    return ReceiverConfig(**base)


def fast_acq(**kw: Any) -> AcquisitionConfig:
    base: dict[str, Any] = {"reconnect_initial_s": 0.02, "reconnect_max_s": 0.08, "max_line_bytes": 2048}
    base.update(kw)
    return AcquisitionConfig(**base)


# ---------------------------------------------------------------------------
# Fake serial port
# ---------------------------------------------------------------------------


class FakeSerial:
    """Scripted stand-in for ``serial.Serial``. Items are byte chunks or
    exceptions (raised by ``read``). When the script is exhausted, reads idle
    like a real port with a timeout."""

    def __init__(self, script: Iterable[bytes | BaseException] = (), *, open_error: BaseException | None = None,
                 idle_sleep: float = 0.005) -> None:
        self.port: str | None = None
        self.baudrate: int | None = None
        self.timeout: float | None = None
        self.dtr: bool | None = None
        self.rts: bool | None = None
        self.is_open = False
        self.settings_at_open: dict[str, Any] | None = None
        self.closed_count = 0
        self._script: deque[bytes | BaseException] = deque(script)
        self._pending = b""
        self._open_error = open_error
        self._idle_sleep = idle_sleep

    def open(self) -> None:
        self.settings_at_open = {"port": self.port, "baudrate": self.baudrate, "timeout": self.timeout,
                                 "dtr": self.dtr, "rts": self.rts}
        if self._open_error is not None:
            raise self._open_error
        self.is_open = True

    def close(self) -> None:
        self.is_open = False
        self.closed_count += 1

    @property
    def in_waiting(self) -> int:
        if not self._pending and self._script and isinstance(self._script[0], bytes):
            self._pending = self._script.popleft()  # type: ignore[assignment]
        return len(self._pending)

    def read(self, n: int = 1) -> bytes:
        if not self.is_open:
            raise serial.SerialException("port not open")
        if not self._pending:
            if not self._script:
                time.sleep(self._idle_sleep)
                return b""
            item = self._script.popleft()
            if isinstance(item, BaseException):
                raise item
            self._pending = item
        out, self._pending = self._pending[:n], self._pending[n:]
        return out


class FakeSerialFactory:
    """Returns one FakeSerial per open attempt. After the scripted sessions
    are used up, every further port "is missing" (open raises)."""

    def __init__(self, sessions: list[list[bytes | BaseException]]) -> None:
        self._sessions = deque(sessions)
        self.created: list[FakeSerial] = []
        self._lock = threading.Lock()

    def __call__(self) -> FakeSerial:
        with self._lock:
            if self._sessions:
                fake = FakeSerial(self._sessions.popleft())
            else:
                fake = FakeSerial(open_error=serial.SerialException("could not open port: No such file or directory"))
            self.created.append(fake)
            return fake


# ---------------------------------------------------------------------------
# Event collection and fake sources
# ---------------------------------------------------------------------------


class EventCollector:
    """Thread-safe sink that records events."""

    def __init__(self) -> None:
        self.events: list[SourceEvent] = []
        self._cond = threading.Condition()

    def __call__(self, ev: SourceEvent) -> None:
        with self._cond:
            self.events.append(ev)
            self._cond.notify_all()

    def snapshot(self) -> list[SourceEvent]:
        with self._cond:
            return list(self.events)

    def wait_for(self, predicate: Callable[[list[SourceEvent]], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not predicate(self.events):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


class FakeSource(FrameSource):
    """A source driven by the test: ``push`` forwards events to the sink."""

    def __init__(self, mode: SourceMode, links: list[str], session_id: str = "fake-session",
                 fail_on_start: bool = False) -> None:
        self.mode = mode
        self.session_id = session_id
        self._links = links
        self.sink: Sink | None = None
        self.stopped = False
        self.fail_on_start = fail_on_start

    def start(self, sink: Sink) -> None:
        if self.fail_on_start:
            raise RuntimeError("cannot open hardware")
        self.sink = sink

    def stop(self, timeout_s: float = 5.0) -> None:
        self.stopped = True

    def link_ids(self) -> list[str]:
        return list(self._links)

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "fake": True}

    def push(self, ev: SourceEvent) -> None:
        assert self.sink is not None
        self.sink(ev)


def make_frame(
    *,
    link_id: str = "tx1->rx1",
    mode: SourceMode = SourceMode.LIVE,
    host_mono_ns: int | None = 1_000_000_000,
    device_us: int | None = 0,
    layout_id: str | None = "classic.lltf_only.sec_none.total128.LLTF64",
    counter: int = 0,
    drops: int | None = None,
    flags: tuple[str, ...] = (),
    session_id: str = "fake-session",
) -> CsiFrame:
    tx, rx = link_id.split("->")
    return CsiFrame(
        source_mode=mode, session_id=session_id, receiver_id=rx, transmitter_id=tx, link_id=link_id,
        input_format=InputFormat.ROOMSENSE_RSCSI_V1,
        device=DeviceIdentity(receiver_id=rx, chip="esp32s3", identity_source="user_config"),
        frame_counter=counter, frame_counter_unwrapped=counter, transmitter_counter=None,
        device_timestamp_us=None if device_us is None else device_us & 0xFFFFFFFF,
        device_timestamp_unwrapped_us=device_us,
        host_arrival_monotonic_ns=host_mono_ns,
        host_arrival_unix_ns=None if host_mono_ns is None else 1_700_000_000_000_000_000 + host_mono_ns,
        transmitter_mac=TX_MAC, channel=6, secondary_channel=0, bandwidth_mhz=20, sig_mode=1, bb_format=None,
        mcs=0, rate=11, stbc=0, rssi_dbm=-40, noise_floor_dbm=-95, agc_gain=None, fft_gain=None, sig_len=60,
        rx_state=0, antenna=0, first_word_invalid=False, csi_len=128, raw_csi=tuple(default_values()),
        values_are_gain_compensated=False, layout_id=layout_id,
        valid_subcarriers=None if layout_id is None else tuple(k for k in range(-26, 27) if k != 0),
        quality_flags=flags, firmware_drop_count=drops,
    )


def storage_cfg(**kw: Any) -> StorageConfig:
    return StorageConfig(**kw)
