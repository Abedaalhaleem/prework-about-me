# Progress log

A short, factual log. "Executed" means a command was actually run in the build
environment (an ephemeral cloud Linux container **without** any ESP32, USB or
Wi-Fi hardware).

## Implementation sequence

1. Inspect the build environment and the upstream references (esp-csi, ESP-IDF docs).
2. Foundation: pinned dependencies and lockfiles, shared typed schemas, config, CSI
   layout tables, the serial protocol, the recording format, and the architecture contract.
3. In parallel: acquisition (parsers, live serial, replay, synthetic), signal
   processing (windows, features, baseline, detector), storage and validation
   metrics, the pose research gate and capability registry, hardware inspection,
   firmware, and the frontend.
4. Zone estimation (session-split training, predefined enablement criteria).
5. Runtime, FastAPI, CLI and scripts; end-to-end tests with replay and simulation;
   safety invariant tests.
6. Adversarial review (honesty, safety, security, correctness), fixes, and documentation.

## Log

### 2026-09-29

* **Environment inspection (executed).**
  * Ubuntu 24.04, 4 vCPU Xeon under KVM, Python 3.11.15, Node 22.22.2.
  * There is no USB bus, no serial devices, and no wireless interface (`/sys/class/net`: eth0 virtio, ifb0, ifb1, lo).
  * docs.espressif.com and arxiv.org are blocked by the egress proxy. GitHub over git works.
* **Upstream references pinned (executed `git clone`/`ls-remote`).**
  * espressif/esp-csi `8633d67152db2808f141cc1595970aa9cf406045` (2026-04-22).
  * ESP-IDF tag v5.5.5 = commit `b774170ff46c393eeb5e495ea37936038d3f4f4f`.
  * The esp-csi CI builds its get-started examples against ESP-IDF release v5.4 and v5.5.
* **CSI layout facts transcribed from ESP-IDF v5.5.5** `docs/en/api-guides/wifi.rst`.
  * The imaginary part comes first, then the real part.
  * LTF order is LLTF, HT-LTF, STBC-HT-LTF.
  * `first_word_invalid` means the first 4 bytes are invalid.
  * The guide has tables for classic chips and for ESP32-C5 only; there is **no ESP32-C6 table**.
  * The LLTF mapping for secondary channel "below" was checked against Espressif's published sample line. All 52 occupied subcarriers are non-zero, and the guard bands are zero.
* **Upstream format quirks found in the source.**
  * In the classic format, the column labelled `rx_state` repeats `sig_mode`.
  * In the C5/C6 format, the `rx_state` column is `cur_bb_format`.
  * On S3/C3/C5/C6/C61, the upstream values are gain-compensated int16.
  * Upstream prints from inside the Wi-Fi callback.
* **Dependencies locked.** uv.lock: fastapi 0.141.1, pydantic 2.13.5, numpy 2.4.6, scipy 1.17.1, scikit-learn 1.9.1, pyserial 3.5, pytest 8.4.2. package-lock.json: react 19.3.0, three 0.186.1, vite 8.3.1, typescript 5.9.3, vitest 5.0.2.
* **Zone enablement criteria written before any zone code or data existed** (`configs/zone_enablement.toml`).
