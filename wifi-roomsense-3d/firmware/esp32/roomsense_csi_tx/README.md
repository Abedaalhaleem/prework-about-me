# roomsense_csi_tx: RoomSense ESP-NOW transmitter

ESP-IDF project (pinned ESP-IDF **v5.5.5**) for the ESP-NOW mode of
`roomsense_csi_rx`. It broadcasts the 12-byte `RSTX` payload
(magic `RSTX`, version 1, `tx_id`, `seq`, `rate_hz`, little-endian; see
[`docs/SERIAL_PROTOCOL.md`](../../../docs/SERIAL_PROTOCOL.md)) at a fixed rate.
It uses HT20 MCS0 on a fixed channel, STA mode, no power save, and a locally
administered MAC.

**Status: not compiled for any target and not tested on hardware.** The
details are in [`../README.md`](../README.md#status-read-this-first).

| Option (`idf.py menuconfig` → "RoomSense transmitter") | Default | Meaning |
|---|---|---|
| `RS_CHANNEL` | 11 | 2.4 GHz channel 1–13. Must match the receiver. Use a channel that is legal where you are. |
| `RS_TX_MAC` | `1a:00:00:00:00:00` | Set on this board. Must be unicast and locally administered; anything else is refused. |
| `RS_RATE_HZ` | 25 | Frames per second (1–100). Exact long-run rate via `xTaskDelayUntil`. |
| `RS_TX_ID` | 1 | Carried in the payload. |
| `RS_TX_LOG_INTERVAL_MS` | 10000 | Period of the `seq / sent_ok / send_fail / no_mem / late` summary log. |

`seq` advances only when `esp_now_send()` accepted the frame, so gaps at the
receiver mean over-the-air loss. On `ESP_ERR_ESPNOW_NO_MEM` it backs off for
10 ms. If it falls behind schedule it restarts the schedule from now instead
of sending a catch-up burst. It never joins a network and only ever sends its
own payload.

```bash
. $IDF_PATH/export.sh
../tools/check_idf_env.sh
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
# flashing is your decision: idf.py -p PORT flash
```
