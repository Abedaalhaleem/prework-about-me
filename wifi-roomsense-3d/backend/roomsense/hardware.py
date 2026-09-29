"""Non-destructive inspection of the computer RoomSense runs on.

:func:`inspect_host` answers two questions for the Hardware page
(``GET /api/hardware``) and for ``HARDWARE_REPORT.md``:

1. What does this computer expose that matters for CSI acquisition: serial
   ports and the USB-UART bridges commonly found on ESP32 boards, Wi-Fi
   interfaces, ESP-IDF, and virtualisation that would hide USB devices?
2. Is RoomSense's documented acquisition path possibly present? That path is
   ESP32 boards running the RoomSense firmware (``firmware/esp32``), each
   receiver connected to this computer over USB serial.

Safety rules enforced here and covered by ``tests/test_hardware.py``:

* Serial ports are enumerated with pyserial's ``list_ports`` only and are
  never opened. Opening a port toggles DTR/RTS, which resets many ESP32
  boards, and could disturb whatever else is attached.
* No Wi-Fi scanning. External commands come from a fixed allow-list of
  read-only commands (:data:`ALLOWED_COMMANDS`), each with a short timeout.
  Anything else is refused before a process is created.
* Nothing privileged: no sudo, no driver or SDK installs, no flashing, no
  writes. (The CLI in ``scripts/inspect_hardware.py`` writes only the report
  file it is explicitly asked to write.)
* Identifiers are redacted by default (MAC addresses, SSIDs/BSSIDs, adapter
  GUIDs, USB serial numbers, the home directory) so that a report can be
  shared without leaking them. The hostname and user name are never
  collected.

A built-in Wi-Fi adapter or working network connectivity is **not** evidence
of CSI access. Ordinary PC Wi-Fi drivers do not expose CSI; the research
tools that do (:data:`PC_RESEARCH_CSI_PATHS`) need specific chips plus
modified drivers or firmware, and RoomSense neither installs nor reads them.

The report is JSON-safe (plain dicts, lists, strings, numbers, booleans and
``None``). ``None`` always means "not available / not collected".
"""

from __future__ import annotations

import copy
import datetime as _dt
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "REPORT_FORMAT",
    "PINNED_IDF_VERSION",
    "PINNED_IDF_COMMIT",
    "ESP_CSI_COMMIT",
    "ESP_DEV_KITS_COMMIT",
    "SERIAL_BAUD",
    "REGENERATE_COMMAND",
    "ESPRESSIF_VID",
    "USB_BRIDGE_VIDS",
    "ALLOWED_COMMANDS",
    "PC_RESEARCH_CSI_PATHS",
    "RECOMMENDED",
    "forbidden_reason",
    "classify_port",
    "list_serial_ports_readonly",
    "linux_network_interfaces",
    "redact_text",
    "inspect_host",
    "assess",
    "render_markdown",
]

REPORT_FORMAT = "roomsense-hardware-report-v1"

# Pinned upstream references. The CSI layout tables in roomsense.csi_layouts
# and the Kconfig names in firmware/esp32 were checked against exactly these.
PINNED_IDF_VERSION = "v5.5.5"
PINNED_IDF_COMMIT = "b774170ff46c393eeb5e495ea37936038d3f4f4f"
ESP_CSI_COMMIT = "8633d67152db2808f141cc1595970aa9cf406045"
ESP_DEV_KITS_COMMIT = "ceaefbd43f80c01496818612b8b874b04eca8038"
SERIAL_BAUD = 921600

REGENERATE_COMMAND = "cd backend && uv run python ../scripts/inspect_hardware.py --write ../HARDWARE_REPORT.local.md"

DEFAULT_TIMEOUT_S = 3.0
# idf.py checks its Python environment before printing the version, which can
# take several seconds on a cold start. It is still bounded.
IDF_TIMEOUT_S = 15.0
MAX_OUTPUT_CHARS = 4000  # per stream; command output is kept only as an excerpt
MAX_PORTS = 64
MAX_INTERFACES = 64
MAX_PROC_READ_BYTES = 256 * 1024

# Linux sysfs locations. Module-level so tests can point them at a fake tree.
SYS_CLASS_NET = Path("/sys/class/net")
SYS_BUS_USB = Path("/sys/bus/usb")
PROC_CPUINFO = Path("/proc/cpuinfo")
PROC_MEMINFO = Path("/proc/meminfo")
PROC_1_CGROUP = Path("/proc/1/cgroup")

# ---------------------------------------------------------------------------
# USB vendor IDs
# ---------------------------------------------------------------------------

ESPRESSIF_VID = 0x303A

# USB vendor IDs of the bridges found on common ESP32 boards. A match means
# "an ESP32 board is plausibly attached", never "an ESP32 is attached":
# CP210x, CH34x and FTDI bridges are used by many unrelated devices too.
# Keep in sync with roomsense.acquisition.serial_source.USB_UART_BRIDGE_VIDS
# (a test checks this).
USB_BRIDGE_VIDS: dict[int, str] = {
    0x303A: "Espressif USB (USB-Serial/JTAG or native USB)",
    0x10C4: "Silicon Labs CP210x USB-UART bridge",
    0x1A86: "WCH CH34x USB-UART bridge",
    0x0403: "FTDI USB-UART bridge",
}

# ---------------------------------------------------------------------------
# External commands: allow-list and deny rules
# ---------------------------------------------------------------------------

# Every external command this module may run. All are read-only listings and
# none scans for Wi-Fi networks. _Inspector.run() refuses anything else, so a
# future edit cannot slip in e.g. an "iw ... scan" without changing this table
# and failing the tests.
ALLOWED_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("node", "--version"),
        ("idf.py", "--version"),
        ("systemd-detect-virt", "--vm"),
        ("systemd-detect-virt", "--container"),
        ("iw", "dev"),  # lists local interfaces; does not scan
        ("networksetup", "-listallhardwareports"),
        # Shows the local adapter state. Recent Windows builds may answer with
        # a location-permission message instead; that text is kept as-is.
        ("netsh", "wlan", "show", "interfaces"),
        ("sysctl", "-n", "machdep.cpu.brand_string"),
        ("sysctl", "-n", "hw.memsize"),
        ("sysctl", "-n", "kern.hv_vmm_present"),
    }
)

_ESCALATION_PROGRAMS = frozenset({"sudo", "doas", "pkexec", "su", "runas", "gsudo"})
_FLASH_TOOLS = frozenset({"idf.py", "esptool", "esptool.py", "espefuse", "espefuse.py"})
_FLASH_VERBS = frozenset(
    {
        "flash",
        "app-flash",
        "erase-flash",
        "erase_flash",
        "erase-region",
        "erase_region",
        "write-flash",
        "write_flash",
        "write_flash_status",
        "burn_efuse",
        "burn-efuse",
        "monitor",  # opens the serial port
        "install",
    }
)


def _prog_name(arg0: str) -> str:
    name = Path(arg0).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def forbidden_reason(argv: Sequence[str]) -> str | None:
    """Why ``argv`` must never be run by the inspector, or ``None``.

    This is a deny-list kept in addition to :data:`ALLOWED_COMMANDS` so the
    intent is explicit and testable: Wi-Fi scans, network (re)configuration,
    privilege escalation and flashing/erasing tools are always refused.
    """
    if not argv:
        return "empty command"
    prog = _prog_name(str(argv[0]))
    args = [str(a).lower() for a in argv[1:]]
    if prog in _ESCALATION_PROGRAMS:
        return "privilege escalation is never used"
    if prog in ("iw", "iwlist") and any(a in ("scan", "scanning") for a in args):
        return "Wi-Fi scanning is never performed"
    if prog == "nmcli" and "wifi" in args:
        # "nmcli dev wifi" lists (and may trigger a scan of) nearby networks.
        return "Wi-Fi scanning is never performed"
    if prog == "netsh" and "wlan" in args and any(
        a.startswith(("networks", "mode=bssid")) or a in ("connect", "disconnect", "set", "add", "delete") for a in args
    ):
        return "Wi-Fi scanning or reconfiguration is never performed"
    if prog == "airport" and any(a == "-s" or a.startswith("--scan") for a in args):
        return "Wi-Fi scanning is never performed"
    if prog == "networksetup" and any(a.startswith("-set") or a.startswith("-remove") for a in args):
        return "network reconfiguration is never performed"
    if prog in _FLASH_TOOLS and any(a in _FLASH_VERBS for a in args):
        return "flashing, erasing, installing or opening a device is never performed by the inspector"
    return None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

REDACTED = "<redacted>"
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
# "key : value" (netsh) and "key value" (iw) lines whose value identifies a
# network or an adapter. The value is replaced; the key stays so the reader
# can see that the field existed.
_KEYED_ID_RE = re.compile(
    r"^(?P<key>[ \t]*(?:ssid|bssid|ap bssid|profile|guid|physical address|ethernet address)[ \t]*(?::[ \t]*|[ \t]+))"
    r"(?P<val>\S.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_USB_SERIAL_RE = re.compile(r"(SER=)\S+", re.IGNORECASE)


def _home_prefix() -> str | None:
    try:
        home = str(Path.home())
    except (RuntimeError, KeyError, OSError):
        return None
    return home if len(home) > 1 else None


def _replace_home(text: str) -> str:
    home = _home_prefix()
    if not home:
        return text
    # Only whole path components: "/root" must not turn "/rootfs" into "~fs".
    return re.sub(re.escape(home) + r"(?=[/\\\s'\"]|$)", "~", text)


def redact_text(text: str) -> str:
    """Remove identifiers from free text (command output, hwid strings)."""
    out = _KEYED_ID_RE.sub(lambda m: m.group("key") + REDACTED, text)
    out = _MAC_RE.sub("<redacted-mac>", out)
    out = _USB_SERIAL_RE.sub(r"\1" + REDACTED, out)
    return _replace_home(out)


def _redact_path(path: str | None, redact: bool) -> str | None:
    if path is None or not redact:
        return path
    return _replace_home(path)


# ---------------------------------------------------------------------------
# Static reference data (recommendations, not detections)
# ---------------------------------------------------------------------------

_DEVKITS = f"espressif/esp-dev-kits@{ESP_DEV_KITS_COMMIT[:7]}"
_ESPCSI = f"espressif/esp-csi@{ESP_CSI_COMMIT[:7]}"
_IDF_WIFI_RST = f"espressif/esp-idf {PINNED_IDF_VERSION} docs/en/api-guides/wifi.rst"

PC_RESEARCH_CSI_PATHS: tuple[dict[str, Any], ...] = (
    {
        "name": "Linux 802.11n CSI Tool",
        "hardware": "Intel Wi-Fi Link 5300 (named by the tool's project documentation; the repository README "
        "describes 'Linux kernel drivers (and driver modifications)' without naming the card)",
        "driver_family": "iwlwifi",
        "requires": "A modified Linux kernel / iwlwifi driver (the repository is a kernel tree with CSI hooks in "
        "drivers/net/wireless/iwlwifi/iwl-connector.c). Installing it is a privileged change.",
        "source": "https://github.com/dhalperi/linux-80211n-csitool (checked at commit ae0db3a, last commit 2014-12-20)",
    },
    {
        "name": "Atheros CSI Tool",
        "hardware": "Atheros 802.11n chipsets supported by ath9k; its README lists AR9580, AR9590, AR9344 and "
        "QCA9558 as validated",
        "driver_family": "ath9k",
        "requires": "Modified ath9k kernel-side components plus a user-space capture app. Installing them is a "
        "privileged change.",
        "source": "https://github.com/xieyaxiongfly/Atheros-CSI-Tool (checked at commit f9bcbd1)",
    },
    {
        "name": "Nexmon CSI",
        "hardware": "Specific Broadcom/Cypress Wi-Fi chips and firmware versions listed in its README: bcm4339 "
        "(Nexus 5), bcm43455c0 (Raspberry Pi 3B+/4B/5), bcm4358 (Nexus 6P), bcm4366c0 (Asus RT-AC86U)",
        "driver_family": "brcmfmac",
        "requires": "Patched Wi-Fi chip firmware built with the nexmon framework, monitor mode and nexutil. "
        "Installing it is a privileged change.",
        "source": "https://github.com/seemoo-lab/nexmon_csi (checked at commit a975a10)",
    },
)

_PC_PATH_STATUS = (
    "Not installed, not supported as an input by RoomSense, and not recommended without your explicit approval: "
    "each needs a specific chip plus modified drivers or firmware (privileged changes)."
)

RECOMMENDED: dict[str, Any] = {
    "status": "RECOMMENDED - not detected on this computer",
    "note": (
        "Recommendations only. Nothing in this section was found on this computer. Verify every item before "
        "buying; no prices are given and RoomSense never orders, flashes or configures hardware by itself."
    ),
    "acquisition_path": {
        "summary": "ESP32 boards running the RoomSense firmware; each receiver is connected to this computer by USB serial.",
        "receiver_firmware": "firmware/esp32/roomsense_csi_rx",
        "transmitter_firmware": "firmware/esp32/roomsense_csi_tx (ESP-NOW mode only; router mode uses your router)",
        "sdk": f"ESP-IDF {PINNED_IDF_VERSION} (commit {PINNED_IDF_COMMIT})",
        "reference_firmware": f"{_ESPCSI} examples/get-started (csi_send, csi_recv, csi_recv_router)",
        "serial": f"{SERIAL_BAUD} baud, protocol roomsense-rscsi-v1 (docs/SERIAL_PROTOCOL.md)",
        "confirmation": "Only the LIVE source confirms a working path: RoomSense receivers print an RSHELLO "
        "line at boot and every 5 s.",
    },
    "boards": [
        {
            "preference": "primary",
            "ordering_code": "ESP32-S3-DevKitC-1U-N8R8",
            "module": "ESP32-S3-WROOM-1U-N8R8",
            "antenna": "ESP32-S3-WROOM-1U 'comes with an external antenna connector'. Connector type: verify in "
            "the ESP32-S3-WROOM-1/1U datasheet before buying an antenna.",
            "usb": "Micro-USB 'USB-to-UART Port' (use this one: flashing and the UART console) plus a separate "
            "ESP32-S3 USB port. Cable: USB 2.0 Standard-A to Micro-B that carries data.",
            "usb_uart_bridge": "Not named in the user guide ('Single USB-to-UART bridge chip provides transfer "
            "rates up to 3 Mbps'); check the board schematic if you need to know.",
            "csi_layout": f"Documented: classic-chip table (esp32/esp32s2/esp32c3/esp32s3) in {_IDF_WIFI_RST}.",
            "why": "CSI layout documented in the pinned ESP-IDF; esp-csi CI builds its get-started examples for "
            "esp32s3; external antenna connector as esp-csi advises.",
            "source": f"{_DEVKITS} docs/en/esp32-s3-devkitc-1/user_guide_v1.1.rst (Ordering Information, "
            "Description of Components)",
        },
        {
            "preference": "acceptable (PCB antenna)",
            "ordering_code": "ESP32-S3-DevKitC-1-N8R8",
            "module": "ESP32-S3-WROOM-1-N8R8",
            "antenna": "On-module PCB antenna. esp-csi advises external antennas because PCB antennas are directional.",
            "usb": "Same as the -1U variant.",
            "csi_layout": "Documented (same chip as above).",
            "why": "Same chip and firmware; expect more sensitivity to board orientation.",
            "source": f"{_DEVKITS} docs/en/esp32-s3-devkitc-1/user_guide_v1.1.rst",
        },
        {
            "preference": "secondary (documented layout rows only)",
            "ordering_code": "ESP32-C5-DevKitC-1 (exact ordering code: verify before buying)",
            "module": "ESP32-C5-WROOM-1(U)",
            "antenna": "The user guide says 'with on-board PCB antenna' for ESP32-C5-WROOM-1(U) and does not say "
            "which variant has an external connector: verify before buying.",
            "usb": "'USB Type-C to UART Port' plus an ESP32-C5 USB Type-C port. Cable: USB-A to USB-C.",
            "csi_layout": "ESP32-C5 table in the pinned ESP-IDF guide. RoomSense accepts only the rows with an "
            "unambiguous index order (106, 114, 234 and 490 values) and rejects the rest.",
            "why": "esp-csi recommends ESP32-C5 for RF quality, but RoomSense's C5 support is partial.",
            "source": f"{_DEVKITS} docs/en/esp32-c5-devkitc-1/user_guide.rst",
        },
        {
            "preference": "not supported by default",
            "ordering_code": "ESP32-C6-DevKitC-1 (exact ordering code: verify before buying)",
            "module": "ESP32-C6-WROOM-1 (PCB antenna) or ESP32-C6-WROOM-1U (external antenna connector)",
            "antenna": "Per the user guide: WROOM-1 uses an on-board PCB antenna, WROOM-1U an external antenna connector.",
            "usb": "'USB Type-C to UART Port' plus an ESP32-C6 USB Type-C port. Cable: USB-A to USB-C.",
            "csi_layout": f"No ESP32-C6 layout table in {_IDF_WIFI_RST}. RoomSense rejects C6 CSI unless the "
            "receiver sets allow_undocumented_layout_assumption, and then flags every frame "
            "UNDOCUMENTED_LAYOUT_ASSUMPTION.",
            "why": "esp-csi recommends ESP32-C6 for RF quality, but its CSI buffer layout is not documented in the "
            "pinned ESP-IDF, so RoomSense cannot interpret it without an assumption.",
            "source": f"{_DEVKITS} docs/en/esp32-c6-devkitc-1/user_guide.rst",
        },
    ],
    "upstream_advice": [
        {
            "quote": "The effect of external IPEX antenna is better than PCB antenna, PCB antenna has directivity.",
            "source": f"{_ESPCSI} README.md, section '5 Note'",
        },
        {
            "quote": "Use an external antenna: The PCB antenna has poor directivity and is easily interfered with "
            "by the motherboard.",
            "source": f"{_ESPCSI} examples/get-started/README.md, section 'hardware'",
        },
        {
            "quote": "The distance between the two devices should be greater than 1 meter.",
            "source": f"{_ESPCSI} examples/get-started/README.md, section 'hardware'",
        },
        {
            "quote": "Use ESP32-C5 / ESP32-C6: ESP32-C5 supports dual-band Wi-Fi communication and is one of the "
            "best RF chips available. ESP32-C6 is the best RF chip among the currently released models.",
            "source": f"{_ESPCSI} examples/get-started/README.md, section 'hardware'",
        },
    ],
    "see_also": ["docs/PARTS_LIST.md", "docs/PLACEMENT.md", "HARDWARE_REPORT.md"],
}

_RESEARCH_DRIVER_HINTS: dict[str, str] = {p["driver_family"]: p["name"] for p in PC_RESEARCH_CSI_PATHS}

# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def _read_text(path: Path, limit: int = MAX_PROC_READ_BYTES) -> str | None:
    try:
        with path.open("rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _readlink_name(path: Path) -> str | None:
    try:
        return Path(os.readlink(path)).name or None
    except OSError:
        return None


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "latin-1", errors="replace")


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _hex4(value: int | None) -> str | None:
    return None if value is None else f"0x{value:04X}"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Serial ports
# ---------------------------------------------------------------------------


def classify_port(port: Any, *, redact: bool = True) -> dict[str, Any]:
    """Describe one pyserial ``ListPortInfo`` (or look-alike) without opening it."""
    device = str(getattr(port, "device", "") or "")
    vid = _int_or_none(getattr(port, "vid", None))
    pid = _int_or_none(getattr(port, "pid", None))
    bridge = USB_BRIDGE_VIDS.get(vid) if vid is not None else None
    if vid is None:
        classification = "non_usb_or_unknown"
        note = "No USB vendor ID: a built-in/legacy UART or a virtual port. Not an ESP32 USB connection."
    elif vid == ESPRESSIF_VID:
        classification = "espressif_usb"
        note = (
            "Espressif's USB vendor ID: an Espressif chip's own USB port is probably attached. Not confirmed; "
            "RoomSense firmware prints on the UART console, so use the board's USB-to-UART port for data."
        )
    elif bridge is not None:
        classification = "usb_uart_bridge"
        note = (
            "A USB-UART bridge used on many ESP32 boards, and also on unrelated devices. Not proof of an ESP32; "
            "unplug and re-run the inspection to check which port is the board."
        )
    else:
        classification = "other_usb_serial"
        note = "USB serial device with a vendor ID not associated with ESP32 boards here."

    serial_number = getattr(port, "serial_number", None)
    hwid = str(getattr(port, "hwid", "") or "")
    description = str(getattr(port, "description", "") or "")
    if redact:
        hwid = redact_text(hwid)
        description = redact_text(description)
        serial_number = REDACTED if serial_number else None

    accessible: bool | None = None
    if os.name == "posix" and device.startswith("/dev/"):
        # os.access only checks permissions; it does not open the device.
        try:
            accessible = os.access(device, os.R_OK | os.W_OK)
        except OSError:
            accessible = None

    def _opt(attr: str) -> str | None:
        v = getattr(port, attr, None)
        return None if v in (None, "") else str(v)

    return {
        "device": device,
        "description": description or None,
        "manufacturer": _opt("manufacturer"),
        "product": _opt("product"),
        "hwid": hwid or None,
        "vid": vid,
        "pid": pid,
        "vid_hex": _hex4(vid),
        "pid_hex": _hex4(pid),
        "serial_number": serial_number if serial_number else None,
        "location": _opt("location"),
        "interface": _opt("interface"),
        "bridge": bridge,
        "classification": classification,
        "esp32_candidate": bridge is not None,
        "accessible_rw": accessible,
        "note": note,
    }


def list_serial_ports_readonly(*, redact: bool = True) -> tuple[list[dict[str, Any]], str | None]:
    """Enumerate serial ports via pyserial ``list_ports`` (never opens them).

    Returns ``(ports, error)``. ``error`` is set when pyserial is missing or
    enumeration failed; the port list is then empty and must not be read as
    "no ports".
    """
    try:
        from serial.tools import list_ports  # local import: pyserial is optional for the CLI
    except ImportError as exc:
        return [], f"pyserial is not installed ({exc}); serial ports could not be listed"
    try:
        raw = list(list_ports.comports())
    except Exception as exc:  # noqa: BLE001 - platform back-ends raise many types
        return [], f"serial port enumeration failed: {type(exc).__name__}: {exc}"
    ports = [classify_port(p, redact=redact) for p in raw[:MAX_PORTS]]
    ports.sort(key=lambda d: d["device"])
    error = None if len(raw) <= MAX_PORTS else f"only the first {MAX_PORTS} of {len(raw)} ports are listed"
    return ports, error


# ---------------------------------------------------------------------------
# Network interfaces
# ---------------------------------------------------------------------------


def _research_hint(driver: str | None) -> str | None:
    if not driver:
        return None
    tool = _RESEARCH_DRIVER_HINTS.get(driver)
    if tool is None:
        return None
    return (
        f"Driver family '{driver}' is the one the research tool '{tool}' modifies. That does not mean this adapter "
        "is supported (the tool needs a specific chip), and nothing is installed or used by RoomSense."
    )


def linux_network_interfaces(base: Path | None = None) -> list[dict[str, Any]]:
    """Read ``/sys/class/net`` (wireless flag, phy, driver). MAC addresses are never read."""
    root = base if base is not None else SYS_CLASS_NET
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for entry in entries[:MAX_INTERFACES]:
        dev = entry / "device"
        driver = _readlink_name(dev / "driver")
        phy = _readlink_name(entry / "phy80211")
        wireless = (entry / "wireless").exists() or phy is not None

        def _id(name: str) -> str | None:
            for candidate in (dev / name, dev / ".." / name):
                txt = _read_text(candidate, 64)
                if txt:
                    return txt.strip() or None
            return None

        vendor = _id("vendor") or _id("idVendor")
        product = _id("device") or _id("idProduct")
        operstate = _read_text(entry / "operstate", 64)
        out.append(
            {
                "name": entry.name,
                "wireless": wireless,
                "phy80211": phy,
                "driver": driver,
                "bus_vendor_id": vendor,
                "bus_device_id": product,
                "operstate": operstate.strip() if operstate else None,
                "research_tool_hint": _research_hint(driver) if wireless else None,
            }
        )
    return out


def _parse_iw_dev(text: str) -> list[dict[str, Any]]:
    """Keep phy, interface name and type from ``iw dev`` output."""
    out: list[dict[str, Any]] = []
    phy: str | None = None
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("phy#"):
            phy = s
        elif s.startswith("Interface "):
            current = {"interface": s.split(None, 1)[1], "phy": phy, "type": None}
            out.append(current)
        elif s.startswith("type ") and current is not None:
            current["type"] = s.split(None, 1)[1]
    return out


def _parse_networksetup(text: str) -> list[dict[str, Any]]:
    """Keep hardware port and device name from ``networksetup -listallhardwareports``."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
            current = {"hardware_port": port, "device": None, "wireless": port.lower() in ("wi-fi", "airport")}
            out.append(current)
        elif line.startswith("Device:") and current is not None:
            current["device"] = line.split(":", 1)[1].strip()
    return out


_NETSH_KEEP = {"name": "name", "description": "description", "state": "state", "radio type": "radio_type"}


def _parse_netsh_interfaces(text: str) -> list[dict[str, Any]]:
    """Keep only non-identifying keys from ``netsh wlan show interfaces`` (English output)."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        k = key.strip().lower()
        if k == "name":
            current = {"name": value.strip(), "wireless": True}
            out.append(current)
        elif current is not None and k in _NETSH_KEEP:
            current[_NETSH_KEEP[k]] = value.strip()
    return out


# ---------------------------------------------------------------------------
# The inspector
# ---------------------------------------------------------------------------


class _Inspector:
    """One inspection run. Holds the command log; not shared between calls."""

    def __init__(self, *, redact: bool, system: str) -> None:
        self.redact = redact
        self.system = system
        self.commands: list[dict[str, Any]] = []
        self.errors: list[str] = []

    # -- external commands -------------------------------------------------

    def run(self, argv: Sequence[str], timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        logical = tuple(str(a) for a in argv)
        entry: dict[str, Any] = {
            "argv": list(logical),
            "found": False,
            "returncode": None,
            "timed_out": False,
            "duration_s": None,
            "refused": None,
            "error": None,
            "stdout": "",
            "stderr": "",
        }
        reason = forbidden_reason(logical)
        if reason is None and logical not in ALLOWED_COMMANDS:
            reason = "not in the inspector's read-only allow-list"
        if reason is not None:
            entry["refused"] = reason
            self.commands.append(entry)
            return entry
        exe = shutil.which(logical[0])
        if exe is None:
            self.commands.append(entry)
            return entry
        entry["found"] = True
        env = dict(os.environ)
        if os.name == "posix":
            env["LC_ALL"] = "C"  # stable, parseable English output
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [exe, *logical[1:]],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            entry["timed_out"] = True
        except OSError as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        else:
            entry["returncode"] = proc.returncode
            entry["stdout"] = self._clean(_decode(proc.stdout))
            entry["stderr"] = self._clean(_decode(proc.stderr))
        entry["duration_s"] = round(time.monotonic() - t0, 3)
        self.commands.append(entry)
        return entry

    def _clean(self, text: str) -> str:
        text = text[:MAX_OUTPUT_CHARS] + ("\n[output truncated]" if len(text) > MAX_OUTPUT_CHARS else "")
        return redact_text(text) if self.redact else text

    # -- sections ------------------------------------------------------------

    def host(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "system": platform.system() or None,
            "release": platform.release() or None,
            "version": platform.version() or None,
            "machine": platform.machine() or None,
            "platform": platform.platform() or None,
            "os_pretty_name": None,
            "cpu_model": None,
            "cpu_count": os.cpu_count(),
            "ram_total_bytes": None,
        }
        system = self.system
        if system == "Linux":
            try:
                info["os_pretty_name"] = platform.freedesktop_os_release().get("PRETTY_NAME")
            except OSError:
                pass
            cpuinfo = _read_text(PROC_CPUINFO) or ""
            for key in ("model name", "Hardware", "Model", "cpu model", "Processor"):
                m = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
                if m:
                    info["cpu_model"] = m.group(1).strip()
                    break
            m = re.search(r"^MemTotal:\s*(\d+)\s*kB", _read_text(PROC_MEMINFO) or "", re.MULTILINE)
            if m:
                info["ram_total_bytes"] = int(m.group(1)) * 1024
        elif system == "Darwin":
            info["os_pretty_name"] = ("macOS " + platform.mac_ver()[0]).strip() if platform.mac_ver()[0] else None
            r = self.run(("sysctl", "-n", "machdep.cpu.brand_string"))
            if r["returncode"] == 0 and r["stdout"].strip():
                info["cpu_model"] = r["stdout"].strip()
            r = self.run(("sysctl", "-n", "hw.memsize"))
            if r["returncode"] == 0 and r["stdout"].strip().isdigit():
                info["ram_total_bytes"] = int(r["stdout"].strip())
        elif system == "Windows":
            rel, ver = platform.win32_ver()[:2]
            info["os_pretty_name"] = f"Windows {rel} ({ver})" if rel else None
            info["cpu_model"] = os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor() or None
            info["ram_total_bytes"] = _windows_total_ram()
        return info

    def software(self) -> dict[str, Any]:
        py = {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": _redact_path(sys.executable, self.redact) or None,
        }
        try:
            import serial  # noqa: PLC0415 - optional dependency

            pyserial: str | None = str(getattr(serial, "__version__", "unknown"))
        except ImportError:
            pyserial = None
        node_r = self.run(("node", "--version"))
        node_version = node_r["stdout"].strip() if node_r["returncode"] == 0 else ""
        node = {"on_path": node_r["found"], "version": node_version or None}
        return {"python": py, "pyserial_version": pyserial, "node": node, "esp_idf": self.esp_idf()}

    def esp_idf(self) -> dict[str, Any]:
        idf_path = os.environ.get("IDF_PATH") or None
        exists = Path(idf_path).is_dir() if idf_path else None
        r = self.run(("idf.py", "--version"), timeout_s=IDF_TIMEOUT_S)
        output = (r["stdout"] or r["stderr"]).strip() or None
        version = None
        if r["returncode"] == 0 and output:
            m = re.search(r"\bv\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.\-]+)?", output)
            version = m.group(0) if m else None
        return {
            "idf_path_set": idf_path is not None,
            "idf_path": _redact_path(idf_path, self.redact),
            "idf_path_exists": exists,
            "idf_py_on_path": r["found"],
            "idf_py_timed_out": r["timed_out"],
            "idf_py_version_output": output,
            "version": version,
            "pinned_version": PINNED_IDF_VERSION,
            "matches_pinned": None if version is None else version == PINNED_IDF_VERSION,
            "note": "Detected only; RoomSense never installs or updates ESP-IDF.",
        }

    def virtualisation(self) -> dict[str, Any]:
        system = self.system
        info: dict[str, Any] = {
            "dockerenv_file": None,
            "containerenv_file": None,
            "cgroup_hints": [],
            "cpu_hypervisor_flag": None,
            "systemd_detect_virt_vm": None,
            "systemd_detect_virt_container": None,
            "macos_hv_vmm_present": None,
            "usb_bus_visible": None,
        }
        if system == "Linux":
            info["dockerenv_file"] = Path("/.dockerenv").exists()
            info["containerenv_file"] = Path("/run/.containerenv").exists()
            cg = (_read_text(PROC_1_CGROUP, 16384) or "").lower()
            info["cgroup_hints"] = sorted(
                {k for k in ("docker", "kubepods", "containerd", "lxc", "libpod", "podman", "garden") if k in cg}
            )
            cpuinfo = _read_text(PROC_CPUINFO)
            if cpuinfo is not None:
                info["cpu_hypervisor_flag"] = bool(re.search(r"^flags\s*:.*\bhypervisor\b", cpuinfo, re.MULTILINE))
            for flag, key in (("--vm", "systemd_detect_virt_vm"), ("--container", "systemd_detect_virt_container")):
                r = self.run(("systemd-detect-virt", flag))
                if r["found"] and not r["timed_out"] and r["returncode"] is not None:
                    value = r["stdout"].strip()
                    info[key] = value if value and value != "none" else "none"
            info["usb_bus_visible"] = SYS_BUS_USB.exists()
        elif system == "Darwin":
            r = self.run(("sysctl", "-n", "kern.hv_vmm_present"))
            if r["returncode"] == 0 and r["stdout"].strip() in ("0", "1"):
                info["macos_hv_vmm_present"] = r["stdout"].strip() == "1"

        sdv_c = info["systemd_detect_virt_container"]
        sdv_v = info["systemd_detect_virt_vm"]
        container = bool(
            info["dockerenv_file"] or info["containerenv_file"] or info["cgroup_hints"] or (sdv_c not in (None, "none"))
        )
        vm = bool(info["cpu_hypervisor_flag"] or (sdv_v not in (None, "none")) or info["macos_hv_vmm_present"])
        info["likely_container"] = container if system in ("Linux",) else None
        info["likely_virtual_machine"] = vm if system in ("Linux", "Darwin") else None
        parts: list[str] = []
        if container:
            parts.append(f"container ({sdv_c})" if sdv_c not in (None, "none") else "container")
        if vm:
            parts.append(f"virtual machine ({sdv_v})" if sdv_v not in (None, "none") else "virtual machine")
        summary = " inside a ".join(parts) if parts else "no virtualisation hints found"
        if info["usb_bus_visible"] is False:
            summary += "; no USB bus is visible, so USB boards cannot be attached to this environment"
        info["summary"] = summary[0].upper() + summary[1:]
        return info

    def network(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (interfaces, wifi_listing_outputs)."""
        system = self.system
        listings: list[dict[str, Any]] = []
        interfaces: list[dict[str, Any]] = []
        if system == "Linux":
            interfaces = linux_network_interfaces()
            r = self.run(("iw", "dev"))
            if r["found"]:
                listings.append(
                    {"command": "iw dev", "returncode": r["returncode"], "timed_out": r["timed_out"],
                     "output": r["stdout"] or r["stderr"], "parsed": _parse_iw_dev(r["stdout"])}
                )
        elif system == "Darwin":
            r = self.run(("networksetup", "-listallhardwareports"))
            if r["found"]:
                parsed = _parse_networksetup(r["stdout"])
                interfaces = [
                    {"name": p["device"], "hardware_port": p["hardware_port"], "wireless": p["wireless"]} for p in parsed
                ]
                listings.append(
                    {"command": "networksetup -listallhardwareports", "returncode": r["returncode"],
                     "timed_out": r["timed_out"], "output": r["stdout"] or r["stderr"], "parsed": parsed}
                )
        elif system == "Windows":
            r = self.run(("netsh", "wlan", "show", "interfaces"))
            if r["found"]:
                parsed = _parse_netsh_interfaces(r["stdout"])
                interfaces = parsed
                listings.append(
                    {"command": "netsh wlan show interfaces", "returncode": r["returncode"],
                     "timed_out": r["timed_out"], "output": r["stdout"] or r["stderr"], "parsed": parsed}
                )
        return interfaces, listings

    def serial_permissions(self) -> dict[str, Any]:
        """Linux group membership that governs /dev/tty* access. Read-only."""
        if self.system != "Linux" or os.name != "posix":
            return {"applicable": False}
        try:
            import grp  # noqa: PLC0415 - POSIX only

            names = set()
            for gid in os.getgroups():
                try:
                    names.add(grp.getgrgid(gid).gr_name)
                except KeyError:
                    continue
            try:
                primary = grp.getgrgid(os.getgid()).gr_name
                names.add(primary)
            except KeyError:
                pass
        except (ImportError, OSError):
            return {"applicable": True, "in_dialout_group": None, "in_uucp_group": None, "running_as_root": None}
        return {
            "applicable": True,
            "in_dialout_group": "dialout" in names,
            "in_uucp_group": "uucp" in names,
            "running_as_root": os.geteuid() == 0 if hasattr(os, "geteuid") else None,
            "note": "On most Linux distributions serial access needs the 'dialout' group ('uucp' on Arch). "
            "Joining it is a privileged change that only you can decide to make.",
        }


def _windows_total_ram() -> int | None:
    try:
        import ctypes  # noqa: PLC0415 - Windows only

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MemStatus()
        stat.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return int(stat.ullTotalPhys)
    except (ImportError, AttributeError, OSError):
        pass
    return None


def inspect_host(*, redact: bool = True) -> dict[str, Any]:
    """Inspect the computer this runs on. Non-destructive; see the module docstring.

    ``redact=False`` keeps MAC addresses, SSIDs, adapter GUIDs, USB serial
    numbers and the home directory in the output. Keep such a report private.
    """
    ins = _Inspector(redact=redact, system=platform.system())
    host = ins.host()
    virt = ins.virtualisation()
    ports, port_error = list_serial_ports_readonly(redact=redact)
    if port_error:
        ins.errors.append(port_error)
    interfaces, listings = ins.network()
    software = ins.software()
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "generated_at_utc": _utc_now_iso(),
        "generator": "roomsense.hardware.inspect_host",
        "scope": (
            "Describes only the computer this inspection ran on. Serial ports were listed, never opened; no Wi-Fi "
            "scan was performed; nothing was installed, flashed or reconfigured."
        ),
        "identifiers_redacted": redact,
        "host": host,
        "virtualisation": virt,
        "detected": {
            "serial_ports": ports,
            "serial_port_error": port_error,
            "serial_permissions": ins.serial_permissions(),
            "network_interfaces": interfaces,
            "wifi_interface_listings": listings,
        },
        "software": software,
        "pc_csi_research_paths": [dict(p, status=_PC_PATH_STATUS) for p in PC_RESEARCH_CSI_PATHS],
        "recommended": copy.deepcopy(RECOMMENDED),
        "commands_run": ins.commands,
        "errors": ins.errors,
    }
    report["assessment"] = assess(report)
    return report


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------

_NOT_EVIDENCE = (
    "A built-in Wi-Fi adapter or working network connectivity is NOT evidence of CSI access: ordinary PC Wi-Fi "
    "drivers do not expose CSI. RoomSense's documented acquisition path is ESP32 boards over USB serial."
)


def _get(d: Mapping[str, Any] | None, *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def assess(report: Mapping[str, Any]) -> dict[str, Any]:
    """Decide whether the documented CSI acquisition path is possibly present.

    ``csi_path_available`` is True only when a USB serial device with a
    vendor ID used by ESP32 boards is attached. Even then the path is not
    *confirmed*: the inspector never opens ports, and only RSHELLO lines seen
    by the LIVE source prove that RoomSense firmware is talking.
    """
    ports = list(_get(report, "detected", "serial_ports") or [])
    port_error = _get(report, "detected", "serial_port_error")
    interfaces = list(_get(report, "detected", "network_interfaces") or [])
    idf = _get(report, "software", "esp_idf") or {}
    virt = _get(report, "virtualisation") or {}
    perms = _get(report, "detected", "serial_permissions") or {}
    system = _get(report, "host", "system")

    candidates = [p for p in ports if isinstance(p, Mapping) and p.get("esp32_candidate")]
    espressif = [p for p in candidates if p.get("vid") == ESPRESSIF_VID]
    available = bool(candidates)
    confidence = "none" if not candidates else ("likely" if espressif else "possible")

    reasons: list[str] = []
    next_steps: list[str] = []
    if port_error:
        reasons.append(f"Serial ports could not be listed completely: {port_error}.")
    if candidates:
        devs = ", ".join(f"{p.get('device')} ({p.get('bridge')})" for p in candidates)
        reasons.append(f"USB serial device(s) with a vendor ID used on ESP32 boards: {devs}.")
        reasons.append(
            "Not confirmed: ports are never opened by the inspector. Only RSHELLO lines received by the LIVE "
            "source confirm that RoomSense firmware is running (docs/SERIAL_PROTOCOL.md)."
        )
    else:
        other = [p.get("device") for p in ports if isinstance(p, Mapping)]
        if other:
            reasons.append(
                "Serial ports were found but none has a USB vendor ID used on ESP32 boards: " + ", ".join(map(str, other)) + "."
            )
        else:
            reasons.append("No serial ports were found.")
        reasons.append("No ESP32 receiver was detected, so no documented CSI acquisition path exists on this computer.")
    reasons.append(_NOT_EVIDENCE)

    wireless = [i for i in interfaces if isinstance(i, Mapping) and i.get("wireless")]
    if wireless:
        names = ", ".join(str(i.get("name")) for i in wireless)
        reasons.append(f"Wireless interface(s) present ({names}); RoomSense does not use them for CSI.")
        for i in wireless:
            if i.get("research_tool_hint"):
                reasons.append(f"{i.get('name')}: {i['research_tool_hint']}")
    else:
        reasons.append("No wireless network interface was found (this does not matter for the ESP32 path).")

    if virt.get("usb_bus_visible") is False or virt.get("likely_container"):
        reasons.append(f"Environment: {virt.get('summary')}.")

    if not idf.get("idf_py_on_path") and not idf.get("idf_path_set"):
        reasons.append("ESP-IDF not found (IDF_PATH unset, idf.py not on PATH); firmware cannot be built here.")
    elif not idf.get("idf_py_on_path"):
        reasons.append(
            "IDF_PATH is set but idf.py is not on PATH; ESP-IDF's export script has not been run in this shell."
        )
    elif idf.get("version") and not idf.get("matches_pinned"):
        reasons.append(f"ESP-IDF {idf.get('version')} found; RoomSense firmware is pinned to {PINNED_IDF_VERSION}.")

    if not candidates:
        next_steps.append("Get the boards listed in docs/PARTS_LIST.md (verify before buying; RoomSense never buys anything).")
        next_steps.append(
            "Connect a receiver with a data-capable USB cable to its USB-to-UART port, then re-run: " + REGENERATE_COMMAND
        )
    else:
        next_steps.append("Unplug the board and re-run the inspection to confirm which port belongs to it.")
    if not idf.get("matches_pinned"):
        next_steps.append(
            f"Install (or activate) ESP-IDF {PINNED_IDF_VERSION} yourself following Espressif's guide; RoomSense "
            "does not install it."
        )
    next_steps.append(
        "With your explicit approval only, build and flash firmware/esp32 (receiver, plus transmitter for ESP-NOW "
        "mode). RoomSense never flashes a device by itself."
    )
    if perms.get("running_as_root"):
        reasons.append("The inspector ran as root. RoomSense does not need root; run it as a normal user.")
    elif system == "Linux" and perms.get("applicable") and perms.get("in_dialout_group") is False:
        next_steps.append(
            "If a port is not readable, joining the 'dialout' group ('uucp' on Arch) may be needed: "
            "sudo usermod -a -G dialout $USER, then log in again. This is a privileged change for you to approve."
        )
    next_steps.append(
        "Enter the port name explicitly in configs/roomsense.toml (ports are never auto-guessed), start the LIVE "
        "source and check that RSHELLO arrives."
    )
    next_steps.append("Mount the boards as described in docs/PLACEMENT.md, then calibrate (CALIBRATION.md).")

    return {
        "csi_path_available": available,
        "csi_path_confirmed": False,
        "hardware_required": not available,
        "confidence": confidence,
        "candidate_ports": [str(p.get("device")) for p in candidates],
        "reasons": reasons,
        "next_steps": next_steps,
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|") if text else "(empty)"


def _fence(text: str) -> str:
    body = text if text.strip() else "(no output)"
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}text\n{body.rstrip()}\n{fence}"


def _fmt_bytes(n: Any) -> str:
    if not isinstance(n, int) or isinstance(n, bool):
        return "unavailable"
    return f"{n / (1024 ** 3):.1f} GiB ({n} bytes)"


def render_markdown(report: Mapping[str, Any]) -> str:
    """Human-readable Markdown for a report produced by :func:`inspect_host`."""
    a = report.get("assessment") or assess(report)
    host = report.get("host") or {}
    virt = report.get("virtualisation") or {}
    det = report.get("detected") or {}
    sw = report.get("software") or {}
    lines: list[str] = []
    add = lines.append

    add("# RoomSense hardware inspection")
    add("")
    add(f"> Generated {report.get('generated_at_utc')} by `{report.get('generator')}` ({report.get('format')}).")
    add(f"> {report.get('scope')}")
    add(f"> Identifiers redacted: {_cell(report.get('identifiers_redacted'))}.")
    add("")
    if a.get("hardware_required"):
        add("> **HARDWARE REQUIRED** - no ESP32 receiver was detected on this computer. Live CSI acquisition is")
        add("> not possible here; only recorded replay and clearly labelled SIMULATION are available.")
        add("")

    add("## Assessment")
    add("")
    avail = "POSSIBLE (not confirmed)" if a.get("csi_path_available") else "NO"
    add(f"* Documented CSI acquisition path present: **{avail}** (confidence: {a.get('confidence')})")
    add(f"* Confirmed by firmware (RSHELLO): **{'YES' if a.get('csi_path_confirmed') else 'NO'}**")
    add(f"* Hardware required: **{'YES' if a.get('hardware_required') else 'NO'}**")
    add("")
    add("Reasons:")
    add("")
    for r in a.get("reasons", []):
        add(f"* {r}")
    add("")
    add("Next steps:")
    add("")
    for i, s in enumerate(a.get("next_steps", []), 1):
        add(f"{i}. {s}")
    add("")

    add("## Host")
    add("")
    add("| Item | Value |")
    add("|---|---|")
    for label, value in (
        ("OS", host.get("os_pretty_name")),
        ("System / release", f"{host.get('system')} {host.get('release')}"),
        ("Machine", host.get("machine")),
        ("Platform", host.get("platform")),
        ("CPU model", host.get("cpu_model")),
        ("Logical CPUs", host.get("cpu_count")),
        ("RAM", _fmt_bytes(host.get("ram_total_bytes"))),
        ("Python", _get(sw, "python", "version")),
        ("pyserial", sw.get("pyserial_version")),
        ("Node.js", _get(sw, "node", "version") if _get(sw, "node", "on_path") else "not on PATH"),
    ):
        add(f"| {label} | {_cell(value)} |")
    add("")

    add("## Virtualisation and container hints")
    add("")
    add(f"Summary: {virt.get('summary', 'unavailable')}.")
    add("")
    add("| Hint | Value |")
    add("|---|---|")
    for key in (
        "dockerenv_file",
        "containerenv_file",
        "cgroup_hints",
        "cpu_hypervisor_flag",
        "systemd_detect_virt_vm",
        "systemd_detect_virt_container",
        "macos_hv_vmm_present",
        "usb_bus_visible",
    ):
        v = virt.get(key)
        add(f"| {key} | {_cell(', '.join(v) if isinstance(v, list) else v) if v != [] else 'none'} |")
    add("")

    add("## Serial ports (listed with pyserial; never opened)")
    add("")
    ports = det.get("serial_ports") or []
    if det.get("serial_port_error"):
        add(f"Enumeration problem: {det['serial_port_error']}")
        add("")
    if ports:
        add("| Device | VID:PID | Classification | Bridge | ESP32 candidate | Read/write access | Description |")
        add("|---|---|---|---|---|---|---|")
        for p in ports:
            vp = f"{p.get('vid_hex')}:{p.get('pid_hex')}" if p.get("vid") is not None else "none"
            add(
                f"| {_cell(p.get('device'))} | {vp} | {_cell(p.get('classification'))} | {_cell(p.get('bridge'))} | "
                f"{_cell(p.get('esp32_candidate'))} | {_cell(p.get('accessible_rw'))} | {_cell(p.get('description'))} |"
            )
        add("")
        for p in ports:
            add(f"* `{p.get('device')}`: {p.get('note')}")
        add("")
    else:
        add("No serial ports were listed.")
        add("")
    perms = det.get("serial_permissions") or {}
    if perms.get("applicable"):
        add(
            f"Serial permissions: in 'dialout' group: {_cell(perms.get('in_dialout_group'))}; "
            f"in 'uucp' group: {_cell(perms.get('in_uucp_group'))}; running as root: {_cell(perms.get('running_as_root'))}."
        )
        add("")

    add("## Network interfaces")
    add("")
    ifaces = det.get("network_interfaces") or []
    if ifaces:
        add("| Name | Wireless | Driver | phy | Bus vendor:device | State |")
        add("|---|---|---|---|---|---|")
        for i in ifaces:
            bus = f"{i.get('bus_vendor_id')}:{i.get('bus_device_id')}" if i.get("bus_vendor_id") else None
            add(
                f"| {_cell(i.get('name'))} | {_cell(i.get('wireless'))} | {_cell(i.get('driver'))} | "
                f"{_cell(i.get('phy80211'))} | {_cell(bus)} | {_cell(i.get('operstate') or i.get('state'))} |"
            )
        add("")
        for i in ifaces:
            if i.get("research_tool_hint"):
                add(f"* `{i.get('name')}`: {i['research_tool_hint']}")
        add("")
    else:
        add("No network interfaces were listed.")
        add("")
    add(_NOT_EVIDENCE)
    add("")
    listings = det.get("wifi_interface_listings") or []
    if listings:
        add("### Wi-Fi interface listings (read-only; never a scan)")
        add("")
        for li in listings:
            add(f"`{li.get('command')}` (exit code {_cell(li.get('returncode'))}):")
            add("")
            add(_fence(str(li.get("output") or "")))
            add("")

    add("## ESP-IDF")
    add("")
    idf = sw.get("esp_idf") or {}
    add(f"* IDF_PATH set: {_cell(idf.get('idf_path_set'))} ({_cell(idf.get('idf_path'))})")
    add(f"* idf.py on PATH: {_cell(idf.get('idf_py_on_path'))}")
    add(f"* Version: {_cell(idf.get('version'))}; pinned: {PINNED_IDF_VERSION}; matches: {_cell(idf.get('matches_pinned'))}")
    add(f"* {idf.get('note', '')}")
    add("")

    add("## Research CSI paths for PCs (not installed, not used)")
    add("")
    for p in report.get("pc_csi_research_paths") or []:
        add(f"* **{p.get('name')}** - hardware: {p.get('hardware')}. Requires: {p.get('requires')} Source: {p.get('source')}.")
    add("")
    add(_PC_PATH_STATUS)
    add("")

    rec = report.get("recommended") or {}
    add("## Recommended hardware (NOT detected - recommendations only)")
    add("")
    add(str(rec.get("note", "")))
    add("")
    path = rec.get("acquisition_path") or {}
    for key in ("summary", "receiver_firmware", "transmitter_firmware", "sdk", "reference_firmware", "serial", "confirmation"):
        if path.get(key):
            add(f"* {key.replace('_', ' ')}: {path[key]}")
    add("")
    add("| Preference | Ordering code | Module | Antenna | CSI layout | Source |")
    add("|---|---|---|---|---|---|")
    for b in rec.get("boards") or []:
        add(
            f"| {_cell(b.get('preference'))} | {_cell(b.get('ordering_code'))} | {_cell(b.get('module'))} | "
            f"{_cell(b.get('antenna'))} | {_cell(b.get('csi_layout'))} | {_cell(b.get('source'))} |"
        )
    add("")
    for q in rec.get("upstream_advice") or []:
        add(f"> \"{q.get('quote')}\" ({q.get('source')})")
        add("")

    add("## Commands run")
    add("")
    cmds = report.get("commands_run") or []
    if cmds:
        add("| Command | Found | Exit code | Timed out | Seconds | Refused |")
        add("|---|---|---|---|---|---|")
        for c in cmds:
            add(
                f"| `{' '.join(c.get('argv', []))}` | {_cell(c.get('found'))} | {_cell(c.get('returncode'))} | "
                f"{_cell(c.get('timed_out'))} | {_cell(c.get('duration_s'))} | {_cell(c.get('refused')) if c.get('refused') else 'no'} |"
            )
    else:
        add("No external commands were run.")
    add("")
    errors = report.get("errors") or []
    if errors:
        add("## Errors")
        add("")
        for e in errors:
            add(f"* {e}")
        add("")
    add(f"Regenerate on your own computer: `{REGENERATE_COMMAND}`")
    add("")
    return "\n".join(lines)
