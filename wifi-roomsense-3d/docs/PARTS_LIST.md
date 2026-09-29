# Parts list

This list gives the parts for each configuration. **It has no prices, and
RoomSense never orders anything.** Each fact about a product cites the
Espressif document it was checked against, at a pinned commit. If a fact
could not be checked here, the entry says **verify before buying**.

> **Status: not hardware-tested.** This project has not yet run any of these
> parts. "Recommended" means the documentation supports the choice. It does not
> mean the choice has been tested. Your own `inspect_hardware.py` report and
> validation runs are the evidence.

## Sources (pinned)

| Short name | Repository and commit | Files read |
|---|---|---|
| S3 guide | espressif/esp-dev-kits `ceaefbd43f80c01496818612b8b874b04eca8038` | `docs/en/esp32-s3-devkitc-1/user_guide_v1.1.rst` (and `user_guide_v1.0.rst`) |
| C5 guide | same | `docs/en/esp32-c5-devkitc-1/user_guide.rst` (v1.2) |
| C6 guide | same | `docs/en/esp32-c6-devkitc-1/user_guide.rst` (v1.2) |
| esp-csi | espressif/esp-csi `8633d67152db2808f141cc1595970aa9cf406045` | `README.md`, `examples/get-started/README.md`, `examples/get-started/csi_recv_router/README.md`, `.gitlab-ci.yml` |
| IDF docs | espressif/esp-idf tag `v5.5.5` (`b774170ff46c393eeb5e495ea37936038d3f4f4f`) | `docs/en/api-guides/wifi.rst`, `docs/en/get-started/establish-serial-connection.rst` |

The module datasheets linked from the user guides (for example
`esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf`) could not be downloaded in the
build environment. Anything that depends on them is marked **verify before
buying**.

## The board: ESP32-S3-DevKitC-1U-N8R8 (primary)

Verified in the S3 guide (v1.1, "Ordering Information" and "Description of
Components"):

* The ordering table lists three variants:

  | Ordering code | Module | Flash | PSRAM | SPI voltage |
  |---|---|---|---|---|
  | ESP32-S3-DevKitC-1-N8R8 | ESP32-S3-WROOM-1-N8R8 | 8 MB Quad SPI | 8 MB Octal SPI | 3.3 V |
  | ESP32-S3-DevKitC-1-N32R16V | ESP32-S3-WROOM-2-N32R16V | 32 MB Octal SPI | 16 MB Octal SPI | 1.8 V |
  | **ESP32-S3-DevKitC-1U-N8R8** | **ESP32-S3-WROOM-1U-N8R8** | 8 MB Quad SPI | 8 MB Octal SPI | 3.3 V |

* Antenna: "ESP32-S3-WROOM-1 and ESP32-S3-WROOM-2 comes with a PCB antenna.
  ESP32-S3-WROOM-1U comes with an external antenna connector." So
  **DevKitC-1U-N8R8 is the only listed variant with an external antenna
  connector.** The guide does not name the connector type.
* USB: the board has a Micro-USB **"USB-to-UART Port"** ("via the on-board
  USB-to-UART bridge") and a separate **"ESP32-S3 USB Port"** (the chip's own
  USB OTG interface). The guide lists "USB 2.0 cable (Standard-A to Micro-B)"
  as required hardware and warns: "Some cables are for charging only and do
  not provide the needed data lines nor work for programming the boards."
* Bridge chip: "Single USB-to-UART bridge chip provides transfer rates up to
  3 Mbps." The guide does not name the chip. The ESP-IDF serial guide says to
  "check the board user guide for specific USB-to-UART bridge chip used". Check
  the board schematic if you need the exact chip.
* Power: "USB-to-UART Port and ESP32-S3 USB Port (either one or both), default
  power supply (recommended)". The 5V/G and 3V3/G pins are the other two
  options. The three options are mutually exclusive.
* Revisions: "Both the initial and v1.1 versions of ESP32-S3-DevKitC-1 are
  available on the market. The main difference lies in the GPIO assignment for
  the RGB LED." RoomSense firmware does not use that LED, so either revision
  should do (not hardware-tested).

**Which USB port to use.** The RoomSense firmware prints on the UART console
at 921600 baud (`firmware/esp32/roomsense_csi_rx/sdkconfig.defaults`:
`CONFIG_ESP_CONSOLE_UART_CUSTOM=y`, `CONFIG_ESP_CONSOLE_UART_BAUDRATE=921600`).
Connect the **USB-to-UART port**. The bridge's rated 3 Mbps is above
921600 baud.

**Why the ESP32-S3:**

1. Its CSI buffer layout is documented in the pinned ESP-IDF guide. `wifi.rst`
   line 2695 opens the CSI table with `.. only:: esp32 or esp32s2 or esp32c3 or
   esp32s3`. RoomSense's layout tables (`backend/roomsense/csi_layouts.py`) are
   transcribed from that table.
2. esp-csi's CI builds the get-started examples for `esp32s3` with ESP-IDF
   release v5.4 and v5.5 (`.gitlab-ci.yml`, jobs `build_idf_v5.4` and
   `build_idf_v5.5`).
3. The -1U variant has an external antenna connector, which esp-csi
   recommends (quotes below).

**Acceptable alternative:** ESP32-S3-DevKitC-1-N8R8. It uses the same chip and
firmware but has a PCB antenna, so expect more sensitivity to how the board is
turned. **Not useful here:** DevKitC-1-N32R16V. It also has a PCB antenna and
gives RoomSense nothing extra.

**Before buying, check:**

* The listing shows the exact ordering code (`-1U-N8R8`) and the module
  marking `ESP32-S3-WROOM-1U`.
* The seller is Espressif or an authorised distributor. Third-party "clone"
  boards can differ in bridge chip, antenna and layout, and none of the facts
  above apply to them.

### What esp-csi says about antennas and chips

> "The effect of external IPEX antenna is better than PCB antenna, PCB antenna
> has directivity." (esp-csi `README.md`, section "5 Note")

> "Use an external antenna: The PCB antenna has poor directivity and is easily
> interfered with by the motherboard." (esp-csi `examples/get-started/README.md`)

> "Use ESP32-C5 / ESP32-C6: ESP32-C5 supports dual-band Wi-Fi communication and
> is one of the best RF chips available. ESP32-C6 is the best RF chip among the
> currently released models." (esp-csi `examples/get-started/README.md`)

RoomSense still makes the ESP32-S3 its primary board, despite that last
recommendation, for these reasons:

* **ESP32-C6: not supported by default.** The pinned ESP-IDF v5.5.5 `wifi.rst`
  has CSI layout tables only for esp32/esp32s2/esp32c3/esp32s3 (line 2695) and
  esp32c5 (line 2746). It has **no ESP32-C6 table**. RoomSense rejects C6 CSI
  unless the receiver config sets `allow_undocumented_layout_assumption = true`.
  In that case it reuses the C5 table and flags every frame
  `UNDOCUMENTED_LAYOUT_ASSUMPTION`. The C6 guide says "ESP32-C6-WROOM-1 uses
  on-board PCB antenna, whereas ESP32-C6-WROOM-1U uses external antenna
  connector". The guide lists no ordering codes, so **verify before buying**.
* **ESP32-C5: secondary, with partial support.** It has a documented table.
  RoomSense accepts only the rows with an unambiguous index order (106, 114,
  234 and 490 values) and rejects the rest. The C5 guide describes
  "ESP32-C5-WROOM-1(U) ... with on-board PCB antenna" and does not say which
  variant has an external connector. The guide lists no ordering codes, so
  **verify before buying**. C5 and C6 boards use "USB-A to USB-C" cables per
  their guides.

## Antennas (for the -1U boards)

| What | Detail | Status |
|---|---|---|
| Band | 2.4 GHz. The RoomSense ESP-NOW mode uses channels 1–13 (`RS_CHANNEL`, `firmware/esp32/*/main/Kconfig.projbuild`), which are 2.4 GHz channels. The ESP32-S3 feature list in the pinned `wifi.rst` (the `esp32 or esp32s2 or esp32c3 or esp32s3` block from line 17) names IEEE 802.11b, 802.11g and 802.11n and does not mention 5 GHz. | Checked in the firmware config and the IDF docs. The antenna's own band: **verify before buying** |
| Connector | Must match the external antenna connector of ESP32-S3-WROOM-1U. The dev-kit guide does not name its type. esp-csi's README calls external antennas "IPEX", but that is a general note, not a statement about this module. | **Verify before buying** in the ESP32-S3-WROOM-1/1U datasheet |
| Type and gain | No specific antenna model has been checked by this project. Use the **same antenna model on every board**. Module radio approvals are often tied to particular antenna types and gains, so check what the module datasheet or certification allows before using a different antenna. | **Verify before buying** |
| Quantity | One per -1U board | — |

## Cables, power and hubs

| Item | Detail | Status |
|---|---|---|
| USB data cable, receiver to PC | USB 2.0 Standard-A to Micro-B that **carries data**, not charge-only (S3 guide). If your PC only has USB-C ports, use a USB-C to Micro-B data cable. | Verified (S3 guide); USB-C variant: verify it carries data |
| Transmitter power | The board can be powered through its USB port (S3 guide, "Power Supply Options"). Use a USB power adapter with a USB-A output plus a cable. A power bank also works, but some switch off at low load. | Verify your adapter or bank stays on |
| Powered USB hub (config 3) | One serial port per receiver. A powered hub near the receivers avoids long passive cables. | Verify each receiver appears as its own port (`inspect_hardware.py`) |
| USB extension or active cables | Only if a receiver has to sit far from the PC. Check the rate and drops in the Dashboard afterwards. | Verify before relying on it |

## Configuration 1: router + one ESP32 receiver

Router-ping mode (`RS_MODE_ROUTER_PING` in `roomsense_csi_rx`) uses the same
approach as esp-csi's `csi_recv_router` example. The receiver joins **your**
Wi-Fi network and pings the gateway at `RS_RATE_HZ` (default 25). CSI is taken
only from frames sent by that access point's BSSID. **Your router is used
unchanged**: RoomSense never changes router settings.

| Qty | Item | Why | Source / status |
|---|---|---|---|
| 1 | ESP32-S3-DevKitC-1U-N8R8 (receiver, `roomsense_csi_rx`) | Documented CSI layout; external antenna connector | S3 guide; IDF docs |
| 1 | 2.4 GHz antenna matching the WROOM-1U connector | esp-csi recommends external antennas | **Verify before buying** |
| 1 | USB 2.0 Standard-A to Micro-B data cable | Flashing and the serial data stream | S3 guide |
| 1 | Your existing router (unchanged) | Acts as the transmitter | It must offer a 2.4 GHz network the board can join. The S3 guide does not describe 5 GHz support for this board, so check the ESP32-S3 datasheet. |
| 1 | Computer with a free USB port | Runs RoomSense | — |

What you need to provide:

* **Wi-Fi credentials, entered locally.** You type them in `idf.py
  menuconfig` → "Example Connection Configuration". They are stored only in
  the local `sdkconfig`, which `.gitignore` excludes. **Never commit them.**
* Your approval before anything is flashed.

Limits, in esp-csi's own words for this approach: "Depends on the router, such
as the location of the router, the supported Wi-Fi protocol, etc." (esp-csi
`README.md`, section 4.1). The router's position fixes one end of the sensing
link, which usually makes through-wall layouts (docs/PLACEMENT.md (b)) hard to
arrange. Other traffic on your network also shares the channel.

## Configuration 2: dedicated transmitter + receiver pair

ESP-NOW mode. `roomsense_csi_tx` broadcasts a 12-byte `RSTX` payload on a
fixed channel (`RS_CHANNEL`, default 11) at `RS_RATE_HZ` (default 25) from a
fixed, locally administered MAC (`RS_TX_MAC`, default `1a:00:00:00:00:00`, the
same as esp-csi's `csi_send`). The receiver uses CSI only from that MAC. The
pair **does not depend on your router** and never joins a network. Pick a
channel that is legal where you are.

| Qty | Item | Why | Source / status |
|---|---|---|---|
| 2 | ESP32-S3-DevKitC-1U-N8R8 (one transmitter, one receiver) | Same documented chip at both ends | S3 guide; IDF docs |
| 2 | 2.4 GHz antennas matching the WROOM-1U connector, same model | Same antenna at both ends | **Verify before buying** |
| 1 | USB 2.0 Standard-A to Micro-B data cable (receiver to PC; also used to flash the transmitter) | Data | S3 guide |
| 1 | USB power adapter + USB-A to Micro-B cable for the transmitter where it is mounted | The transmitter only needs power | Verify the adapter |
| 1 | Computer with a free USB port | Runs RoomSense | — |

This is the configuration for the through-wall layouts in
`docs/PLACEMENT.md` (b1) and (b2). You can put both boards exactly where the
link has to go.

## Configuration 3: zone experiment (1 transmitter + 3 or 4 receivers)

**Experimental.** Several independent receivers are **not a substitute for a
synchronised antenna array**: each has its own clock and there is no shared
phase reference. Zone output (capability C) stays DISABLED until a model
passes the predefined criteria in `configs/zone_enablement.toml` on held-out
sessions. That file requires `min_receivers = 3` and the same links at runtime.

| Qty | Item | Why | Source / status |
|---|---|---|---|
| 4–5 | ESP32-S3-DevKitC-1U-N8R8 (1 transmitter + 3 or 4 receivers) | Links that cross different zones | S3 guide; IDF docs |
| 4–5 | 2.4 GHz antennas matching the WROOM-1U connector, all the same model | Consistent links | **Verify before buying** |
| 3–4 | USB data cables, one per receiver | One serial stream per receiver at 921600 baud | S3 guide. The host has not been measured with 3–4 streams at once. |
| 1 | Powered USB hub (if the PC lacks ports or the receivers are far away) | One port per receiver | Verify each port appears |
| 1 | USB power adapter + cable for the transmitter | Power only | Verify the adapter |
| optional | USB extension or active cables | Distance to receivers | Verify before relying on them |

Placement needs access to **several adjacent spaces** around the target room
(`docs/PLACEMENT.md` (d)). If you only control one side of the wall, don't buy
parts for this configuration yet: layout (c) explains why.

## Not recommended

* **Your PC's built-in Wi-Fi adapter.** It is **not** a CSI source. Ordinary
  drivers do not expose CSI. The research tools that do (Linux 802.11n CSI Tool
  for the Intel 5300, the Atheros CSI Tool for some ath9k chips, Nexmon CSI for
  specific Broadcom/Cypress chips) need specific hardware plus modified
  drivers or firmware. Those are privileged changes. RoomSense does not install
  or support them, and does not recommend them without your explicit approval.
  `inspect_hardware.py` lists them with sources.
* **ESP32-C6 boards as your only boards**, unless you accept the flagged
  layout assumption described above.
* **Anything sold as "CSI-ready" without an Espressif ordering code.** The
  facts on this page apply only to the documented Espressif boards.
