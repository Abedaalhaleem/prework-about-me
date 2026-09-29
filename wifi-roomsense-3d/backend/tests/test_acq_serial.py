"""LiveSerialSource tests with fake serial ports (no hardware is touched).

These check threading, framing, reconnect and event behaviour of the live
source. They say nothing about sensing accuracy.
"""

from __future__ import annotations

import ast
import inspect
import time
from types import SimpleNamespace

import pytest
import serial

import roomsense.acquisition.serial_source as serial_source
from roomsense.acquisition.base import EndOfStream, FrameEvent, LinkEvent
from roomsense.acquisition.serial_source import OVERFLOW, LineAssembler, LiveSerialSource, list_serial_ports
from roomsense.schemas import SourceMode
from tests.acq_helpers import (
    OTHER_MAC,
    EventCollector,
    FakeSerialFactory,
    fast_acq,
    hello_line,
    rs_receiver,
    rscsi_line,
    stat_line,
    storage_cfg,
)


def frames(events):
    return [e.frame for e in events if isinstance(e, FrameEvent)]


def kinds(events, link=None):
    return [e.kind for e in events if isinstance(e, LinkEvent) and (link is None or e.link_id == link)]


def link_events(events, kind):
    return [e for e in events if isinstance(e, LinkEvent) and e.kind == kind]


def run_source(sessions, *, receivers=None, acq=None, wait=None, timeout=5.0):
    factory = FakeSerialFactory(sessions)
    src = LiveSerialSource(receivers or [rs_receiver()], acq or fast_acq(), storage_cfg(), "live-session",
                           serial_factory=factory)
    col = EventCollector()
    src.start(col)
    if wait is not None:
        assert col.wait_for(wait, timeout), f"timed out; events so far: {kinds(col.snapshot())}"
    return src, col, factory


# ---------------------------------------------------------------------------
# LineAssembler
# ---------------------------------------------------------------------------


def test_line_assembler_handles_partial_chunks_and_crlf():
    la = LineAssembler(64)
    assert la.feed(b"abc") == []
    assert la.feed(b"def\r\nxy") == [b"abcdef\r"]
    assert la.feed(b"z\n\n") == [b"xyz", b""]


def test_line_assembler_discards_overlong_line_until_newline():
    la = LineAssembler(10)
    out = la.feed(b"0123456789AB")  # 12 bytes without newline > cap (10 + 1)
    assert out == [OVERFLOW]
    assert la.feed(b"still-garbage") == []  # still discarding, no second overflow
    assert la.feed(b"-end\nok\n") == [b"ok"]
    assert la.feed(b"0123456789ABCDEF\nfine\n") == [OVERFLOW, b"fine"]


# ---------------------------------------------------------------------------
# Live source with fake ports
# ---------------------------------------------------------------------------


def test_port_opened_without_asserting_reset_lines():
    line = (hello_line() + "\n").encode()
    src, col, factory = run_source([[line]], wait=lambda ev: "HELLO" in kinds(ev))
    src.stop(2.0)
    s = factory.created[0].settings_at_open
    assert s == {"port": "/dev/fake-rx1", "baudrate": 921600, "timeout": 0.2, "dtr": False, "rts": False}


def test_partial_chunked_reads_produce_one_frame():
    hello = (hello_line() + "\n").encode()
    csi = (rscsi_line() + "\n").encode()
    chunks = [hello[:7], hello[7:], csi[:10], csi[10:50], csi[50:200], csi[200:]]
    src, col, _ = run_source([chunks], wait=lambda ev: len(frames(ev)) >= 1)
    src.stop(2.0)
    ev = col.snapshot()
    fr = frames(ev)
    assert len(fr) == 1
    f = fr[0]
    assert f.source_mode == SourceMode.LIVE and f.session_id == "live-session"
    assert f.layout_id == "classic.lltf_only.sec_none.total128.LLTF64"
    assert f.device.identity_source == "firmware_hello"
    assert f.host_arrival_monotonic_ns is not None and f.host_arrival_unix_ns is not None
    assert f.raw_line == rscsi_line()
    assert kinds(ev)[:2] == ["CONNECTED", "HELLO"]


def test_crlf_lines_and_stat_events():
    data = (hello_line() + "\r\n" + rscsi_line() + "\r\n" + stat_line() + "\r\n").encode()
    src, col, _ = run_source([[data]], wait=lambda ev: "STAT" in kinds(ev))
    src.stop(2.0)
    ev = col.snapshot()
    assert len(frames(ev)) == 1
    stat = link_events(ev, "STAT")[0]
    assert stat.data["dropped_queue_full"] == 2 and stat.data["tx_ok"] is None
    hello = link_events(ev, "HELLO")[0]
    assert hello.data["chip"] == "esp32s3"


def test_overlong_line_is_discarded_and_stream_recovers():
    acq = fast_acq(max_line_bytes=1024)
    junk = b"X" * 3000
    good = (hello_line() + "\n" + rscsi_line() + "\n").encode()
    src, col, _ = run_source([[junk[:1500], junk[1500:], b"tail-of-junk\n", good]], acq=acq,
                             wait=lambda ev: len(frames(ev)) >= 1)
    src.stop(2.0)
    ev = col.snapshot()
    errs = link_events(ev, "PARSE_ERROR")
    assert [e.data["code"] for e in errs] == ["LINE_TOO_LONG"]
    assert len(frames(ev)) == 1


def test_parse_errors_are_reported_with_codes():
    bad = rscsi_line()[:-1] + ("0" if rscsi_line()[-1] != "0" else "1")
    data = (hello_line() + "\n" + bad + "\n" + rscsi_line() + "\n").encode()
    src, col, _ = run_source([[data]], wait=lambda ev: len(frames(ev)) >= 1)
    src.stop(2.0)
    errs = link_events(col.snapshot(), "PARSE_ERROR")
    assert [e.data["code"] for e in errs] == ["CRC_MISMATCH"]
    assert errs[0].data["count"] == 1


def test_unconfigured_mac_lines_are_counted_not_emitted_per_line():
    other = "".join(rscsi_line(src_mac=OTHER_MAC, rec_seq=i) + "\n" for i in range(30))
    data = (hello_line() + "\n" + other + rscsi_line(rec_seq=99) + "\n").encode()
    src, col, _ = run_source([[data]], wait=lambda ev: len(frames(ev)) >= 1)
    time.sleep(0.05)
    assert link_events(col.snapshot(), "PARSE_ERROR") == []  # nothing per line
    src.stop(2.0)
    ev = col.snapshot()
    assert len(frames(ev)) == 1
    summaries = [e for e in link_events(ev, "PARSE_ERROR") if e.data["code"] == "MAC_NOT_CONFIGURED"]
    assert len(summaries) == 1 and summaries[0].data["count"] == 30
    assert src.describe()["receivers"][0]["mac_filtered_lines"] == 30


def test_diagnostics_are_rate_limited_and_truncated():
    lines = "".join(f"I ({i}) wifi: log line {i} " + "y" * 400 + "\n" for i in range(100))
    src, col, _ = run_source([[lines.encode()]], wait=lambda ev: len(link_events(ev, "DIAGNOSTIC")) >= 1)
    time.sleep(0.1)
    src.stop(2.0)
    diags = link_events(col.snapshot(), "DIAGNOSTIC")
    shown = [d for d in diags if "suppressed" not in d.data]
    assert 1 <= len(shown) <= 8
    assert all(len(d.detail) <= serial_source.DIAGNOSTIC_MAX_CHARS for d in diags)
    assert sum(d.data.get("suppressed", 0) for d in diags) + len(shown) == 100


def test_disconnect_emits_disconnected_then_reconnecting_and_never_other_modes():
    data = (hello_line() + "\n" + rscsi_line() + "\n").encode()
    sessions = [[data, serial.SerialException("device reports readiness to read but returned no data")]]
    src, col, factory = run_source(sessions, wait=lambda ev: kinds(ev).count("RECONNECTING") >= 3)
    t0 = time.monotonic()
    src.stop(2.0)
    assert time.monotonic() - t0 < 1.0
    ev = col.snapshot()
    k = kinds(ev)
    assert k[0] == "CONNECTED"
    i_disc = k.index("DISCONNECTED")
    assert "RECONNECTING" in k[i_disc + 1 :]
    assert k.count("DISCONNECTED") == 1  # failed re-opens do not repeat it
    assert all(f.source_mode == SourceMode.LIVE for f in frames(ev))
    assert not any(isinstance(e, EndOfStream) for e in ev)
    # Re-open attempts use exponential backoff and each creates a fresh port.
    delays = [e.data["delay_s"] for e in link_events(ev, "RECONNECTING")]
    assert delays[:3] == [0.02, 0.04, 0.08]
    assert len(factory.created) >= 3


def test_reconnects_after_port_returns():
    first = [(hello_line() + "\n" + rscsi_line(rec_seq=1) + "\n").encode(), OSError(5, "Input/output error")]
    second = [(hello_line() + "\n" + rscsi_line(rec_seq=2) + "\n").encode()]
    src, col, _ = run_source([first, second], wait=lambda ev: len(frames(ev)) >= 2)
    src.stop(2.0)
    ev = col.snapshot()
    k = kinds(ev)
    assert k.count("CONNECTED") == 2
    assert k.index("DISCONNECTED") < len(k) - 1 - k[::-1].index("CONNECTED")
    assert [f.frame_counter for f in frames(ev)] == [1, 2]


def test_missing_port_at_start_reports_disconnected():
    src, col, factory = run_source([], wait=lambda ev: "RECONNECTING" in kinds(ev))
    src.stop(2.0)
    ev = col.snapshot()
    assert kinds(ev)[:2] == ["DISCONNECTED", "RECONNECTING"]
    assert "No such file" in link_events(ev, "DISCONNECTED")[0].detail
    assert frames(ev) == []


def test_stop_joins_quickly_with_idle_ports_and_is_idempotent():
    rxs = [rs_receiver("rx1"), rs_receiver("rx2")]
    src, col, _ = run_source([[b""], [b""]], receivers=rxs, wait=lambda ev: kinds(ev).count("CONNECTED") == 2)
    t0 = time.monotonic()
    src.stop(2.0)
    src.stop(2.0)
    assert time.monotonic() - t0 < 1.0
    assert all(not w.thread.is_alive() for w in src._workers)
    assert sorted(src.link_ids()) == ["tx1->rx1", "tx1->rx2"]


def test_sink_exceptions_do_not_kill_reader():
    calls = []

    def bad_sink(ev):
        calls.append(ev)
        raise RuntimeError("consumer bug")

    data = (hello_line() + "\n" + rscsi_line(rec_seq=1) + "\n" + rscsi_line(rec_seq=2) + "\n").encode()
    src = LiveSerialSource([rs_receiver()], fast_acq(), storage_cfg(), "s", serial_factory=FakeSerialFactory([[data]]))
    src.start(bad_sink)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and sum(isinstance(e, FrameEvent) for e in calls) < 2:
        time.sleep(0.01)
    src.stop(2.0)
    assert sum(isinstance(e, FrameEvent) for e in calls) == 2


def test_describe_has_no_secrets_and_validates_receivers():
    src = LiveSerialSource([rs_receiver()], fast_acq(), storage_cfg(), "s", serial_factory=FakeSerialFactory([]))
    d = src.describe()
    assert d["mode"] == "LIVE" and d["simulated"] is False
    assert d["receivers"][0]["port"] == "/dev/fake-rx1"
    assert "token" not in repr(d).lower()
    with pytest.raises(ValueError):
        LiveSerialSource([], fast_acq(), storage_cfg(), "s")
    with pytest.raises(ValueError):
        LiveSerialSource([rs_receiver(), rs_receiver()], fast_acq(), storage_cfg(), "s")


def test_live_source_module_has_no_path_to_replay_or_simulation():
    tree = ast.parse(inspect.getsource(serial_source))
    imported: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    for forbidden in ("replay_source", "synthetic", "ReplaySource", "SyntheticSource", "generate_frames"):
        assert forbidden not in imported and forbidden not in names
    assert "REPLAY" not in names and "SIMULATION" not in names
    assert LiveSerialSource.mode == SourceMode.LIVE


# ---------------------------------------------------------------------------
# Port listing
# ---------------------------------------------------------------------------


def test_list_serial_ports_flags_bridges_and_never_opens(monkeypatch):
    fake_ports = [
        SimpleNamespace(device="/dev/ttyUSB0", description="CP2102 USB to UART", hwid="USB VID:PID=10C4:EA60", vid=0x10C4, pid=0xEA60),
        SimpleNamespace(device="/dev/ttyACM0", description="USB JTAG/serial debug unit", hwid="USB VID:PID=303A:1001", vid=0x303A, pid=0x1001),
        SimpleNamespace(device="/dev/ttyS0", description="n/a", hwid="PNP0501", vid=None, pid=None),
        SimpleNamespace(device="/dev/ttyUSB1", description="USB Serial", hwid="USB VID:PID=1A86:7523", vid=0x1A86, pid=0x7523),
    ]
    monkeypatch.setattr(serial_source.list_ports, "comports", lambda: fake_ports)

    def no_open(*a, **k):
        raise AssertionError("list_serial_ports must not open ports")

    monkeypatch.setattr(serial_source.serial, "Serial", no_open)
    ports = list_serial_ports()
    by_dev = {p["device"]: p for p in ports}
    assert set(ports[0]) == {"device", "description", "hwid", "vid", "pid", "likely_usb_uart_bridge"}
    assert by_dev["/dev/ttyUSB0"]["likely_usb_uart_bridge"] is True
    assert by_dev["/dev/ttyACM0"]["likely_usb_uart_bridge"] is True
    assert by_dev["/dev/ttyUSB1"]["likely_usb_uart_bridge"] is True
    assert by_dev["/dev/ttyS0"]["likely_usb_uart_bridge"] is False
    assert set(serial_source.USB_UART_BRIDGE_VIDS) == {0x303A, 0x10C4, 0x1A86, 0x0403}


def test_boot_rom_garbage_is_a_parse_error_and_stream_continues():
    garbage = bytes([0xE0, 0x9F, 0xFF, 0x00, 0x13]) * 20 + b"\n"
    data = garbage + (hello_line() + "\n" + rscsi_line() + "\n").encode()
    src, col, _ = run_source([[data]], wait=lambda ev: len(frames(ev)) >= 1)
    src.stop(2.0)
    errs = link_events(col.snapshot(), "PARSE_ERROR")
    assert [e.data["code"] for e in errs] == ["NOT_ASCII"]


def test_unexpected_reader_exception_reports_error_and_retries():
    sessions = [[(hello_line() + "\n").encode(), TypeError("driver returned None")]]
    src, col, _ = run_source(sessions, wait=lambda ev: "RECONNECTING" in kinds(ev))
    src.stop(2.0)
    k = kinds(col.snapshot())
    assert k.index("ERROR") < k.index("DISCONNECTED") < k.index("RECONNECTING")
