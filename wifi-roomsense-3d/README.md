# WiFi RoomSense 3D

A local, experimental app for sensing **movement** with Wi-Fi Channel State
Information (CSI) from ESP32 boards. It shows the supported results on a
rotatable 3-D model of a room that **you** enter.

> **Read this first.**
>
> * The 3-D view is a display of **your room model** (entered by hand) with
>   **sensing links** coloured by their measured motion state. It is not a
>   3-D reconstruction, not an image, and not a body or skeleton.
> * A laptop's built-in Wi-Fi does **not** provide CSI. You need at least one
>   supported ESP32 board. See [`HARDWARE_REPORT.md`](HARDWARE_REPORT.md).
> * Motion detection is not presence detection. **A motionless person can
>   remain undetected.** It does not count or identify people.
> * **Nothing here has been validated on real hardware yet.** Through-wall
>   detection is **UNVERIFIED** (see [`VALIDATION_REPORT.md`](VALIDATION_REPORT.md)).
>
> Use it only in spaces you control, with every participant's consent. Data
> stays on your computer.

## Status

| Capability | State in this release | Evidence |
|---|---|---|
| **A** Live CSI acquisition and signal diagnostics | Implemented. Shows **HARDWARE REQUIRED** until a board streams data. | Parser checked against Espressif's published sample lines and the firmware's formatter; live path tested with fake serial ports only. |
| **B** Motion/activity detection from measured CSI | Implemented: quiet baseline, robust score, hysteresis, UNKNOWN / CALIBRATING / NO_MOTION_DETECTED / MOTION_DETECTED / SENSOR_OFFLINE | Software-tested on **synthetic** data only |
| **C** Experimental single-person zone estimation | Implemented but **DISABLED**. It is enabled only after real labelled sessions pass the predefined criteria in [`configs/zone_enablement.toml`](configs/zone_enablement.toml). | Pipeline tested on synthetic sessions, which are never allowed to enable it |
| **D** Body-pose research extension | **DISABLED**: no compatible model or weights exist for ESP32 single-antenna CSI | [`MODEL_COMPATIBILITY.md`](MODEL_COMPATIBILITY.md) |

| Evidence level | Status |
|---|---|
| Software tested | **Yes**: backend, frontend and firmware-core test suites executed (counts in [`PROGRESS_LOG.md`](PROGRESS_LOG.md)) |
| Firmware compiled for a target | **No**: the toolchain was not available in the build environment |
| Hardware tested | **No** |
| Through-wall validated | **No** |

## Quick start (macOS / Linux)

Requirements: `git`, **Node.js ≥ 22.12**, and **[uv](https://docs.astral.sh/uv/)**.
uv installs the pinned Python packages and can provide Python 3.11+.

```bash
# one-time: tools (skip what you already have)
curl -LsSf https://astral.sh/uv/install.sh | sh   # then open a new terminal
uv python install 3.11

# get the code
git clone -b claude/wifi-roomsense-3d-1xvtvv https://github.com/abedaalhaleem/prework-about-me.git
cd prework-about-me/wifi-roomsense-3d

scripts/setup.sh    # installs pinned deps (uv.lock, package-lock.json), builds the UI, creates configs/roomsense.toml
scripts/start.sh    # starts the local server in the background and prints the URL
open http://127.0.0.1:8765        # macOS; on Linux open the URL in a browser
scripts/stop.sh     # graceful shutdown (closes serial ports, finalises recordings, closes the database)
```

* **Windows (PowerShell):** `scripts\setup.ps1`, `scripts\start.ps1`, `scripts\stop.ps1`.
* **Development:** `scripts/dev.sh` runs the backend with the Vite dev server
  (http://127.0.0.1:5173) and hot reload.
* **Health check:** `curl http://127.0.0.1:8765/api/health`.
* **Logs:** structured JSON lines in `logs/roomsense.log`.
* **Problems:** see [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

### Without hardware

* **Simulation**: Dashboard → Source → Simulation. You must tick *"I understand
  this is simulated data"*. Every screen then shows a large **SIMULATED DATA —
  NOT A MEASUREMENT** banner. Simulation is never selected automatically.
* **Replay**: replays a recording made earlier, with its original gaps. A
  replay of simulated data is still marked simulated.
* Live mode with no board attached shows **HARDWARE REQUIRED** and
  **SENSOR_OFFLINE**. It never falls back to replay or simulation.

## Hardware and firmware

* [`HARDWARE_REPORT.md`](HARDWARE_REPORT.md): what was detected (in the
  build container: nothing), the documented CSI path (ESP32 +
  [esp-csi](https://github.com/espressif/esp-csi) @ `8633d67`, ESP-IDF
  **v5.5.5**), and what needs your physical setup or confirmation.
  * Regenerate it for your own computer with
    `cd backend && uv run python ../scripts/inspect_hardware.py --write ../HARDWARE_REPORT.local.md`.
* [`docs/PARTS_LIST.md`](docs/PARTS_LIST.md): verified parts, without prices.
  The primary board is the **ESP32-S3-DevKitC-1U** (external-antenna variant).
* [`docs/PLACEMENT.md`](docs/PLACEMENT.md): where the transmitters and
  receivers go, and which layouts need a device **inside** the target room.
  Motion on the far side of one wall, sensed with every device on the near
  side, is expected to be weak and dominated by near-side motion.
* [`firmware/esp32/`](firmware/esp32/README.md) holds the receiver (ESP-NOW or
  router-ping mode) and the transmitter. They are derived from esp-csi, use a
  bounded queue, send CRC-framed lines, and apply the MAC filter first. **The
  app never flashes or erases boards.** You build and flash them yourself with
  the pinned ESP-IDF, after reviewing the commands.
* [`docs/SERIAL_PROTOCOL.md`](docs/SERIAL_PROTOCOL.md) is the firmware ↔ host line format.

## Your first real experiment

1. Get one **ESP32-S3-DevKitC-1U**, a 2.4 GHz antenna matching its connector,
   and a data-capable USB cable. For the dedicated transmitter/receiver
   variant, get two boards.
2. Install **ESP-IDF v5.5.5** exactly (`firmware/esp32/tools/check_idf_env.sh`
   refuses other versions). Build `roomsense_csi_rx` in **router-ping** mode
   (your existing router, unchanged). Enter your Wi-Fi credentials locally in
   `idf.py menuconfig`; they stay in the git-ignored `sdkconfig`.
3. Flash the board yourself (`idf.py -p <port> flash`) and add the receiver to
   `configs/roomsense.toml`. `roomsense ports` lists the ports without opening them.
4. Start **Live**. Check that the measured rate, the quality and the CSI layout
   look sane on the Dashboard.
5. Place the board and the router per `docs/PLACEMENT.md`, enter the room in
   **Calibration**, and record a **quiet baseline**. Then do a **walk test**.
   See [`CALIBRATION.md`](CALIBRATION.md).
6. Run the validation protocol (S1–S6) in [`VALIDATION_REPORT.md`](VALIDATION_REPORT.md)
   before believing any behind-wall result.

## How it works

```
ESP32 receivers ─USB serial─► strict parser ─► acquisition manager ─► processing engine ─► FastAPI ─► React + three.js
recordings ────────────────► replay ─────────┘   (one source,          (gap-aware windows,           (loopback only)
simulator (explicit) ──────► synthetic ──────┘    chosen by you)        quality, baseline, hysteresis)
```

* Every frame keeps its **provenance**: session, receiver/link, device and
  host timestamps, board and firmware identity, channel, raw CSI bytes,
  subcarrier layout, parser version and quality flags. Missing metadata stays
  *unavailable*.
* Every result carries its **source mode**, measurement window, config
  version, calibration ID and **age**.
* The activity score is a heuristic deviation from the quiet baseline. It is
  **not** a probability. Stale or low-quality data yields UNKNOWN or
  SENSOR_OFFLINE, never "empty".
* **No phase processing** and no angle-of-arrival or time-of-flight: separate
  boards share no RF clock.
* The details are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Security and privacy

* The server binds to **127.0.0.1** by default. To serve a LAN you need
  `server.allow_non_loopback = true` **and** a `ROOMSENSE_API_TOKEN`
  (≥ 16 characters, set in the environment, never in a file). Never expose it
  publicly.
* Requests from foreign web origins and non-loopback `Host` headers are refused.
* Recording is **opt-in**. It requires recorded consent (no names), is bounded
  in size and duration, and can be deleted from the UI or with
  `roomsense recordings delete <id>`.
* The app makes no cloud calls, uses no paid APIs or language models at
  runtime, and does no Wi-Fi scanning. The firmware never exports payloads
  from other devices.

## Tests

```bash
cd backend && uv run pytest -q                  # backend (synthetic data and upstream fixtures only)
cd frontend && npm run typecheck && npm test && npm run build
make -C firmware/esp32/host_tests test          # firmware core on the host (not a target build)
```

Fixtures are labelled by origin in
[`backend/tests/fixtures/README.md`](backend/tests/fixtures/README.md). Passing
tests verify **software behaviour**, not sensing accuracy.

## Repository layout

```
backend/roomsense/{acquisition,processing,inference,storage,api,validation}/   Python package
backend/tests/                  pytest suite
frontend/src/{components,scene,pages,lib,api}/   React + three.js UI
firmware/esp32/                 ESP-IDF receiver/transmitter + host-tested core
configs/                        example config, predefined criteria, pose manifest
scripts/                        setup/start/stop/dev (bash + PowerShell), hardware inspection
docs/                           architecture, serial protocol, placement, parts, troubleshooting
data/                           local runtime data (git-ignored)
```

## Documents

[`HARDWARE_REPORT.md`](HARDWARE_REPORT.md) ·
[`CALIBRATION.md`](CALIBRATION.md) ·
[`VALIDATION_REPORT.md`](VALIDATION_REPORT.md) ·
[`MODEL_COMPATIBILITY.md`](MODEL_COMPATIBILITY.md) ·
[`PROGRESS_LOG.md`](PROGRESS_LOG.md) ·
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/SERIAL_PROTOCOL.md`](docs/SERIAL_PROTOCOL.md) ·
[`docs/PLACEMENT.md`](docs/PLACEMENT.md) ·
[`docs/PARTS_LIST.md`](docs/PARTS_LIST.md) ·
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)

## Attribution

The firmware is derived from Espressif's
[esp-csi](https://github.com/espressif/esp-csi) get-started examples at commit
`8633d67152db2808f141cc1595970aa9cf406045` (Apache-2.0; the example sources
are also offered as Public Domain / CC0). The CSI layout tables are
transcribed from the ESP-IDF v5.5.5 Wi-Fi guide. Research context:
[DensePose From WiFi](https://arxiv.org/abs/2301.00250),
[RF-Pose3D](https://rfpose3d.csail.mit.edu/). These establish methods, not a
guarantee that this application or your hardware can reconstruct people in 3-D.
