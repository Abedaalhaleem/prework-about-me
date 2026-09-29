# Troubleshooting

Start with the facts:

* `curl -s http://127.0.0.1:8765/api/health` shows the server version, the
  source state and `websocket_support`.
* `logs/roomsense.log` (written when you start with `scripts/start.sh`) holds
  structured JSON log lines, plus a few plain start-up lines. Tokens and
  Authorization headers are redacted. With a token configured, `/api/health`
  answers 401 unless you send `Authorization: Bearer <token>`.
* The Dashboard link panel shows, per link: measured rate, quality, frames
  and rejections, parse errors, firmware drops, channel and CSI layout.

## Setup

| Symptom | Cause and fix |
|---|---|
| `uv: command not found` right after installing uv | The shell hasn't picked it up yet. Open a new terminal, or run `source $HOME/.local/bin/env`. |
| `setup: Python 3.11 or newer is required` | Run `uv python install 3.11`. You don't need to replace your system Python. |
| `Node.js >= 22.12 is required` | Install a current LTS from nodejs.org, or use `scripts/setup.sh --skip-frontend` (the API still works; the UI won't be built). |
| `npm ci` fails | Check your network, then delete `frontend/node_modules` and run `scripts/setup.sh` again. |
| `setup: do not run this as root` | Run it as your normal user. RoomSense needs no elevated privileges. In a container that only has root, `ROOMSENSE_ALLOW_ROOT=1 scripts/setup.sh`. |
| Windows: PowerShell refuses to run `scripts\*.ps1` | Run it as `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (the same for `start.ps1` / `stop.ps1`). The PowerShell scripts have never been executed by this project, so report any failure. |

## The server and the page

| Symptom | Cause and fix |
|---|---|
| Browser shows **NO CONNECTION TO BACKEND** | The server isn't running, or its WebSocket isn't available. Run `scripts/start.sh` and check `/api/health`. If `websocket_support` is false, run `scripts/setup.sh` again (it installs the pinned `websockets`). |
| `start.sh` reports *RoomSense exited during startup* and the log lines show `address already in use` | Another program uses 8765. Run `scripts/start.sh --port 8766` and open that port. |
| `DATA_DIR_LOCKED` from a `roomsense` command, or a second server exits with *Application startup failed* | Another RoomSense process (a server, `roomsense capture`, `recordings delete/export` or `zone-train`) uses the same `data/` folder. Stop it (a server started with `scripts/start.sh`: `scripts/stop.sh`). A second server's output ends with the `DATA_DIR_LOCKED` line naming the folder. |
| `421 HOST_NOT_ALLOWED` | The request's `Host` isn't a loopback name. Use `http://127.0.0.1:8765` (or `localhost`). |
| `403 ORIGIN_NOT_ALLOWED` | A page from another web origin tried to control the server. That is refused by design. Use the UI served by RoomSense itself. |
| The server refuses to start with a non-loopback host | This is by design. LAN exposure needs `server.allow_non_loopback = true` **and** a `ROOMSENSE_API_TOKEN` in the environment. |
| The server refuses to start: `ROOMSENSE_API_TOKEN ...` (on any address, loopback included) | The variable is set but unusable. A token needs at least 16 characters, only from `A-Z a-z 0-9 . _ ~ -` (for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). Or unset the variable to run on loopback without a token. |
| The page shows the **SIMULATED DATA** banner | A simulation, or a replay of simulated data, is running. Stop it with Dashboard → Source → **Stop source**. |

## Live boards

| Symptom | Cause and fix |
|---|---|
| Starting Live is refused with `NO_RECEIVERS_CONFIGURED` | `configs/roomsense.toml` has no active `[[acquisition.receivers]]` block. The example ships with both blocks commented out, because their ports are only samples. Uncomment one block and set its `port` (then restart the server), or enter a receiver under Dashboard → Source → Live → *Override receivers for this session*. |
| `PORT_NOT_AVAILABLE` (422) when starting Live with receivers from the UI | The port is neither configured nor currently listed by the OS (`GET /api/serial/ports`). Plug the board in, check the port name, and try again. |
| **HARDWARE REQUIRED** / **SENSOR_OFFLINE**; `RECONNECTING` | The board isn't delivering data. Check these: <br>• The cable carries data (many cables are charge-only). <br>• The port in `configs/roomsense.toml` is right. `cd backend && uv run roomsense ports` lists ports without opening them. macOS names look like `/dev/cu.usbserial-*` or `/dev/cu.usbmodem*`, Linux `/dev/ttyUSB*` / `/dev/ttyACM*`, Windows `COM*`. <br>• No other program (for example `idf.py monitor`) has the port open. |
| Linux: `Permission denied` on the port | Your user needs to be in the `dialout` group. Changing that is a privileged system change that you make yourself: `sudo usermod -aG dialout $USER`, then log out and back in. |
| The port doesn't appear at all | Try another cable or USB port. Find the board's USB-to-UART bridge chip (the ESP32-S3-DevKitC-1 guide does not name it; the board schematic does) and check whether your OS needs a driver. RoomSense never installs drivers. |
| Many `CRC_MISMATCH` or `NOT_ASCII` parse errors | Common causes: <br>• Baud mismatch: the firmware is configured for 921600. <br>• A noisy or long cable. <br>• Two readers on one port. <br>• Upstream firmware printing debug output mid-line. |
| `MAC_NOT_CONFIGURED` summaries | Frames come from a transmitter MAC other than the configured `transmitter_mac`. Check `CONFIG_RS_TX_MAC` on the transmitter, or the router BSSID in router mode. |
| Frames rejected as `UNKNOWN_LAYOUT` | Possible causes: <br>• RoomSense firmware: the host is waiting for the first `RSHELLO` (up to 5 s). <br>• Upstream esp-csi firmware: set `declared_chip` and `ltf_config` for the receiver. <br>• ESP32-C6/C61: their CSI layout isn't documented in ESP-IDF v5.5.5, so they are rejected unless you opt into the flagged assumption. |
| Measured rate well below the configured rate; quality DEGRADED | Check the firmware drop counters (RSSTAT, link panel), the USB connection, interference and the distance between boards. The measured rate is what counts; the configured rate is only a target. |
| Everything stays **UNKNOWN** | There is no valid baseline yet, the calibration was invalidated, or quality is too low. The link panel lists the reason codes. A baseline applies only to the session it was recorded in: after stopping or switching the source, or restarting the server, record a new one (`NO_BASELINE`). |

## Calibration and detection

| Symptom | Cause and fix |
|---|---|
| Baseline rejected with `BASELINE_UNSTABLE` | There was movement during calibration, or the static channel shifted. Make sure the target room is empty and quiet, then record again. A longer recording helps. |
| Baseline rejected with `LOW_QUALITY_DURING_CALIBRATION` | More than half of the windows were skipped for low quality. Fix the link quality first (see above). |
| Baseline rejected with `INSUFFICIENT_DURATION` or `INSUFFICIENT_WINDOWS` | Record longer: at least `detection.baseline_min_duration_s` (default 60 s) and `detection.baseline_min_windows` (default 30) usable windows. |
| Starting the baseline is refused with `ROOM_NOT_CONFIRMED_EMPTY` | Tick *"I confirm the target room is empty and quiet"* (API: `"confirm_room_empty": true`). |
| `CALIBRATION_INVALID` after changing something | This is expected. A different channel or bandwidth, CSI layout, chip or firmware, or transmitter MAC on a link, or saved room geometry with a new hash, invalidates the baseline. Record a new one. (A changed `[processing]`/`[detection]` config needs a restart, which starts a session without a baseline: `NO_BASELINE`.) |
| `STATIC_CHANNEL_CHANGED` (UNKNOWN) | Something changed the static channel for longer than `drift_hold_s`: a board or furniture moved, or someone is standing still. Recalibrate **only if the room is really empty**. |
| A still person isn't detected | This is a known limitation. RoomSense detects motion, not presence. |
| Motion near the sensors, outside the target room, triggers detections | Expected for many layouts. Run protocol scenario S3. If outside motion is detected in at least half of at least 10 labelled S3 events, the validation status is **NOT_DISTINGUISHABLE**. Consider a layout where the link crosses the target room (`docs/PLACEMENT.md`). |
| Zone estimation stays **DISABLED** | This is expected until a model trained on real labelled sessions passes `configs/zone_enablement.toml`. See `CALIBRATION.md`. |

## Recordings

| Symptom | Cause and fix |
|---|---|
| `CONSENT_INVALID` | Every person present must have been informed and agreed. The consent form must be completed (no names are collected). |
| `QUOTA_EXCEEDED` | Delete old recordings (this also deletes their exports), or raise the `[storage]` limits in `configs/roomsense.toml`. |
| `RECORDING_IN_USE` on delete | Stop the replay of that recording first, or wait until a zone training that reads it has finished. |
| The CLI says the data folder is locked (`DATA_DIR_LOCKED`) | A server is running. Stop it, or do the same through the running server: recordings in the UI, zone training with `POST /api/zone/train` (the UI has no training form). |

## Firmware (you build and flash it yourself)

| Symptom | Cause and fix |
|---|---|
| `check_idf_env.sh` refuses | Install ESP-IDF **v5.5.5** exactly: the firmware was written against it (it has not been compiled with any version yet). `RS_ALLOW_OTHER_IDF=1` overrides at your own risk. |
| Router mode never connects | Enter the SSID and password in `idf.py menuconfig` → *Example Connection Configuration*. They stay in the git-ignored `sdkconfig`. |
| No CSI in ESP-NOW mode | The transmitter and receiver must use the same channel (`CONFIG_RS_CHANNEL`) and 20 MHz bandwidth, and the configured TX MAC must match. |
