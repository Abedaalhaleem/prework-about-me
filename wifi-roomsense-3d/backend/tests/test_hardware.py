"""Tests for the read-only hardware inspector (roomsense.hardware).

These run in the build container, which has no USB bus, no ESP32 and no
Wi-Fi adapter. They check software behaviour only: that inspection never
opens serial ports, never runs a Wi-Fi scan or a privileged command, classifies
ports correctly, redacts identifiers and reports "hardware required" honestly.
They say nothing about any real board.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from roomsense import hardware
from roomsense.hardware import (
    ALLOWED_COMMANDS,
    ESPRESSIF_VID,
    USB_BRIDGE_VIDS,
    assess,
    classify_port,
    forbidden_reason,
    inspect_host,
    linux_network_interfaces,
    redact_text,
    render_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "inspect_hardware.py"

# ---------------------------------------------------------------------------
# Process-wide audit hook: records file opens and process launches, but only
# while a test has pushed a recording list. Audit hooks cannot be removed, so
# the hook is inert whenever _AUDIT_STACK is empty.
# ---------------------------------------------------------------------------

_AUDIT_STACK: list[list[tuple[str, tuple[Any, ...]]]] = []
_AUDIT_EVENTS = frozenset({"open", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"})


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if _AUDIT_STACK and event in _AUDIT_EVENTS:
        _AUDIT_STACK[-1].append((event, args))


sys.addaudithook(_audit_hook)


@pytest.fixture
def audit_events():
    events: list[tuple[str, tuple[Any, ...]]] = []
    _AUDIT_STACK.append(events)
    try:
        yield events
    finally:
        _AUDIT_STACK.remove(events)


_SERIAL_DEVICE_RE = re.compile(r"^(/dev/(tty|cu\.|serial|rfcomm)|COM\d+$|\\\\\.\\COM)", re.IGNORECASE)


def _opened_paths(events: list[tuple[str, tuple[Any, ...]]]) -> list[str]:
    paths = []
    for event, args in events:
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            paths.append(os.fsdecode(args[0]))
    return paths


# Commands that would scan for Wi-Fi networks (or change network state).
def _is_scan_like(argv: list[str]) -> bool:
    name = Path(argv[0]).name.lower().removesuffix(".exe")
    rest = [a.lower() for a in argv[1:]]
    return (
        (name in ("iw", "iwlist") and any(a in ("scan", "scanning") for a in rest))
        or (name == "nmcli" and "wifi" in rest)
        or (name == "netsh" and any(a.startswith("networks") or a == "mode=bssid" for a in rest))
        or (name == "airport" and any(a == "-s" or a.startswith("--scan") for a in rest))
        or name in ("sudo", "doas", "pkexec", "su")
    )


# ---------------------------------------------------------------------------
# Fake pyserial ports and fake command outputs
# ---------------------------------------------------------------------------


def _port(device: str, vid: int | None, pid: int | None, **kw: Any) -> SimpleNamespace:
    base = {
        "device": device,
        "vid": vid,
        "pid": pid,
        "description": kw.pop("description", "n/a"),
        "hwid": kw.pop("hwid", "n/a"),
        "serial_number": kw.pop("serial_number", None),
        "manufacturer": kw.pop("manufacturer", None),
        "product": kw.pop("product", None),
        "location": kw.pop("location", None),
        "interface": kw.pop("interface", None),
        "name": Path(device).name,
    }
    base.update(kw)
    return SimpleNamespace(**base)


FAKE_PORTS = [
    _port(
        "/dev/ttyACM0",
        0x303A,
        0x1001,
        description="USB JTAG/serial debug unit",
        hwid="USB VID:PID=303A:1001 SER=F4:12:FA:00:11:22 LOCATION=1-1:1.0",
        serial_number="F4:12:FA:00:11:22",
    ),
    _port(
        "/dev/ttyUSB0",
        0x10C4,
        0xEA60,
        description="CP2102N USB to UART Bridge Controller",
        hwid="USB VID:PID=10C4:EA60 SER=0123456789abcdef LOCATION=1-2",
        serial_number="0123456789abcdef",
    ),
    _port("/dev/ttyUSB1", 0x1A86, 0x7523, description="USB Serial"),
    _port("/dev/ttyUSB2", 0x0403, 0x6001, description="FT232R | USB UART"),
    _port("/dev/ttyACM1", 0x2341, 0x0043, description="Arduino Uno"),
    _port("/dev/ttyS0", None, None),
]

SECRET_SSID = "HomeNetSecret-7f3a"
MAC_A = "3c:a9:f4:12:34:56"
MAC_B = "00:11:22:33:44:55"
MAC_C = "a4:b1:c2:d3:e4:f5"

FAKE_OUTPUTS: dict[tuple[str, ...], str] = {
    ("node", "--version"): "v22.22.2\n",
    ("idf.py", "--version"): "ESP-IDF v5.5.5\n",
    ("systemd-detect-virt", "--vm"): "none\n",
    ("systemd-detect-virt", "--container"): "none\n",
    ("iw", "dev"): (
        "phy#0\n\tInterface wlp2s0\n\t\tifindex 3\n\t\twdev 0x1\n"
        f"\t\taddr {MAC_A}\n\t\tssid {SECRET_SSID}\n\t\ttype managed\n"
        "\t\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz\n"
    ),
    ("networksetup", "-listallhardwareports"): (
        f"\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: {MAC_A}\n\n"
        f"Hardware Port: Thunderbolt Bridge\nDevice: bridge0\nEthernet Address: {MAC_B}\n\n"
        "VLAN Configurations\n===================\n"
    ),
    ("netsh", "wlan", "show", "interfaces"): (
        "\nThere is 1 interface on the system:\n\n"
        "    Name                   : Wi-Fi\n"
        "    Description            : Intel(R) Wi-Fi 6 AX201 160MHz\n"
        "    GUID                   : 12345678-aaaa-bbbb-cccc-1234567890ab\n"
        f"    Physical address       : {MAC_C}\n"
        "    State                  : connected\n"
        f"    SSID                   : {SECRET_SSID}\n"
        f"    AP BSSID               : {MAC_B}\n"
        "    Radio type             : 802.11ax\n"
        f"    Profile                : {SECRET_SSID}\n"
    ),
    ("sysctl", "-n", "machdep.cpu.brand_string"): "Apple M2\n",
    ("sysctl", "-n", "hw.memsize"): "17179869184\n",
    ("sysctl", "-n", "kern.hv_vmm_present"): "0\n",
}


class _Recorder:
    """Stands in for subprocess.run; records argv and keyword arguments."""

    def __init__(self, raise_exc: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.raise_exc = raise_exc

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        key = (Path(argv[0]).name, *argv[1:])
        out = FAKE_OUTPUTS.get(key, "")
        rc = 1 if out.strip() == "none" else 0  # systemd-detect-virt exits 1 for "none"
        return subprocess.CompletedProcess(argv, rc, stdout=out.encode(), stderr=b"")


@pytest.fixture
def no_serial_open(monkeypatch: pytest.MonkeyPatch):
    """Make any attempt to construct/open a pyserial port fail the test."""
    import serial

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"serial port opened: args={args!r} kwargs={kwargs!r}")

    class _NoSerial:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _boom(*args, **kwargs)

    monkeypatch.setattr(serial, "Serial", _NoSerial)
    monkeypatch.setattr(serial, "serial_for_url", _boom)


@pytest.fixture
def fake_ports(monkeypatch: pytest.MonkeyPatch):
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda *a, **k: list(FAKE_PORTS))
    return FAKE_PORTS


@pytest.fixture
def all_tools_present(monkeypatch: pytest.MonkeyPatch):
    """Every allow-listed tool is 'on PATH' and answers with canned output."""
    rec = _Recorder()
    monkeypatch.setattr(hardware.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(hardware.subprocess, "run", rec)
    return rec


# ---------------------------------------------------------------------------
# Real run in this environment
# ---------------------------------------------------------------------------


def test_inspect_host_runs_here_and_is_json_safe(no_serial_open, audit_events) -> None:
    report = inspect_host()
    text = json.dumps(report, allow_nan=False)
    assert json.loads(text)["format"] == hardware.REPORT_FORMAT
    for key in ("host", "virtualisation", "detected", "software", "assessment", "recommended", "commands_run"):
        assert key in report
    a = report["assessment"]
    assert isinstance(a["csi_path_available"], bool)
    assert a["csi_path_confirmed"] is False  # the inspector can never confirm; it does not open ports
    assert a["hardware_required"] is (not a["csi_path_available"])
    assert report["recommended"]["status"].startswith("RECOMMENDED - not detected")
    assert report["identifiers_redacted"] is True

    opened = _opened_paths(audit_events)
    assert not [p for p in opened if _SERIAL_DEVICE_RE.match(p)], opened
    launched = [list(args[1]) for ev, args in audit_events if ev == "subprocess.Popen"]
    for argv in launched:
        assert not _is_scan_like([os.fsdecode(a) for a in argv]), argv
        name = Path(os.fsdecode(argv[0])).name
        assert (name, *map(os.fsdecode, argv[1:])) in ALLOWED_COMMANDS, argv


def test_build_container_reports_hardware_required_without_esp32_ports(no_serial_open) -> None:
    report = inspect_host()
    ports = report["detected"]["serial_ports"]
    if any(p["esp32_candidate"] for p in ports):
        pytest.skip("an ESP32-class USB bridge is attached to this machine")
    assert report["assessment"]["hardware_required"] is True
    assert report["assessment"]["csi_path_available"] is False
    md = render_markdown(report)
    assert "HARDWARE REQUIRED" in md
    assert "NOT evidence of CSI access" in md


def test_hostname_is_never_collected(monkeypatch: pytest.MonkeyPatch, no_serial_open) -> None:
    monkeypatch.setattr(hardware.platform, "node", lambda: "unique-hostname-q9z")
    assert "unique-hostname-q9z" not in json.dumps(inspect_host())


# ---------------------------------------------------------------------------
# Serial port classification (fake pyserial lists)
# ---------------------------------------------------------------------------


def test_classify_fake_ports() -> None:
    by_dev = {p.device: classify_port(p) for p in FAKE_PORTS}
    assert by_dev["/dev/ttyACM0"]["classification"] == "espressif_usb"
    assert by_dev["/dev/ttyACM0"]["esp32_candidate"] is True
    assert by_dev["/dev/ttyACM0"]["vid_hex"] == "0x303A" and by_dev["/dev/ttyACM0"]["pid_hex"] == "0x1001"
    for dev, vid in (("/dev/ttyUSB0", 0x10C4), ("/dev/ttyUSB1", 0x1A86), ("/dev/ttyUSB2", 0x0403)):
        assert by_dev[dev]["classification"] == "usb_uart_bridge"
        assert by_dev[dev]["esp32_candidate"] is True
        assert by_dev[dev]["bridge"] == USB_BRIDGE_VIDS[vid]
        assert "Not proof of an ESP32" in by_dev[dev]["note"]
    assert by_dev["/dev/ttyACM1"]["classification"] == "other_usb_serial"
    assert by_dev["/dev/ttyACM1"]["esp32_candidate"] is False
    assert by_dev["/dev/ttyS0"]["classification"] == "non_usb_or_unknown"
    assert by_dev["/dev/ttyS0"]["vid"] is None and by_dev["/dev/ttyS0"]["vid_hex"] is None


def test_classify_port_redacts_usb_serial_numbers() -> None:
    red = classify_port(FAKE_PORTS[0])
    assert red["serial_number"] == "<redacted>"
    assert "F4:12:FA" not in (red["hwid"] or "")
    assert "SER=<redacted>" in red["hwid"]
    raw = classify_port(FAKE_PORTS[0], redact=False)
    assert raw["serial_number"] == "F4:12:FA:00:11:22"
    assert "SER=F4:12:FA:00:11:22" in raw["hwid"]


def test_classify_port_tolerates_odd_objects() -> None:
    weird = SimpleNamespace(device="COM7", vid="303A", pid=True)  # wrong types, missing attributes
    info = classify_port(weird)
    assert info["vid"] is None and info["pid"] is None
    assert info["classification"] == "non_usb_or_unknown"
    assert info["accessible_rw"] is None  # not a /dev path: never probed


def test_fake_port_list_flows_into_report_without_opening(fake_ports, no_serial_open, audit_events) -> None:
    report = inspect_host()
    devices = [p["device"] for p in report["detected"]["serial_ports"]]
    assert devices == sorted(p.device for p in FAKE_PORTS)
    a = report["assessment"]
    assert a["csi_path_available"] is True
    assert a["csi_path_confirmed"] is False
    assert a["hardware_required"] is False
    assert a["confidence"] == "likely"  # an Espressif VID is present
    assert set(a["candidate_ports"]) == {"/dev/ttyACM0", "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"}
    assert not [p for p in _opened_paths(audit_events) if _SERIAL_DEVICE_RE.match(p)]
    md = render_markdown(report)
    assert "HARDWARE REQUIRED" not in md
    assert "POSSIBLE (not confirmed)" in md
    assert "FT232R \\| USB UART" in md  # table cells escape pipes
    assert "0123456789abcdef" not in md and "0123456789abcdef" not in json.dumps(report)


def test_missing_pyserial_is_reported_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _no_serial(name: str, *args: Any, **kwargs: Any):
        if name == "serial.tools" or name.startswith("serial"):
            raise ImportError("No module named 'serial'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_serial)
    ports, err = hardware.list_serial_ports_readonly()
    assert ports == [] and err and "pyserial is not installed" in err
    a = assess({"detected": {"serial_ports": [], "serial_port_error": err}})
    assert a["hardware_required"] is True
    assert any("could not be listed" in r for r in a["reasons"])


def test_enumeration_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from serial.tools import list_ports

    def _fail() -> list[Any]:
        raise OSError("permission denied on /sys")

    monkeypatch.setattr(list_ports, "comports", _fail)
    ports, err = hardware.list_serial_ports_readonly()
    assert ports == []
    assert err is not None and "OSError" in err


def test_vid_table_matches_live_serial_source() -> None:
    try:
        from roomsense.acquisition.serial_source import USB_UART_BRIDGE_VIDS
    except Exception as exc:  # noqa: BLE001 - another module may be mid-edit
        pytest.skip(f"serial_source not importable: {exc}")
    assert set(USB_UART_BRIDGE_VIDS) == set(USB_BRIDGE_VIDS)
    assert ESPRESSIF_VID in USB_BRIDGE_VIDS


# ---------------------------------------------------------------------------
# Commands: never scans, never privileged, allow-list only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_no_scan_or_privileged_commands_on_any_platform(
    system: str, monkeypatch: pytest.MonkeyPatch, all_tools_present: _Recorder, no_serial_open
) -> None:
    monkeypatch.setattr(hardware.platform, "system", lambda: system)
    report = inspect_host()
    assert all_tools_present.calls, "expected some read-only commands to run"
    for argv, kwargs in all_tools_present.calls:
        assert not _is_scan_like(argv), argv
        assert forbidden_reason(argv) is None, argv
        assert (Path(argv[0]).name, *argv[1:]) in ALLOWED_COMMANDS, argv
        assert not kwargs.get("shell"), argv
        assert 0 < kwargs["timeout"] <= hardware.IDF_TIMEOUT_S
        assert kwargs["stdin"] is subprocess.DEVNULL
    names = {Path(argv[0]).name for argv, _ in all_tools_present.calls}
    if system == "Linux":
        assert {"iw", "systemd-detect-virt"} <= names
    elif system == "Darwin":
        assert {"networksetup", "sysctl"} <= names
        assert report["host"]["cpu_model"] == "Apple M2"
        assert report["host"]["ram_total_bytes"] == 17179869184
    else:
        assert "netsh" in names
    assert report["software"]["esp_idf"]["version"] == "v5.5.5"
    assert report["software"]["esp_idf"]["matches_pinned"] is True
    assert report["software"]["node"]["version"] == "v22.22.2"
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_wifi_listing_output_is_redacted(system: str, monkeypatch: pytest.MonkeyPatch, all_tools_present) -> None:
    monkeypatch.setattr(hardware.platform, "system", lambda: system)
    text = json.dumps(inspect_host())
    for secret in (SECRET_SSID, MAC_A, MAC_B, MAC_C, "12345678-aaaa-bbbb-cccc-1234567890ab"):
        assert secret not in text
    unredacted = json.dumps(inspect_host(redact=False))
    assert SECRET_SSID in unredacted or system == "Darwin"  # networksetup prints no SSID
    assert MAC_A in unredacted or system == "Windows"


def test_parsed_wifi_interfaces_per_platform(monkeypatch: pytest.MonkeyPatch, all_tools_present) -> None:
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    win = inspect_host()["detected"]
    assert win["network_interfaces"] == [
        {
            "name": "Wi-Fi",
            "wireless": True,
            "description": "Intel(R) Wi-Fi 6 AX201 160MHz",
            "state": "connected",
            "radio_type": "802.11ax",
        }
    ]
    monkeypatch.setattr(hardware.platform, "system", lambda: "Darwin")
    mac = inspect_host()["detected"]["network_interfaces"]
    assert {"name": "en0", "hardware_port": "Wi-Fi", "wireless": True} in mac
    assert {"name": "bridge0", "hardware_port": "Thunderbolt Bridge", "wireless": False} in mac
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    listing = inspect_host()["detected"]["wifi_interface_listings"]
    assert listing[0]["command"] == "iw dev"
    assert listing[0]["parsed"] == [{"interface": "wlp2s0", "phy": "phy#0", "type": "managed"}]


def test_wireless_adapter_is_not_evidence_of_csi(monkeypatch: pytest.MonkeyPatch, all_tools_present) -> None:
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    report = inspect_host()
    a = report["assessment"]
    if report["detected"]["serial_ports"] and any(p["esp32_candidate"] for p in report["detected"]["serial_ports"]):
        pytest.skip("an ESP32-class USB bridge is attached to this machine")
    assert a["csi_path_available"] is False and a["hardware_required"] is True
    assert any("NOT evidence of CSI access" in r for r in a["reasons"])
    assert any("Wi-Fi" in r and "does not use them" in r for r in a["reasons"])


@pytest.mark.parametrize(
    "argv",
    [
        ["iw", "dev", "wlan0", "scan"],
        ["/usr/sbin/iw", "wlan0", "scan", "dump"],
        ["iwlist", "wlan0", "scanning"],
        ["nmcli", "dev", "wifi", "list"],
        ["nmcli", "device", "wifi"],
        ["netsh", "wlan", "show", "networks"],
        ["netsh.exe", "wlan", "show", "networks", "mode=bssid"],
        ["netsh", "wlan", "connect", "name=x"],
        ["airport", "-s"],
        ["/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport", "--scan"],
        ["sudo", "usermod", "-a", "-G", "dialout", "me"],
        ["pkexec", "true"],
        ["idf.py", "-p", "/dev/ttyUSB0", "flash"],
        ["esptool.py", "erase_flash"],
        ["idf.py", "monitor"],
        ["networksetup", "-setairportpower", "en0", "off"],
    ],
)
def test_forbidden_commands_are_refused(argv: list[str]) -> None:
    assert forbidden_reason(argv) is not None


def test_allow_list_contains_no_forbidden_command() -> None:
    for cmd in ALLOWED_COMMANDS:
        assert forbidden_reason(cmd) is None, cmd
        assert not _is_scan_like(list(cmd)), cmd


def test_runner_refuses_before_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(raise_exc=AssertionError("subprocess.run must not be called"))
    monkeypatch.setattr(hardware.subprocess, "run", rec)
    monkeypatch.setattr(hardware.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    ins = hardware._Inspector(redact=True, system="Linux")
    for argv in (("iw", "dev", "wlan0", "scan"), ("sudo", "iw", "dev"), ("uname", "-a"), ("iw", "dev", "extra")):
        res = ins.run(argv)
        assert res["refused"], argv
        assert res["found"] is False
    assert rec.calls == []
    assert all(c["refused"] for c in ins.commands)


def test_command_timeout_is_recorded_and_inspection_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(raise_exc=subprocess.TimeoutExpired(cmd="x", timeout=0.01))
    monkeypatch.setattr(hardware.subprocess, "run", rec)
    monkeypatch.setattr(hardware.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    report = inspect_host()
    assert rec.calls
    assert all(c["timed_out"] for c in report["commands_run"] if c["found"])
    assert report["software"]["esp_idf"]["version"] is None
    assert report["software"]["esp_idf"]["idf_py_timed_out"] is True
    assert report["software"]["node"]["version"] is None
    json.dumps(report, allow_nan=False)


def test_oserror_from_command_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(raise_exc=PermissionError("denied"))
    monkeypatch.setattr(hardware.subprocess, "run", rec)
    monkeypatch.setattr(hardware.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    report = inspect_host()
    assert any(c["error"] and "PermissionError" in c["error"] for c in report["commands_run"])


def test_long_command_output_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    big = "x" * (hardware.MAX_OUTPUT_CHARS * 5)

    def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=big.encode(), stderr=big.encode())

    monkeypatch.setattr(hardware.subprocess, "run", _run)
    monkeypatch.setattr(hardware.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    ins = hardware._Inspector(redact=True, system="Linux")
    res = ins.run(("node", "--version"))
    assert len(res["stdout"]) < hardware.MAX_OUTPUT_CHARS + 100
    assert res["stdout"].endswith("[output truncated]")


def test_idf_version_mismatch_is_flagged(monkeypatch: pytest.MonkeyPatch, all_tools_present) -> None:
    monkeypatch.setitem(FAKE_OUTPUTS, ("idf.py", "--version"), "ESP-IDF v5.4.1-dirty\n")
    report = inspect_host()
    idf = report["software"]["esp_idf"]
    assert idf["version"] == "v5.4.1-dirty"
    assert idf["matches_pinned"] is False
    assert any("pinned to v5.5.5" in r for r in report["assessment"]["reasons"])


# ---------------------------------------------------------------------------
# Linux sysfs parsing
# ---------------------------------------------------------------------------


def _fake_sysfs(tmp: Path) -> Path:
    net = tmp / "class" / "net"
    drivers = tmp / "bus" / "pci" / "drivers"
    (drivers / "iwlwifi").mkdir(parents=True)
    (drivers / "e1000e").mkdir(parents=True)
    ieee = tmp / "class" / "ieee80211" / "phy0"
    ieee.mkdir(parents=True)

    wl_dev = tmp / "devices" / "pci0000:00" / "0000:02:00.0"
    wl_dev.mkdir(parents=True)
    (wl_dev / "vendor").write_text("0x8086\n")
    (wl_dev / "device").write_text("0x4235\n")
    os.symlink(drivers / "iwlwifi", wl_dev / "driver")
    wlan = net / "wlan0"
    wlan.mkdir(parents=True)
    (wlan / "wireless").mkdir()
    os.symlink(ieee, wlan / "phy80211")
    os.symlink(wl_dev, wlan / "device")
    (wlan / "address").write_text(f"{MAC_A}\n")
    (wlan / "operstate").write_text("up\n")

    eth_dev = tmp / "devices" / "pci0000:00" / "0000:00:1f.6"
    eth_dev.mkdir(parents=True)
    (eth_dev / "vendor").write_text("0x8086\n")
    (eth_dev / "device").write_text("0x15d7\n")
    os.symlink(drivers / "e1000e", eth_dev / "driver")
    eth = net / "eth0"
    eth.mkdir()
    os.symlink(eth_dev, eth / "device")
    (eth / "address").write_text(f"{MAC_B}\n")

    (net / "lo").mkdir()
    return net


@pytest.mark.skipif(os.name != "posix", reason="uses symlinks like Linux sysfs")
def test_linux_sysfs_interfaces(tmp_path: Path) -> None:
    ifaces = {i["name"]: i for i in linux_network_interfaces(_fake_sysfs(tmp_path))}
    assert set(ifaces) == {"wlan0", "eth0", "lo"}
    wl = ifaces["wlan0"]
    assert wl["wireless"] is True and wl["phy80211"] == "phy0" and wl["driver"] == "iwlwifi"
    assert (wl["bus_vendor_id"], wl["bus_device_id"]) == ("0x8086", "0x4235")
    assert wl["operstate"] == "up"
    assert "Linux 802.11n CSI Tool" in wl["research_tool_hint"]
    assert "does not mean this adapter is supported" in wl["research_tool_hint"]
    assert ifaces["eth0"]["wireless"] is False and ifaces["eth0"]["driver"] == "e1000e"
    assert ifaces["eth0"]["research_tool_hint"] is None
    assert ifaces["lo"]["driver"] is None
    text = json.dumps(list(ifaces.values()))
    assert MAC_A not in text and MAC_B not in text  # MAC addresses are never read


def test_linux_sysfs_missing_directory(tmp_path: Path) -> None:
    assert linux_network_interfaces(tmp_path / "does-not-exist") == []


def test_research_driver_hint_does_not_enable_csi() -> None:
    report = {
        "detected": {
            "serial_ports": [],
            "network_interfaces": [
                {
                    "name": "wlan0",
                    "wireless": True,
                    "driver": "ath9k",
                    "research_tool_hint": hardware._research_hint("ath9k"),
                },
            ],
        }
    }
    a = assess(report)
    assert a["csi_path_available"] is False and a["hardware_required"] is True
    assert any("Atheros CSI Tool" in r for r in a["reasons"])


# ---------------------------------------------------------------------------
# assess() and render_markdown()
# ---------------------------------------------------------------------------


def test_assess_no_ports_requires_hardware() -> None:
    a = assess({"detected": {"serial_ports": []}})
    assert a == {
        **a,
        "csi_path_available": False,
        "csi_path_confirmed": False,
        "hardware_required": True,
        "confidence": "none",
        "candidate_ports": [],
    }
    assert any("No serial ports were found" in r for r in a["reasons"])
    assert any("PARTS_LIST" in s for s in a["next_steps"])
    assert any("explicit approval" in s for s in a["next_steps"])


def test_assess_tolerates_empty_or_malformed_report() -> None:
    for rep in ({}, {"detected": None}, {"detected": {"serial_ports": None}}, {"detected": {"serial_ports": ["junk"]}}):
        a = assess(rep)
        assert a["hardware_required"] is True and a["csi_path_available"] is False


def test_assess_generic_bridge_is_only_possible() -> None:
    port = classify_port(FAKE_PORTS[1])  # CP210x
    a = assess({"detected": {"serial_ports": [port]}})
    assert a["csi_path_available"] is True
    assert a["csi_path_confirmed"] is False
    assert a["confidence"] == "possible"
    assert any("RSHELLO" in r for r in a["reasons"])
    assert any("Unplug" in s for s in a["next_steps"])


def test_assess_unrelated_usb_serial_is_not_a_path() -> None:
    a = assess({"detected": {"serial_ports": [classify_port(FAKE_PORTS[4]), classify_port(FAKE_PORTS[5])]}})
    assert a["csi_path_available"] is False and a["hardware_required"] is True
    assert any("/dev/ttyACM1" in r and "/dev/ttyS0" in r for r in a["reasons"])


def test_render_markdown_sections_and_banner(no_serial_open) -> None:
    report = inspect_host()
    report["detected"]["serial_ports"] = []
    report["assessment"] = assess(report)
    md = render_markdown(report)
    for heading in (
        "# RoomSense hardware inspection",
        "## Assessment",
        "## Host",
        "## Serial ports (listed with pyserial; never opened)",
        "## Network interfaces",
        "## ESP-IDF",
        "## Research CSI paths for PCs (not installed, not used)",
        "## Recommended hardware (NOT detected - recommendations only)",
        "## Commands run",
    ):
        assert heading in md
    assert "**HARDWARE REQUIRED**" in md
    assert hardware.REGENERATE_COMMAND in md
    assert "ESP32-S3-DevKitC-1U-N8R8" in md


def test_render_markdown_handles_minimal_report() -> None:
    md = render_markdown({})
    assert "HARDWARE REQUIRED" in md  # assess() on an empty report
    assert "No serial ports were listed." in md


def test_recommendations_carry_citations_and_no_prices() -> None:
    rec = hardware.RECOMMENDED
    for board in rec["boards"]:
        assert board["source"].startswith("espressif/esp-dev-kits@")
    blob = json.dumps(rec)
    assert not re.search(r"[$€£¥]\s?\d|\bUSD\b|\bEUR\b|\bprice\b", blob, re.IGNORECASE)
    assert "C6" in blob and "UNDOCUMENTED_LAYOUT_ASSUMPTION" in blob
    for path in hardware.PC_RESEARCH_CSI_PATHS:
        assert path["source"].startswith("https://github.com/")
        assert "privileged" in path["requires"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_text_patterns() -> None:
    out = redact_text(FAKE_OUTPUTS[("netsh", "wlan", "show", "interfaces")])
    assert SECRET_SSID not in out and MAC_B not in out and MAC_C not in out
    assert "SSID                   : <redacted>" in out
    assert "State                  : connected" in out  # non-identifying fields stay
    out = redact_text(FAKE_OUTPUTS[("iw", "dev")])
    assert "ssid <redacted>" in out and MAC_A not in out and "type managed" in out
    assert redact_text("SER=ABC123 LOCATION=1-1") == "SER=<redacted> LOCATION=1-1"
    assert redact_text("mac 00-11-22-33-44-55 end") == "mac <redacted-mac> end"


def test_redact_home_directory_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware.Path, "home", classmethod(lambda cls: Path("/home/alice")))
    assert redact_text("/home/alice/esp/esp-idf") == "~/esp/esp-idf"
    assert redact_text("/home/alicexyz/other") == "/home/alicexyz/other"
    assert hardware._redact_path("/home/alice", True) == "~"
    assert hardware._redact_path("/home/alice/x", False) == "/home/alice/x"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_cli():
    spec = importlib.util.spec_from_file_location("inspect_hardware_cli", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_json_and_write(tmp_path: Path, capsys: pytest.CaptureFixture[str], no_serial_open) -> None:
    cli = _load_cli()
    assert cli.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["format"] == hardware.REPORT_FORMAT

    out = tmp_path / "report.md"
    assert cli.main(["--write", str(out)]) == 0
    assert "wrote" in capsys.readouterr().out
    assert out.read_text(encoding="utf-8").startswith("# RoomSense hardware inspection")

    assert cli.main(["--write", str(tmp_path)]) == 2  # a directory
    assert cli.main(["--write", str(tmp_path / "missing" / "r.md")]) == 2  # never creates directories
    assert not (tmp_path / "missing").exists()


def test_cli_runs_as_a_plain_script(tmp_path: Path) -> None:
    # Different working directory on purpose: the script must find backend/ itself.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["generator"] == "roomsense.hardware.inspect_host"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_failing_section_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch, no_serial_open) -> None:
    def _broken(self: Any) -> Any:
        raise RuntimeError("sysfs exploded")

    monkeypatch.setattr(hardware._Inspector, "network", _broken)
    monkeypatch.setattr(hardware._Inspector, "virtualisation", _broken)
    report = inspect_host()
    assert report["detected"]["network_interfaces"] == []
    assert report["virtualisation"]["summary"].startswith("unavailable")
    assert any("network inspection failed: RuntimeError: sysfs exploded" in e for e in report["errors"])
    assert any("virtualisation inspection failed" in e for e in report["errors"])
    assert "## Errors" in render_markdown(report)
    json.dumps(report, allow_nan=False)


def test_exists_reports_none_on_permission_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "forbidden"
    real_exists = Path.exists

    def _exists(self: Path, *args: Any, **kwargs: Any) -> bool:
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", _exists)
    assert hardware._exists(target) is None
    assert hardware._exists(tmp_path) is True


def test_concurrent_inspections_do_not_share_state(no_serial_open) -> None:
    import threading

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def _work() -> None:
        try:
            results.append(inspect_host())
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=_work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and len(results) == 4
    # Each report keeps its own command log; RECOMMENDED is copied, not shared.
    assert len({id(r["commands_run"]) for r in results}) == 4
    results[0]["recommended"]["boards"].clear()
    assert hardware.RECOMMENDED["boards"] and results[1]["recommended"]["boards"]


def test_netsh_parser_handles_several_interfaces_and_noise() -> None:
    text = (
        "There are 2 interfaces on the system:\n\n"
        "    Name                   : Wi-Fi\n    State                  : disconnected\n"
        "    Radio type             : 802.11n\n\n"
        "    Name                   : Wi-Fi 2\n    Description            : USB adapter\n"
        "    State                  : connected\n    SSID                   : <redacted>\n\n"
        "Hosted network status  : Not available\n"
    )
    parsed = hardware._parse_netsh_interfaces(text)
    assert [p["name"] for p in parsed] == ["Wi-Fi", "Wi-Fi 2"]
    assert parsed[0] == {"name": "Wi-Fi", "wireless": True, "state": "disconnected", "radio_type": "802.11n"}
    assert "ssid" not in json.dumps(parsed).lower()
    assert hardware._parse_netsh_interfaces("The Wireless AutoConfig Service (wlansvc) is not running.") == []


def test_render_markdown_with_odd_values() -> None:
    report = {
        "detected": {
            "serial_ports": [{"device": "COM3", "vid": None, "note": "x"}],
            "network_interfaces": [{"name": "a|b", "wireless": None}],
            "wifi_interface_listings": [{"command": "iw dev", "returncode": None, "output": "```\nnested fence"}],
        },
        "commands_run": [{"argv": ["iw", "dev"], "found": False}],
        "host": {"ram_total_bytes": True},
    }
    md = render_markdown(report)
    assert "| a\\|b |" in md
    assert "````text\n```\nnested fence\n````" in md  # a longer fence wraps output that contains a fence
    assert "RAM | unavailable" in md


def test_assess_tolerates_wrongly_typed_sections() -> None:
    rep = {
        "detected": {"serial_ports": "COM3", "network_interfaces": 5, "serial_permissions": "x"},
        "software": {"esp_idf": ["v5.5.5"]},
        "virtualisation": "docker",
        "host": None,
    }
    a = assess(rep)
    assert a["hardware_required"] is True and a["csi_path_available"] is False
    assert any("could not be listed" in r for r in a["reasons"])


def test_virtualisation_not_claimed_where_not_checked(monkeypatch: pytest.MonkeyPatch, all_tools_present) -> None:
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    virt = inspect_host()["virtualisation"]
    assert virt["summary"] == "Virtualisation was not checked on this operating system"
    assert virt["likely_container"] is None and virt["likely_virtual_machine"] is None


def test_render_markdown_tolerates_wrongly_typed_sections() -> None:
    md = render_markdown(
        {
            "assessment": "yes",
            "host": [],
            "virtualisation": "docker",
            "detected": {"serial_ports": ["junk", {"device": "/dev/ttyUSB9", "vid": 0x10C4}], "network_interfaces": 3},
            "software": {"esp_idf": "v5.5.5"},
            "recommended": {"boards": "none", "upstream_advice": [1, 2]},
            "commands_run": "none",
            "errors": "oops",
        }
    )
    assert "HARDWARE REQUIRED" in md  # recomputed: the port has no esp32_candidate flag
    assert "/dev/ttyUSB9" in md
