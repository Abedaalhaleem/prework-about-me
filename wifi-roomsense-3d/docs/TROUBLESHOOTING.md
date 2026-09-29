# Troubleshooting

Start with the facts:

* `curl -s http://127.0.0.1:8765/api/health` shows the server version, the
  source state and `websocket_support`.
* `logs/roomsense.log` holds structured JSON log lines. Tokens and
  Authorization headers are redacted.
* The Dashboard link panel shows, per link: measured rate, quality, frames
  and rejections, parse errors, firmware drops, channel and CSI layout.

## Setup

| Symptom | Cause and fix |
|---|---|
| `uv: command not found` right after installing uv | The shell hasn't picked it up yet. Open a new terminal, or run `source $HOME/.local/bin/env`. |
| `setup: Python 3.11 or newer is required` | Run `uv python install 3.11`. You don't need to replace your system Python. |
| `Node.js >= 22.12 is required` | Install a current LTS from nodejs.org, or use `scripts/setup.sh --skip-frontend` (the API still works; the UI won't be built). |
| `npm ci` fails | Check your network, then delete `frontend/node_modules` and run `scripts/setup.sh` again. |
| `setup: do not run this as root` | Run it as your normal user. RoomSense needs no elevated privileges. |

## The server and the page

| Symptom | Cause and fix |
|---|---|
| Browser shows **NO CONNECTION TO BACKEND** | The server isn't running, or its WebSocket isn't available. Run `scripts/start.sh` and check `/api/health`. If `websocket_support` is false, run `scripts/setup.sh` again (it installs the pinned `websockets`). |
| `start.sh` says the port is in use | Another program uses 8765. Run `scripts/start.sh --port 8766` and open that port. |
| `409 DATA_DIR_LOCKED` | Another RoomSense process uses the same `data/` folder. Stop it with `scripts/stop.sh`. |
| `421` response | The request's `Host` isn't a loopback name. Use `http://127.0.0.1:8765` (or `localhost`). |
| `403 ORIGIN_NOT_ALLOWED` | A page from another web origin tried to control the server. That is refused by design. Use the UI served by RoomSense itself. |
| The server refuses to start with a non-loopback host | This is by design. LAN exposure needs `server.allow_non_loopback = true` **and** a `ROOMSENSE_API_TOKEN` of at least 16 characters in the environment. |
| The page shows the **SIMULATED DATA** banner | A simulation, or a replay of simulated data, is running. Stop it under Source → Stop. |

## Live boards

| Symptom | Cause and fix |
|---|---|
| **HARDWARE REQUIRED** / **SENSOR_OFFLINE**; `RECONNECTING` | The board isn't delivering data. Check these: <br>• The cable carries data (many cables are charge-only). <br>• The port in `configs/roomsense.toml` is right. `uv run roomsense ports` lists ports without opening them. macOS names look like `/dev/cu.usbserial-*` or `/dev/cu.usbmodem*`, Linux `/dev/ttyUSB*` / `/dev/ttyACM*`, Windows `COM*`. <br>• No other program (for example `idf.py monitor`) has the port open. |
| Linux: `Permission denied` on the port | Your user needs to be in the `dialout` group. Changing that is a privileged system change that you make yourself: `sudo usermod -aG dialout $USER`, then log out and back in. |
| The port doesn't appear at all | Try another cable or USB port. Check the board's user guide for its USB-to-UART bridge and whether your OS needs a driver. RoomSense never installs drivers. |
| Many `CRC_MISMATCH` or `NOT_ASCII` parse errors | Common causes: <br>• Baud mismatch: the firmware uses 921600. <br>• A noisy or long cable. <br>• Two readers on one port. <br>• Upstream firmware printing debug output mid-line. |
| `MAC_NOT_CONFIGURED` summaries | Frames come from a transmitter MAC other than the configured `transmitter_mac`. Check `CONFIG_RS_TX_MAC` on the transmitter, or the router BSSID in router mode. |
| Frames rejected as `UNKNOWN_LAYOUT` | Possible causes: <br>• RoomSense firmware: the host is waiting for the first `RSHELLO` (up to 5 s). <br>• Upstream esp-csi firmware: set `declared_chip` and `ltf_config` for the receiver. <br>• ESP32-C6/C61: their CSI layout isn't documented in ESP-IDF v5.5.5, so they are rejected unless you opt into the flagged assumption. |
| Measured rate well below the configured rate; quality DEGRADED | Check the firmware drop counters (RSSTAT, link panel), the USB connection, interference and the distance between boards. The measured rate is what counts; the configured rate is only a target. |
| Everything stays **UNKNOWN** | There is no valid baseline yet, the calibration was invalidated, or quality is too low. The link panel lists the reason codes. |

## Calibration and detection

| Symptom | Cause and fix |
|---|---|
| Baseline rejected with `BASELINE_UNSTABLE` | There was movement during calibration, or the static channel shifted. Make sure the target room is empty and quiet, then record again. A longer recording helps. |
| Baseline rejected with `LOW_QUALITY_DURING_CALIBRATION` | Fix the link quality first (see above). |
| `CALIBRATION_INVALID` after changing something | This is expected. Changes to the channel, layout, firmware, transmitter, links, room geometry or processing config invalidate the baseline. Record a new one. |
| `STATIC_CHANNEL_CHANGED` (UNKNOWN) | Something changed the static channel for longer than `drift_hold_s`: a board or furniture moved, or someone is standing still. Recalibrate **only if the room is really empty**. |
| A still person isn't detected | This is a known limitation. RoomSense detects motion, not presence. |
| Motion near the sensors, outside the target room, triggers detections | Expected for many layouts. Run protocol scenario S3. If it persists, the UI reports **NOT_DISTINGUISHABLE**. Consider a layout where the link crosses the target room (`docs/PLACEMENT.md`). |
| Zone estimation stays **DISABLED** | This is expected until a model trained on real labelled sessions passes `configs/zone_enablement.toml`. See `CALIBRATION.md`. |

## Recordings

| Symptom | Cause and fix |
|---|---|
| `CONSENT_INVALID` | Every person present must have been informed and agreed. The consent form must be completed (no names are collected). |
| `QUOTA_EXCEEDED` | Delete old recordings or exports, or raise the `[storage]` limits in `configs/roomsense.toml`. |
| `RECORDING_IN_USE` on delete | Stop the replay of that recording first. |
| The CLI says the data folder is locked | A server is running. Stop it, or use the UI. |

## Firmware (you build and flash it yourself)

| Symptom | Cause and fix |
|---|---|
| `check_idf_env.sh` refuses | Install ESP-IDF **v5.5.5** exactly. Other versions are untested. `RS_ALLOW_OTHER_IDF=1` overrides at your own risk. |
| Router mode never connects | Enter the SSID and password in `idf.py menuconfig` → *Example Connection Configuration*. They stay in the git-ignored `sdkconfig`. |
| No CSI in ESP-NOW mode | The transmitter and receiver must use the same channel (`CONFIG_RS_CHANNEL`) and 20 MHz bandwidth, and the configured TX MAC must match. |
