# RoomSense serial protocol v1 (`roomsense-rscsi-v1`)

The RoomSense firmware (`firmware/esp32`) and the host parser
(`backend/roomsense/acquisition/parser.py`) share this contract. Change it only
by bumping the version number that follows each tag.

## Framing

* ASCII lines ending in `\n`. A trailing `\r` is tolerated and removed.
* A line starting with `RSHELLO,`, `RSCSI,` or `RSSTAT,` is a **machine record**.
  Any other line (ESP-IDF log output such as `I (123) tag: ...`, boot ROM
  messages) is a **diagnostic line**. Diagnostics are logged and never parsed
  as data.
* Every machine record ends with `*` and 8 uppercase hex digits. Those digits are
  the IEEE CRC-32 (the same as Python `zlib.crc32`) of all bytes before the `*`.
  A record whose CRC doesn't match is rejected (`CRC_MISMATCH`). This catches
  lines that got interleaved or truncated.
* The host rejects any line longer than `acquisition.max_line_bytes` (default
  8192) before parsing it.
* The line-length limit does not count the trailing `\r\n`. The CRC is checked
  **before** the version number, so a record with a valid CRC and an unknown
  version is rejected as `UNSUPPORTED_VERSION`.
* The host learns the CSI layout configuration (`ltf_config`) of an `RSCSI` stream
  **only** from `RSHELLO`. Frames that arrive before the first hello (for example
  when the host attaches mid-stream) are kept and recorded, but flagged
  `UNKNOWN_LAYOUT`, and processing rejects them. That is why the firmware
  repeats `RSHELLO` every few seconds.
* `NA` means the field is unavailable. The host maps it to `None` and never
  substitutes a number.
* Integers are decimal. The CSI payload is lowercase hex: two hex digits per
  value, each value a two's-complement **int8** byte copied from
  `wifi_csi_info_t.buf`. No gain compensation is applied on the device.

## `RSHELLO,1,...` — sent at boot, then every `CONFIG_RS_HELLO_INTERVAL_MS` (default 5000 ms)

| # | field | example | notes |
|---|---|---|---|
| 0 | tag | `RSHELLO` | |
| 1 | version | `1` | |
| 2 | fw_name | `roomsense_csi_rx` | |
| 3 | fw_version | `0.1.0` | |
| 4 | idf_version | `v5.5.5` | from `esp_get_idf_version()` |
| 5 | chip | `esp32s3` | the `CONFIG_IDF_TARGET` string |
| 6 | chip_revision | `0.2` | from `esp_chip_info()` |
| 7 | sta_mac | `aa:bb:cc:dd:ee:ff` | the receiver's own station MAC |
| 8 | mode | `ESPNOW_RX` / `ROUTER_PING` | |
| 9 | ltf_config | `lltf_only` | the CSI acquisition config that is in use |
| 10 | channel | `11` | primary channel (`NA` until known) |
| 11 | secondary_channel | `0` | 0 none, 1 above, 2 below (`NA` until known) |
| 12 | tx_mac_filter | `1a:00:00:00:00:00` | only CSI from this MAC is exported (the AP BSSID in router mode) |
| 13 | rate_hz | `25` | configured ping rate, or the expected transmitter rate |
| 14 | queue_depth | `32` | callback→printer queue length |
| 15 | baud | `921600` | |
| 16 | build_id | `a1b2c3d` | project git describe, or `NA` |

## `RSCSI,1,...` — one CSI measurement

| # | field | notes |
|---|---|---|
| 0 | tag `RSCSI` | |
| 1 | version `1` | |
| 2 | rec_seq | u32. The receiver increments it for every CSI callback that **passed the MAC filter**, **before** queueing. Gaps therefore show queue and serial drops. It wraps at 2^32. |
| 3 | tx_seq | u32 counter from a RoomSense transmitter's ESP-NOW payload, or `NA` (router mode, or a payload that isn't ours). Gaps show over-the-air loss. |
| 4 | drops | cumulative count of records dropped on the device (queue full + oversize), u32 |
| 5 | src_mac | transmitter MAC, which matches the filter |
| 6 | rssi | dBm, signed |
| 7 | rate | `rx_ctrl.rate` |
| 8 | sig_mode | classic chips; `NA` on C5/C6/C61 |
| 9 | mcs | classic; `NA` otherwise |
| 10 | cwb | 0 = 20 MHz, 1 = 40 MHz (classic); `NA` otherwise |
| 11 | smoothing | classic; `NA` otherwise |
| 12 | not_sounding | classic; `NA` otherwise |
| 13 | aggregation | classic; `NA` otherwise |
| 14 | stbc | classic; `NA` otherwise |
| 15 | fec_coding | classic; `NA` otherwise |
| 16 | sgi | classic; `NA` otherwise |
| 17 | noise_floor | dBm. `NA` where the chip's rx_ctrl lacks it |
| 18 | ampdu_cnt | classic; `NA` otherwise |
| 19 | channel | primary channel |
| 20 | secondary_channel | classic `secondary_channel`; C5/C6 `second` |
| 21 | rx_timestamp_us | `rx_ctrl.timestamp`, u32 µs. Wraps about every 71.6 min. |
| 22 | ant | classic; `NA` otherwise |
| 23 | sig_len | |
| 24 | rx_state | classic `rx_state`; `NA` otherwise |
| 25 | bb_format | C5/C6/C61 `cur_bb_format`; `NA` on classic |
| 26 | agc_gain | `NA` (not read in v1) |
| 27 | fft_gain | `NA` (not read in v1) |
| 28 | first_word_invalid | 0/1 |
| 29 | len | number of int8 values that follow (`wifi_csi_info_t.len`) |
| 30 | csi_hex | `2*len` lowercase hex characters |

The line then ends with `*CRC8HEX`.

## `RSSTAT,1,...` — firmware health, every `CONFIG_RS_STAT_INTERVAL_MS` (default 1000 ms)

| # | field |
|---|---|
| 2 | uptime_ms (u64) |
| 3 | cb_total: CSI callbacks invoked |
| 4 | cb_filtered: dropped by the MAC filter, never copied |
| 5 | enqueued |
| 6 | dropped_queue_full |
| 7 | dropped_oversize (`len` > `RS_MAX_CSI_LEN` = 612) |
| 8 | printed |
| 9 | tx_ok (router ping replies received, or `NA`) |
| 10 | tx_fail (`NA` if not applicable) |
| 11 | free_heap |
| 12 | min_free_heap |

## Transmitter payload (ESP-NOW, `roomsense_csi_tx`)

This payload is 12 bytes, little-endian. The receiver reads it only when the source MAC matches
the configured transmitter MAC **and** the magic matches. Otherwise `tx_seq` is
`NA`. The receiver never reads or exports payloads from any other device.

| offset | size | field |
|---|---|---|
| 0 | 4 | magic `RSTX` |
| 4 | 1 | version (1) |
| 5 | 1 | tx_id (0–255) |
| 6 | 4 | seq (u32) |
| 10 | 2 | configured rate_hz (u16) |
