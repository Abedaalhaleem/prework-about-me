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
  * There is no USB bus, no USB serial device (only a legacy `/dev/ttyS0` UART with no USB ID), and no wireless interface (`/sys/class/net`: eth0 virtio, ifb0, ifb1, lo).
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
* **Stage 1, built in parallel** (every count is from an executed run):
  * Acquisition: strict parsers for the upstream classic and C5/C6 formats and for RoomSense RSCSI v1, rollover handling, live serial with reconnect, replay, a labelled simulator and the acquisition manager. 156 tests.
  * Processing: gap-aware windows, quality, features, quiet baseline, hysteresis detector, drift monitor and engine. 112 tests.
  * Storage and validation metrics. 95 tests.
  * Pose gate, capability registry and model research. 196 tests.
  * Hardware inspection. 62 tests.
  * Firmware core: 27 host tests / 347 checks. A header-level syntax check was also run against the real ESP-IDF v5.5.5 headers. It is **not a build**.
  * Frontend: 73 tests, typecheck and build.
* **Stage 2.**
  * Zone estimation: 104 tests on synthetic sessions. They show synthetic data can never enable it.
  * Runtime, HTTP/WebSocket API, CLI and scripts, with the required safety-invariant tests. Full backend suite: 821 passed.
* **Integration review (executed against a real server and headless Chromium).**
  * Found and fixed a cross-origin hole: any web page could read `/api/ws` and trigger state-changing POSTs. A foreign `Origin` is now refused (826 passed).
* **First real run of the app.** Uvicorn had no WebSocket implementation, so the UI could never connect. Added the pinned `websockets` dependency.
  * The full path simulation → processing → API → 3-D dashboard was then run and screenshotted: SIMULATED DATA banner, a baseline accepted after 68 s, and MOTION_DETECTED on the simulated walk.
* **Fix round from the review.**
  * UI: simulated or replayed links are never shown as connected boards. The replay-of-simulation banner is fixed. A disconnected LIVE source gets a red pill. Staleness is based on each result's own age. Labels are decluttered and the header is compacted.
  * Backend:
    * errors always come back as `{detail, code}`;
    * `hardware_required` is no longer sticky;
    * deleting a recording also removes models, exports and consent rows, then runs a WAL checkpoint with `secure_delete`;
    * export zips count toward the quota;
    * CLI commands take the data-folder lock;
    * an invalid token refuses startup;
    * stalled processing makes results UNKNOWN;
    * `GET /api/source/receivers` was added;
    * the example config ships with no active receiver.
  * The API token charset was restricted to what a browser can send over a WebSocket. Capability B is not ENABLED while processing is stalled.
* **Fresh-clone check (executed).** `git clone` of the pushed branch → `scripts/setup.sh` → `scripts/start.sh` → `/api/health` OK, UI served, status NO SOURCE / HARDWARE REQUIRED / zone and pose DISABLED → `scripts/stop.sh`, which shut down gracefully.
  * This ran as root in the container with `ROOMSENSE_ALLOW_ROOT=1`.
  * macOS and Windows runs have **not** been executed.

## Final status (2026-09-29)

| Check | Result |
|---|---|
| Backend `uv run pytest -q` | **884 passed** (synthetic data and upstream fixtures only) |
| Frontend `npm run typecheck && npm test && npm run build` | **124 tests passed**, typecheck and build OK |
| Firmware core `make -C firmware/esp32/host_tests test` | **27/27 tests, 347 checks** |
| Firmware compiled for an ESP32 target | **Not done.** The toolchain could not be downloaded here. |
| Hardware tested | **Not done.** No boards. |
| Through-wall validated | **Not done.** UNVERIFIED. |
| PowerShell scripts | Written, **never executed** |

### Still unverified

* Everything that depends on radio behaviour:
  * real packet rates and drops;
  * whether CSI changes enough with a person moving behind the user's wall;
  * the thresholds on real data;
  * the false-alarm rate;
  * whether target-room motion can be told apart from near-side motion.
* The firmware compiling with GCC for Xtensa/RISC-V, linking, and running.
* The ESP32-C5/C6/C61 paths.
* The ESP-NOW `tx_seq` payload offset on real frames.

### Next real experiment

1. Get one ESP32-S3-DevKitC-1U with a matching 2.4 GHz antenna and a data USB cable.
2. Install ESP-IDF v5.5.5. Build `roomsense_csi_rx` in router-ping mode and flash it yourself.
3. Add the receiver to `configs/roomsense.toml`, start **Live**, and confirm RSHELLO, the measured rate and the documented layout on the Dashboard.
4. In the same room, record a quiet baseline and do a walk test.
5. Only then move to a through-wall layout (`docs/PLACEMENT.md`) and run protocol S1–S6 (`VALIDATION_REPORT.md`).

### Late fixes (2026-09-29, after the docs-versus-code review)

* A reviewer checked every document against the code and fixed documentation errors (see the git history). It flagged small code issues, which are now fixed:
  * the Settings page told users to leave the token empty on 127.0.0.1, which gave 401s when the server had a token;
  * log clipping cut off the end of tracebacks, which hid the `DATA_DIR_LOCKED` line;
  * the CLI lock message pointed to a zone-training UI that does not exist;
  * "serving on" was printed before the port was bound;
  * token-charset wording and one config comment were wrong;
  * the protocol text promised a baseline "for the whole series", but baselines are per session.
* Comments in both criteria files were corrected **before any validation or zone data existed**, and **no threshold changed**. The zone criteria comment now says `criteria_version` is the first 16 hex digits of the SHA-256. The through-wall comment now says the status uses the most recent *eligible* LIVE run. Because criteria versions hash the whole file, the through-wall `criteria_version` changed from `e76147a9ab732095` to `059548fab1041092`.

### 2026-10-02: My Wi-Fi signal and simulated Wi-Fi waves

* **My Wi-Fi signal page.** It shows this computer's own RSSI (CoreWLAN or `system_profiler` on macOS, `/proc/net/wireless` on Linux, `netsh` on Windows) as a flat chart and a 3-D ribbon chart.
  * Samples are memory-only. The network name is never read into them.
  * It never feeds the detector or the 3-D room.
  * It is tested with captured-format text only. It has not been run on a real Mac by us.
* **Wi-Fi waves (simulated) page.** It shows a 3-D wireframe of the room layout, a blue predicted-coverage glow and animated rings from the chosen source.
  * The model is a textbook multi-wall path-loss model: free-space distance loss plus an assumed loss per wall crossed, chosen by material text. It is labelled SIMULATED everywhere.
  * It cannot show people or objects.
  * Optionally, it compares the prediction at a user-marked spot with the computer's real RSSI.
  * Verified by unit tests of the model and by the page smoke test. A headless-Chromium screenshot (SwiftShader) rendered it without console errors.
