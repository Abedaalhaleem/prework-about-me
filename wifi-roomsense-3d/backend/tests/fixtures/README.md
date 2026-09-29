# Test fixtures and where they come from

Fixtures check **software behaviour only** (parsing, bookkeeping, state machines).
They say nothing about how well sensing works. Every fixture is labelled with its origin.

| File | Origin | What it is | What it is not |
|---|---|---|---|
| `upstream_classic_csi_recv_router_readme.txt` | Copied verbatim from `examples/get-started/csi_recv_router/README.md` in espressif/esp-csi at commit `8633d67152db2808f141cc1595970aa9cf406045` (Apache-2.0). | 14 `CSI_DATA` lines that Espressif published as example output of the router-ping receiver on a classic chip. The same README shows an ESP-IDF v4.4.1 boot log. | Not measured by this project. Not from the user's room. We don't know the board or the room. |
| `upstream_classic_header.txt` | Same README | The 25-column header line printed by the classic-chip firmware | — |
| `upstream_c5c6_get_started_readme.txt` | Copied verbatim from `examples/get-started/README.md` in espressif/esp-csi at the same commit | One 15-column `CSI_DATA` line in the ESP32-C5/C6 format, with `len=256` | 256 values is **not** a layout in the ESP32-C5 table of the ESP-IDF v5.5.5 guide, and ESP32-C6 has no published table. RoomSense therefore has to **reject** this line's layout. That rejection is what this fixture tests. |
| `synthetic_*` (made during tests) | Made by `roomsense.acquisition.synthetic` with a fixed seed | CSI-shaped int8 values from a simple made-up multipath model, tagged `SYNTHETIC` | They are not real measurements. Detector results on this data show that the code paths run. They don't show sensing accuracy. |

## Quirks in the upstream format (checked against the source at the pinned commit)

* The classic 25-column format has a header column called `rx_state` (the firmware
  source calls it `rx_format`). The firmware actually prints `rx_ctrl->sig_mode`
  in that column a second time, so the real `rx_state` is **not available** from
  the upstream classic format.
* The C5/C6 15-column format has a column labelled `rx_state` in its README
  (`rx_format` in the source). It holds `rx_ctrl->cur_bb_format`, not `rx_state`.
* When gain control is compiled in (ESP32-S3/C3/C5/C6/C61 in the upstream
  examples), the firmware multiplies each value by a float gain factor and prints
  it as `int16`. The values are then **not** the raw int8 `wifi_csi_info_t.buf`
  bytes.
* The upstream receive callbacks print from inside the Wi-Fi task. ESP-IDF says
  not to do lengthy work in that callback. The RoomSense firmware copies the data
  into a bounded queue instead and prints from a separate task.

## Firmware and host agreement

`test_acq_parser.py` cross-checks `firmware/esp32/host_tests/golden_rscsi_lines.txt`
field by field against `golden_rscsi_expected.jsonl`. The C formatter in the
firmware produces both files: it runs on the host, with values chosen by hand, so
they are synthetic. If the files are missing, the test skips and says so.
