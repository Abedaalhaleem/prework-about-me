# roomsense_csi_rx: RoomSense CSI receiver

ESP-IDF project (pinned ESP-IDF **v5.5.5**) that captures Wi-Fi CSI from **one**
known source and streams it over USB serial as `RSHELLO` / `RSCSI` / `RSSTAT`
lines (see [`docs/SERIAL_PROTOCOL.md`](../../../docs/SERIAL_PROTOCOL.md)).

**Status: not compiled for any target and not tested on hardware.** The
details are in [`../README.md`](../README.md#status-read-this-first).

## Modes (`idf.py menuconfig` → "RoomSense receiver")

| Option | Default | Meaning |
|---|---|---|
| `RS_MODE` | `RS_MODE_ESPNOW_RX` | `ESPNOW_RX`: CSI from a `roomsense_csi_tx` board. `ROUTER_PING`: CSI from your own AP, triggered by gateway pings. |
| `RS_CHANNEL` | 11 | ESP-NOW channel (1–13), HT20. Must match the transmitter. |
| `RS_TX_MAC` | `1a:00:00:00:00:00` | The only MAC whose CSI is exported (ESP-NOW mode). In router mode the AP's BSSID is used. |
| `RS_RATE_HZ` | 25 | Router: ping rate. ESP-NOW: expected transmitter rate, reported in RSHELLO. Range 1–100. |
| `RS_QUEUE_DEPTH` | 32 | Records buffered between the Wi-Fi callback and the printer (736 B each). |
| `RS_HELLO_INTERVAL_MS` | 5000 | RSHELLO period (also sent at boot and once the channel/filter are known). |
| `RS_STAT_INTERVAL_MS` | 1000 | RSSTAT period. |

Router-mode SSID and password go in *Example Connection Configuration*. They
stay in the local, git-ignored `sdkconfig` and must never go into
`sdkconfig.defaults`.

## Build

```bash
. $IDF_PATH/export.sh
../tools/check_idf_env.sh
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
```

Flashing (`idf.py -p PORT flash`) is a step you take yourself. The RoomSense
application never flashes boards.

## Host configuration

Add a receiver to `configs/roomsense.toml` with
`input_format = "roomsense-rscsi-v1"`, `baud = 921600`, the serial `port`, and
`transmitter_mac` set to the same MAC as `RS_TX_MAC` (or the AP BSSID in
router mode). `ltf_config` and the chip come from RSHELLO.
