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
| **D** Body-pose research extension | **DISABLED**: none of the reviewed models has usable weights that fit ESP32 single-antenna CSI, and no inference runtime is shipped | [`MODEL_COMPATIBILITY.md`](MODEL_COMPATIBILITY.md) |

| Evidence level | Status |
|---|---|
| Software tested | **Yes**: backend, frontend and firmware-core test suites executed (counts in [`PROGRESS_LOG.md`](PROGRESS_LOG.md)) |
| Firmware compiled for a target | **No**: the ESP-IDF toolchain was not available in the build environment (only host tests, compile-only checks of the portable core and a header-level syntax check ran; see [`firmware/esp32/README.md`](firmware/esp32/README.md)) |
| Hardware tested | **No** |
| Through-wall validated | **No** |

## Quick start (macOS / Linux)

Requirements: `git`, **Node.js ≥ 22.12**, and **[uv](https://docs.astral.sh/uv/)**.
uv installs the pinned Python packages and can provide a Python 3.11, 3.12 or 3.13
(`backend/pyproject.toml` requires `>=3.11,<3.14`).

```bash
# one-time: tools (skip what you already have)
curl -LsSf https://astral.sh/uv/install.sh | sh   # then open a new terminal
uv python install 3.11

# get the code
git clone -b claude/wifi-roomsense-3d-1xvtvv https://github.com/abedaalhaleem/prework-about-me.git
cd prework-about-me/wifi-roomsense-3d

scripts/setup.sh    # installs pinned deps (uv.lock, package-lock.json), builds the UI, creates configs/roomsense.toml if missing (no receiver configured yet)
scripts/start.sh    # starts the local server in the background and prints the URL
open http://127.0.0.1:8765        # macOS; on Linux open the URL in a browser
scripts/stop.sh     # graceful shutdown (closes serial ports, finalises recordings, closes the database)
```

* **Windows (PowerShell):** `scripts\setup.ps1`, `scripts\start.ps1`, `scripts\stop.ps1`
  (for example `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`).
  These scripts have **never been executed**: the build environment is Linux
  only, so treat them as untested.
* **Development:** `scripts/dev.sh` runs the backend with the Vite dev server
  (http://127.0.0.1:5173) and hot reload.
* **Health check:** `curl http://127.0.0.1:8765/api/health`.
* **Logs:** `scripts/start.sh` appends the server output (structured JSON log
  lines, plus a few plain start-up lines) to `logs/roomsense.log`.
  `scripts\start.ps1` writes the log lines to `logs\roomsense.log` and stdout to
  `logs\roomsense.out.log`. `scripts/dev.sh` logs to the terminal.
* **Problems:** see [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

### My Wi-Fi signal (this computer, no extra hardware)

The **My Wi-Fi signal** page plots the signal strength (RSSI, in dBm) and
noise of the computer's own Wi-Fi connection, about twice a second, as a flat
chart and as a rotatable **3-D ribbon chart** (x = time, height = strength). On macOS
it uses Apple's CoreWLAN framework and falls back to `system_profiler` if
CoreWLAN gives no reading. Linux uses `/proc/net/wireless`, and Windows uses
`netsh`, which reports a percentage instead.

This is **one coarse number, not CSI**:

* It is not motion or through-wall sensing.
* It never feeds the motion detector, recordings, validation or the 3-D room.
* It changes mostly when the laptop or people right next to it move, or when the router adapts.

The page is read-only. It does no scanning, never reads the network name into
its samples, and keeps samples in memory only.

The macOS reader has **not yet been run on a real Mac**. It was tested only
with captured-format text and a test reader.

### Wi-Fi waves (simulated)

The **Wi-Fi waves (simulated)** page draws the room layout as a glowing 3-D
wireframe. A blue glow spreads from the chosen Wi-Fi source, and animated
rings dim as they pass through each wall. The page is a **model, not a
measurement**: it uses a textbook multi-wall path-loss model.

* **Distance loss** is free-space loss at 1 m plus 20·log10(distance).
* **Wall loss** is an assumed value for each wall crossed, taken from the material you typed. For example drywall is 3 dB, brick 9 dB, concrete 12 dB and metal 20 dB. An open doorway counts as about 1 dB.

The model ignores reflections and furniture. It **cannot show people,
objects or anything behind a wall**, and no ordinary router or laptop can.
The page carries a large SIMULATED banner and watermark. It stays separate
from the dashboard and from the measured 3-D room.

**Using it with your own home:**

1. Enter your walls, materials and a ROUTER node on the Calibration page. Until then it shows the EXAMPLE room, labelled as such.
2. Optionally, mark where your computer is.
3. The page then shows the model's prediction for that spot beside your computer's real measured RSSI, the only measured number on the page. Differences of 10–20 dB are normal.

### Without hardware

* **Simulation**: Dashboard → Source → Simulation. You must tick *"I understand
  this is simulated data, not a measurement"*. Every screen then shows a large
  **SIMULATED DATA — NOT A MEASUREMENT** banner. Simulation is never selected
  automatically.
* **Replay**: replays a recording made earlier, with its original gaps. A
  replay of simulated data is still marked simulated.
* The example config ships with **no receiver configured**, so *Start live
  (configured receivers)* is refused (`NO_RECEIVERS_CONFIGURED`) until you add
  one to `configs/roomsense.toml`, or enter one for the session under *Override
  receivers for this session* (its port must be listed by the OS). With a
  receiver but no board delivering data, Live shows **HARDWARE REQUIRED** and
  **SENSOR_OFFLINE**. It never falls back to replay or simulation.

## Hardware and firmware

* [`HARDWARE_REPORT.md`](HARDWARE_REPORT.md): what was detected (in the
  build container: nothing), the documented CSI path (ESP32 +
  [esp-csi](https://github.com/espressif/esp-csi) @ `8633d67`, ESP-IDF
  **v5.5.5**), and what needs your physical setup or confirmation.
  * Regenerate it for your own computer with
    `cd backend && uv run python ../scripts/inspect_hardware.py --write ../HARDWARE_REPORT.local.md`.
* [`docs/PARTS_LIST.md`](docs/PARTS_LIST.md): parts per configuration, without
  prices. Product facts were checked against pinned Espressif documents; none
  of the parts has been tested by this project, and anything that could not be
  checked is marked **verify before buying**. The primary board is the
  **ESP32-S3-DevKitC-1U-N8R8** (external-antenna variant).
* [`docs/PLACEMENT.md`](docs/PLACEMENT.md): where the transmitters and
  receivers go, and which layouts need a device **inside** the target room.
  Motion on the far side of one wall, sensed with every device on the near
  side, is expected to be weak and dominated by near-side motion.
* [`firmware/esp32/`](firmware/esp32/README.md) holds the receiver (ESP-NOW or
  router-ping mode) and the transmitter. They are derived from esp-csi and are
  written to use a bounded queue, send CRC-framed lines, and apply the MAC
  filter first. **They have not been compiled for any target or run on a
  board**, so this is design intent. **The app never flashes or erases
  boards.** You build and flash them yourself with the pinned ESP-IDF, after
  reviewing the commands.
* [`docs/SERIAL_PROTOCOL.md`](docs/SERIAL_PROTOCOL.md) is the firmware ↔ host line format.

## Your first real experiment

1. Get one **ESP32-S3-DevKitC-1U-N8R8**, a 2.4 GHz antenna matching its
   connector (the connector type is not named in the board guide: verify it in
   the module datasheet before buying, see `docs/PARTS_LIST.md`), and a
   data-capable USB cable. For the dedicated transmitter/receiver variant, get
   two boards.
2. Install **ESP-IDF v5.5.5** exactly (`firmware/esp32/tools/check_idf_env.sh`
   refuses other versions). Build `roomsense_csi_rx` in **router-ping** mode
   (your existing router, unchanged). This project has never compiled the
   firmware, so your build is its first. Enter your Wi-Fi credentials locally
   in `idf.py menuconfig`; they stay in the git-ignored `sdkconfig`.
3. Flash the board yourself (`idf.py -p <port> flash`). Then, in
   `configs/roomsense.toml`, uncomment the first (RoomSense firmware)
   `[[acquisition.receivers]]` block and set its `port`. Its `transmitter_mac`
   is the ESP-NOW default, so for router-ping mode set it to your router's BSSID
   or delete that line (the host then uses the MAC filter the firmware reports in
   `RSHELLO`); otherwise every frame is rejected as `MAC_NOT_CONFIGURED`. Restart
   the server afterwards (the config is read at start-up).
   `cd backend && uv run roomsense ports` lists the ports without opening them.
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
recordings ────────────────► replay ─────────┘   (one source,          (gap-aware windows,           (loopback by default)
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
  `server.allow_non_loopback = true` **and** a `ROOMSENSE_API_TOKEN` set in the
  environment, never in a file: at least 16 characters, only `A-Z a-z 0-9 . _ ~ -`.
  Once the variable is set, every `/api` request needs the token, on loopback
  too (the UI takes it under Settings), and a set but unusable token stops the
  server from starting on any address. Never expose it publicly.
* WebSocket connections and state-changing requests (POST/PUT/DELETE) sent by
  pages from other web origins are refused, and CORS lets only the Vite dev
  server's origin read responses. While bound to loopback, requests whose `Host`
  header is not a loopback name are refused too.
* Recording is **opt-in**. It requires recorded consent (no names), is bounded
  in size and duration, and can be deleted from the UI (Recordings & Replay →
  Delete) or, with the server stopped, with
  `cd backend && uv run roomsense recordings delete <id>`. Deleting a recording
  also deletes its exports and every zone model trained on it.
* The app makes no cloud calls, uses no paid APIs or language models at
  runtime, and does no Wi-Fi scanning. The firmware is written to export CSI
  only from the one configured transmitter or access point (not verified on
  hardware).

## Tests

Run each line from the `wifi-roomsense-3d` directory (after `scripts/setup.sh`):

```bash
(cd backend && uv run pytest -q)                # backend (synthetic data and upstream fixtures only)
(cd frontend && npm run typecheck && npm test && npm run build)
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
[DensePose From WiFi](https://arxiv.org/abs/2301.00250) (3×3 antennas, 30
subcarriers, amplitude and phase) and [RF-Pose3D](https://rfpose3d.csail.mit.edu/)
(FMCW radar with antenna arrays, not Wi-Fi). Neither setup matches single-antenna
ESP32 boards (see [`MODEL_COMPATIBILITY.md`](MODEL_COMPATIBILITY.md)), and this
application does not reconstruct people in 3-D.
