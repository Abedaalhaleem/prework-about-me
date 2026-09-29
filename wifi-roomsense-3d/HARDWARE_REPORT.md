# Hardware report

> **HARDWARE REQUIRED.** No ESP32 receiver was detected where this report was
> generated, so no live CSI acquisition path exists there. See section 7.

## 1. Where this report was generated

**This report describes the cloud build container, not your computer.** The
container is an ephemeral Linux environment (Ubuntu 24.04.4 LTS, 4 vCPU Intel
Xeon under KVM, Python 3.11.15, Node v22.22.2). It has no USB bus, no ESP32
board and no Wi-Fi adapter. Nothing below says anything about the hardware
attached to your own machine.

To get a report for **your** computer, run this from the repository root:

```bash
cd backend && uv run python ../scripts/inspect_hardware.py --write ../HARDWARE_REPORT.local.md
```

The inspection is read-only:

* Serial ports are listed with pyserial and **never opened**. Opening a port
  can reset an ESP32 board.
* It **never scans for Wi-Fi networks** and never runs anything privileged.
* It never installs, flashes, erases or reconfigures anything.
* It runs external commands only from a fixed allow-list (`ALLOWED_COMMANDS`
  in `backend/roomsense/hardware.py`), each with a short timeout.
* MAC addresses, SSIDs/BSSIDs, adapter GUIDs, USB serial numbers and your home
  directory are redacted unless you pass `--include-identifiers`. The hostname
  and user name are never collected.
* The report file stays on your computer. If your checkout's `.gitignore`
  does not list `HARDWARE_REPORT.local.md`, take care not to commit it by
  accident.

Other options: `--json` (machine-readable output), and no `--write` to print
the report to the terminal. The running app serves the same inspection at
`GET /api/hardware` (Hardware page).

## 2. Detected devices vs devices merely recommended

### 2.1 Detected in the build container (exact output)

This is the exact output of `inspect_host()` from one run of
`uv run python ../scripts/inspect_hardware.py --json` in the build container
at 2026-09-29T02:51:40Z (identifiers redacted). The keys below are copied
unchanged. Left out are `scope`, `pc_csi_research_paths` and `recommended`
(static text, see sections 3 and 5), `commands_run` (summarised below), and
the assessment's `reasons` and `next_steps` (prose in the Markdown report).

```json
{
  "generated_at_utc": "2026-09-29T02:51:40Z",
  "identifiers_redacted": true,
  "host": {
    "system": "Linux",
    "release": "6.18.44-fc-v37",
    "version": "#1 SMP PREEMPT_DYNAMIC @0",
    "machine": "x86_64",
    "platform": "Linux-6.18.44-fc-v37-x86_64",
    "os_pretty_name": "Ubuntu 24.04.4 LTS",
    "cpu_model": "Intel(R) Xeon(R) Processor @ 2.10GHz",
    "cpu_count": 4,
    "ram_total_bytes": 16877547520
  },
  "virtualisation": {
    "dockerenv_file": false,
    "containerenv_file": false,
    "cgroup_hints": [],
    "cpu_hypervisor_flag": true,
    "systemd_detect_virt_vm": "kvm",
    "systemd_detect_virt_container": "docker",
    "macos_hv_vmm_present": null,
    "usb_bus_visible": false,
    "likely_container": true,
    "likely_virtual_machine": true,
    "summary": "Container (docker) inside a virtual machine (kvm); no USB bus is visible, so USB boards cannot be attached to this environment"
  },
  "detected": {
    "serial_ports": [
      {
        "device": "/dev/ttyS0",
        "description": "n/a",
        "manufacturer": null,
        "product": null,
        "hwid": "n/a",
        "vid": null,
        "pid": null,
        "vid_hex": null,
        "pid_hex": null,
        "serial_number": null,
        "location": null,
        "interface": null,
        "bridge": null,
        "classification": "non_usb_or_unknown",
        "esp32_candidate": false,
        "accessible_rw": true,
        "note": "No USB vendor ID: a built-in/legacy UART or a virtual port. Not an ESP32 USB connection."
      }
    ],
    "serial_port_error": null,
    "serial_permissions": {
      "applicable": true,
      "in_dialout_group": false,
      "in_uucp_group": false,
      "running_as_root": true,
      "note": "On most Linux distributions serial access needs the 'dialout' group ('uucp' on Arch). Joining it is a privileged change that only you can decide to make."
    },
    "network_interfaces": [
      {
        "name": "eth0",
        "wireless": false,
        "phy80211": null,
        "driver": "virtio_net",
        "bus_vendor_id": "0x1af4",
        "bus_device_id": "0x0001",
        "operstate": "up",
        "research_tool_hint": null
      },
      {
        "name": "ifb0",
        "wireless": false,
        "phy80211": null,
        "driver": null,
        "bus_vendor_id": null,
        "bus_device_id": null,
        "operstate": "down",
        "research_tool_hint": null
      },
      {
        "name": "ifb1",
        "wireless": false,
        "phy80211": null,
        "driver": null,
        "bus_vendor_id": null,
        "bus_device_id": null,
        "operstate": "down",
        "research_tool_hint": null
      },
      {
        "name": "lo",
        "wireless": false,
        "phy80211": null,
        "driver": null,
        "bus_vendor_id": null,
        "bus_device_id": null,
        "operstate": "unknown",
        "research_tool_hint": null
      }
    ],
    "wifi_interface_listings": []
  },
  "software": {
    "python": {
      "version": "3.11.15",
      "implementation": "CPython",
      "executable": "/home/user/prework-about-me/wifi-roomsense-3d/backend/.venv/bin/python3"
    },
    "pyserial_version": "3.5",
    "node": {
      "on_path": true,
      "version": "v22.22.2"
    },
    "esp_idf": {
      "idf_path_set": false,
      "idf_path": null,
      "idf_path_exists": null,
      "idf_py_on_path": false,
      "idf_py_timed_out": false,
      "idf_py_version_output": null,
      "version": null,
      "pinned_version": "v5.5.5",
      "matches_pinned": null,
      "note": "Detected only; RoomSense never installs or updates ESP-IDF."
    }
  },
  "assessment": {
    "csi_path_available": false,
    "csi_path_confirmed": false,
    "hardware_required": true,
    "confidence": "none",
    "candidate_ports": []
  }
}
```

Commands the inspector ran: `systemd-detect-virt --vm` (exit 0), `systemd-detect-virt --container` (exit 0), `node --version` (exit 0).
`iw dev` and `idf.py --version` were not on PATH and were not run.

What this means:

* `/dev/ttyS0` is a legacy or virtual UART with no USB vendor ID. It is **not**
  an ESP32 connection.
* No interface is wireless (`eth0` is a virtio network device), and no USB bus
  is visible. **No ESP32 board is present, and none could be attached here.**
* ESP-IDF is not installed here, so the firmware was **not compiled** here (see
  section 8).

### 2.2 Recommended only (NOT detected anywhere)

The following are **recommendations**. None of them was found in the build
container, and this project has not tested any of them: the ESP32-S3 boards,
antennas, cables and power supplies in section 5 and `docs/PARTS_LIST.md`.

## 3. Documented CSI acquisition path

RoomSense documents one acquisition path:

* **ESP32 boards running the RoomSense firmware** (`firmware/esp32`), with each
  receiver streaming `roomsense-rscsi-v1` lines (`docs/SERIAL_PROTOCOL.md`) over
  USB serial to the computer running RoomSense.
* The firmware is derived from espressif/esp-csi `examples/get-started`
  (`csi_send`, `csi_recv`, `csi_recv_router`) at commit
  `8633d67152db2808f141cc1595970aa9cf406045`.
* It is built with **ESP-IDF v5.5.5** (tag commit
  `b774170ff46c393eeb5e495ea37936038d3f4f4f`).
* The upstream esp-csi get-started output formats are also parsed, for
  comparison, if you declare the chip and LTF configuration yourself.

**Does this path exist in the build container? No.** There is no USB bus, no
ESP32 and no ESP-IDF there (section 2.1).

A built-in Wi-Fi adapter or working network connectivity is **not** evidence of
CSI access, because ordinary PC Wi-Fi drivers do not expose CSI. The known
research paths for PCs all need specific hardware plus modified drivers or
firmware:

| Research tool | Hardware | What it needs | Source checked |
|---|---|---|---|
| Linux 802.11n CSI Tool | Intel Wi-Fi Link 5300 (named by the tool's project documentation; the repository README describes "Linux kernel drivers (and driver modifications)") | A modified Linux kernel / iwlwifi driver (CSI hooks in `drivers/net/wireless/iwlwifi/iwl-connector.c`) | github.com/dhalperi/linux-80211n-csitool @ `ae0db3a` |
| Atheros CSI Tool | ath9k-supported Atheros 802.11n chips; README lists AR9580, AR9590, AR9344, QCA9558 as validated | Modified ath9k kernel-side components plus a user-space app | github.com/xieyaxiongfly/Atheros-CSI-Tool @ `f9bcbd1` |
| Nexmon CSI | bcm4339 (Nexus 5), bcm43455c0 (Raspberry Pi 3B+/4B/5), bcm4358 (Nexus 6P), bcm4366c0 (Asus RT-AC86U), each at a specific firmware version | Patched Wi-Fi chip firmware, monitor mode, nexutil | github.com/seemoo-lab/nexmon_csi @ `a975a10` |

All three are **privileged changes**. RoomSense does not install them or accept
their output, and does not recommend them without your explicit approval.

## 4. Required firmware, drivers, SDK, board and connection

| Item | Requirement | Notes |
|---|---|---|
| Receiver firmware | `firmware/esp32/roomsense_csi_rx` | ESP-NOW mode (default) or router-ping mode (`RS_MODE_ROUTER_PING`). Prints RSHELLO / RSCSI / RSSTAT lines. |
| Transmitter firmware | `firmware/esp32/roomsense_csi_tx` | ESP-NOW mode only. Broadcasts the 12-byte `RSTX` payload on a fixed channel (default 11) at a fixed rate (default 25 Hz). Needs only power once flashed. |
| SDK | **ESP-IDF v5.5.5, exactly** | See "Why v5.5.5" below. `firmware/esp32/tools/check_idf_env.sh` checks the version. RoomSense never installs ESP-IDF. |
| Board | ESP32-S3-DevKitC-1U-N8R8 (primary) | Section 5 and `docs/PARTS_LIST.md` |
| USB-UART driver | Usually none to install | ESP-IDF's serial guide says the bridge drivers "should be bundled with an operating system and automatically installed". On Linux the common bridges are handled by drivers included with the kernel. If a port does not appear, identify the bridge chip on your board and follow the ESP-IDF serial-connection guide. Installing a driver is your decision. |
| Linux serial permission | Membership of the `dialout` group (`uucp` on Arch) | ESP-IDF's guide: `sudo usermod -a -G dialout $USER`, then log in again. This is a **privileged change that needs your approval**. RoomSense never runs sudo. |
| Serial settings | **921600 baud** | Firmware `sdkconfig.defaults` (`CONFIG_ESP_CONSOLE_UART_BAUDRATE=921600`, UART console). The host default is `baud = 921600` in `ReceiverConfig`. RSHELLO reports the baud. The S3 board's bridge is rated "up to 3 Mbps". |
| Which USB port | The board's **USB-to-UART** port | The firmware prints on the UART console, not on the chip's native USB port. |
| USB cable | **Data-capable** USB 2.0 Standard-A to Micro-B (S3 board) | The S3 guide warns that charge-only cables "do not provide the needed data lines". |
| Port name | Entered explicitly in `configs/roomsense.toml` | For example `/dev/ttyUSB0`, `/dev/cu.usbserial-…` or `COM5`. RoomSense never guesses ports. |

**Why ESP-IDF v5.5.5 exactly:**

1. RoomSense's CSI layout tables (`backend/roomsense/csi_layouts.py`) were
   transcribed from `docs/en/api-guides/wifi.rst` at tag v5.5.5. That file
   contains the CSI layout table for the recommended chip (`.. only:: esp32 or
   esp32s2 or esp32c3 or esp32s3`, line 2695) and for ESP32-C5 (line 2746). It
   has **no ESP32-C6 table**.
2. The firmware's Kconfig names in `sdkconfig.defaults` were checked against
   v5.5.5.
3. esp-csi's CI (`.gitlab-ci.yml` at the pinned commit) builds the get-started
   examples with ESP-IDF **release v5.4** (esp32, esp32s3, esp32s2, esp32c3,
   esp32c6) and **release v5.5** (those plus esp32c5 and esp32c61). v5.5.5 is a
   tag in the v5.5 release line, so it lies inside the range upstream builds
   against.
4. Pinning one tag makes the CSI layout, the Wi-Fi driver behaviour and the
   `RSHELLO` `idf_version` field reproducible. A different version may change
   CSI details without warning.

## 5. Recommended boards

**Primary: ESP32-S3-DevKitC-1U-N8R8**, with module ESP32-S3-WROOM-1U-N8R8.

* The CSI layout for esp32s3 is documented in ESP-IDF v5.5.5 (line 2695 of
  `wifi.rst`).
* esp32s3 is an esp-csi CI target.
* It is the only variant in the S3 guide's ordering table with an **external
  antenna connector**: "ESP32-S3-WROOM-1U comes with an external antenna
  connector". Source: espressif/esp-dev-kits `ceaefbd` →
  `docs/en/esp32-s3-devkitc-1/user_guide_v1.1.rst`. The same table lists
  ESP32-S3-DevKitC-1-N8R8 (WROOM-1, PCB antenna) and -1-N32R16V (WROOM-2, PCB
  antenna).
* The connector type is not named in the dev-kit guide. **Verify it in the
  ESP32-S3-WROOM-1/1U datasheet before buying an antenna.**

Why an external antenna, in esp-csi's words:

> "The effect of external IPEX antenna is better than PCB antenna, PCB antenna
> has directivity." (esp-csi `README.md`, "5 Note")

> "Use an external antenna: The PCB antenna has poor directivity and is easily
> interfered with by the motherboard." (esp-csi `examples/get-started/README.md`)

**About ESP32-C5 and ESP32-C6.** esp-csi's get-started README recommends them
for RF quality: "Use ESP32-C5 / ESP32-C6: ESP32-C5 supports dual-band Wi-Fi
communication and is one of the best RF chips available. ESP32-C6 is the best
RF chip among the currently released models." But:

* **ESP32-C6 has no CSI layout table in the ESP-IDF v5.5.5 guide.** RoomSense
  therefore **rejects C6 layouts** unless you opt into a flagged assumption
  (`allow_undocumented_layout_assumption = true` on the receiver). The C5 table
  is then reused, and every frame is flagged `UNDOCUMENTED_LAYOUT_ASSUMPTION`.
* **ESP32-C5 is supported only for its documented rows.** RoomSense accepts the
  rows with an unambiguous index order (106, 114, 234 and 490 values) and
  rejects the rest.
* The C5 and C6 dev-kit guides list no ordering codes. The C6 guide says
  WROOM-1U "uses external antenna connector". The C5 guide does not say which
  variant has one. **Verify before buying.**

Full list with quantities for each configuration (router + receiver, TX/RX
pair, zone experiment): `docs/PARTS_LIST.md`. No prices are given, and nothing
is bought automatically.

## 6. What needs physical setup or your confirmation

Nothing in this list can be done or confirmed from the build container.

1. **Boards:** get them (`docs/PARTS_LIST.md`; verify before buying). One
   receiver for router mode, a TX/RX pair for ESP-NOW mode, or 1 TX + 3–4 RX
   for a zone experiment.
2. **Antennas:** attach an antenna whose connector matches the module (type
   verified in its datasheet). Use the same model and the same orientation on
   every board.
3. **Cables:** data-capable USB cables from each receiver's USB-to-UART port to
   your computer. Use a powered hub if you have several receivers.
4. **Power for remote nodes:** a USB power supply for the transmitter (and for
   any board not connected to the computer).
5. **Placement per `docs/PLACEMENT.md`:** the TX–RX link must cross the target
   room. Most through-wall layouts need a board on the far side of the target
   room or inside it. Don't assume everything can stay on one side of a wall.
   Use fixed mounts at about 1 m height, away from metal, with at least 1 m
   TX–RX spacing. Recalibrate after any move.
6. **Wi-Fi credentials (router mode only):** you enter them locally with
   `idf.py menuconfig` → "Example Connection Configuration". They are stored
   only in the local `sdkconfig`, which `.gitignore` excludes. **Never commit
   them.** Router settings are never changed.
7. **Approval before any flashing:** building and flashing `firmware/esp32` is
   your decision, each time. RoomSense never flashes, erases or reconfigures a
   device by itself.
8. **Serial port names:** confirm which port is which board (unplug, re-run
   the inspection, plug back in) and enter them in `configs/roomsense.toml`.
   RoomSense never guesses ports.
9. **`dialout` membership (Linux):** if the ports are not readable, joining
   `dialout` (`uucp` on Arch) is a privileged change that needs your approval.
10. **Consent:** use it only in spaces you control, with everyone present
    informed and agreeing (see the validation protocol's general rules).

## 7. HARDWARE REQUIRED

```
+----------------------------------------------------------------------------+
|                           HARDWARE REQUIRED                                |
|                                                                            |
|  No ESP32 receiver was detected where this report was generated (the       |
|  cloud build container, which has no USB bus). Live CSI acquisition is     |
|  not possible there. Only recorded replay and clearly labelled SIMULATION  |
|  are available until real boards run the RoomSense firmware and their      |
|  RSHELLO lines reach the LIVE source.                                      |
+----------------------------------------------------------------------------+
```

## 8. Verification status

The four evidence levels are kept separate. A "yes" needs the named evidence.
`configs/verification_evidence.json` is authoritative for the capabilities.
The values below match it as of 2026-09-29, after the zone tests were added.

| Item | Software-tested | Firmware-compiled | Hardware-tested | Through-wall-validated |
|---|---|---|---|---|
| Hardware inspection (`roomsense/hardware.py`, `scripts/inspect_hardware.py`) | **yes**: `backend/tests/test_hardware.py`, 62 passed, 0 failed, run in the build container. Covers fake port lists and simulated Linux/macOS/Windows command output. | n/a | **NO**: never run with a real board attached | n/a |
| Firmware (`firmware/esp32`) | Host unit tests of the portable core only (see `firmware/esp32/README.md`) | **NO**: ESP-IDF is not installed in the build container | **NO** | **NO** |
| A: acquisition | yes (synthetic data and upstream fixtures only) | **NO** | **NO** | **NO** |
| B: motion detection | yes (synthetic data only) | **NO** | **NO** | **NO** |
| C: zone estimation | yes (synthetic sessions only; tests also prove synthetic data can never enable it) | **NO** | **NO** | **NO** |
| D: pose research gate | yes (gate logic only) | **NO** | **NO** | **NO** |

Passing software tests show that the code paths behave as specified on
synthetic data and fixtures. They say **nothing** about sensing accuracy, about
any real board, or about any wall.
