"""Helpers for the API, end-to-end and safety-invariant tests.

Everything here is test scaffolding. The "serial ports" are fakes that emit
hand-made RoomSense firmware lines (tests.acq_helpers builders); frames they
produce are marked LIVE only because they travel the live code path. None of
it is a measurement, and passing tests say nothing about sensing accuracy.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import pytest
import serial
from fastapi.testclient import TestClient

from roomsense.api.app import create_app
from roomsense.config import API_TOKEN_ENV, AppConfig
from roomsense.runtime import AppRuntime
from roomsense.schemas import ActivityResult
from tests.acq_helpers import FakeSerial, default_values, hello_line, rscsi_line

BASE_URL = "http://127.0.0.1"
WS_URL = "ws://127.0.0.1/api/ws"
TOKEN = "test-token-0123456789abcdef"  # made-up, only used inside tests
LINK = "tx1->rx1"

CONSENT = {
    "all_participants_consented": True,
    "participant_count": 1,
    "purpose": "software test (synthetic data)",
    "statement_version": "consent-v1",
}


@pytest.fixture(autouse=True)
def _no_api_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token in the developer's environment must not change test results.
    Imported by test modules; tests that need a token set it explicitly."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def make_cfg(tmp_path: Path, **sections: Any) -> AppConfig:
    """Config with an isolated data dir, fast reconnects and quick staleness."""
    data_dir = tmp_path / "data"
    base: dict[str, Any] = {
        "storage": {"data_dir": str(data_dir)},
        "room": {"geometry_file": str(data_dir / "room.json")},
        "acquisition": {"reconnect_initial_s": 0.02, "reconnect_max_s": 0.1, "stale_after_s": 0.5},
        "server": {"websocket_push_hz": 20.0},
    }
    return AppConfig.model_validate(_merge(base, sections))


# Short windows and a short quiet baseline so live calibrations finish in seconds.
FAST_CALIBRATION: dict[str, Any] = {
    "processing": {"window_s": 1.0, "hop_s": 0.25, "min_frames_per_window": 10},
    "detection": {"baseline_min_duration_s": 1.5, "baseline_min_windows": 5, "min_quiet_hold_s": 0.5,
                  "min_motion_hold_s": 0.5},
}


def receiver_cfg(receiver_id: str = "rx1") -> dict[str, Any]:
    return {"receiver_id": receiver_id, "port": f"/dev/fake-{receiver_id}", "input_format": "roomsense-rscsi-v1",
            "transmitter_id": "tx1"}


@contextmanager
def api_client(cfg: AppConfig, runtime: AppRuntime | None = None, **app_kw: Any) -> Iterator[TestClient]:
    """TestClient on a loopback base URL (the app refuses non-loopback Host headers)."""
    app = create_app(cfg, runtime, **app_kw)
    with TestClient(app, base_url=BASE_URL) as client:
        yield client


def wait_until(cond: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def pump_until(rt: AppRuntime, cond: Callable[[], bool], timeout: float = 60.0, batch: int = 25) -> None:
    """Drive a runtime built with background_processing=False until ``cond``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = rt.pump(batch)
        if cond():
            return
        if n == 0:
            time.sleep(0.001)
    raise AssertionError("condition not reached before the timeout")


def rel_s(res: ActivityResult, t0_unix_ns: int) -> float | None:
    """Window end of a result, in seconds after ``t0_unix_ns`` (None for results without a window)."""
    end = res.provenance.window_end_unix_ns
    return None if end is None else (end - t0_unix_ns) / 1e9


# ---------------------------------------------------------------------------
# Paced fake serial port (emits firmware lines in real time)
# ---------------------------------------------------------------------------


class PacedSerial:
    """Stand-in for ``serial.Serial`` that emits one RSHELLO line and then
    RSCSI lines at ``rate_hz`` in real time, like a board on USB would.

    ``identical=True`` repeats exactly the same CSI values (a perfectly still,
    made-up channel); otherwise small deterministic pseudo-random noise is
    added. After ``fail_after`` CSI lines, reads raise ``SerialException`` as a
    real port does when the board is unplugged.
    """

    def __init__(self, rate_hz: float = 50.0, *, fail_after: int | None = None, identical: bool = False,
                 seed: int = 0) -> None:
        self.rate_hz = rate_hz
        self.fail_after = fail_after
        self.identical = identical
        self.port: str | None = None
        self.baudrate: int | None = None
        self.timeout: float | None = None
        self.dtr: bool | None = None
        self.rts: bool | None = None
        self.is_open = False
        self.lines_emitted = 0
        self._buf = b""
        self._t0 = 0.0
        self._rng = np.random.default_rng(seed)
        self._base = default_values()
        self._lock = threading.Lock()

    def open(self) -> None:
        self.is_open = True
        self._t0 = time.monotonic()
        self._buf = (hello_line(rate_hz=int(self.rate_hz)) + "\n").encode()

    def close(self) -> None:
        self.is_open = False

    def _line(self, i: int) -> str:
        if self.identical:
            vals = self._base
        else:
            noise = self._rng.integers(-1, 2, size=len(self._base))
            vals = [int(max(-127, min(127, v + d))) for v, d in zip(self._base, noise)]
        return rscsi_line(vals, rec_seq=i + 1, tx_seq=5000 + i, rx_timestamp_us=1_000_000 + int(i * 1e6 / self.rate_hz))

    def _exhausted(self) -> bool:
        return self.fail_after is not None and self.lines_emitted >= self.fail_after

    def _fill(self) -> None:
        due = int((time.monotonic() - self._t0) * self.rate_hz)
        while self.lines_emitted < due and not self._exhausted():
            self._buf += (self._line(self.lines_emitted) + "\n").encode()
            self.lines_emitted += 1

    @property
    def in_waiting(self) -> int:
        with self._lock:
            self._fill()
            return len(self._buf)

    def read(self, n: int = 1) -> bytes:
        if not self.is_open:
            raise serial.SerialException("port not open")
        with self._lock:
            self._fill()
            if not self._buf:
                if self._exhausted():
                    raise serial.SerialException("device reports readiness to read but returned no data "
                                                 "(device disconnected or multiple access on port?)")
        if not self._buf:
            time.sleep(min(self.timeout or 0.2, 1.0 / self.rate_hz))
            with self._lock:
                self._fill()
        with self._lock:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


class PacedSerialFactory:
    """First open returns a :class:`PacedSerial`; every later open (the
    reader's reconnect attempts) fails like a missing device."""

    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.created: list[Any] = []
        self._lock = threading.Lock()

    def __call__(self) -> Any:
        with self._lock:
            if not self.created:
                port: Any = PacedSerial(**self.kw)
            else:
                port = FakeSerial(open_error=serial.SerialException("could not open port: No such file or directory"))
            self.created.append(port)
            return port


class MissingPortFactory:
    """Every open fails: the configured board is not plugged in."""

    def __init__(self) -> None:
        self.opens = 0

    def __call__(self) -> Any:
        self.opens += 1
        return FakeSerial(open_error=serial.SerialException("could not open port /dev/fake-rx1: No such file"))
