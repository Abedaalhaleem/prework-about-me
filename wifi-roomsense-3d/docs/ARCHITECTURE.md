# Architecture and module contracts

This file is the contract between modules. Where a public signature here
differs from the code, one of them must be fixed.

## Data flow

```
 ESP32 receiver(s) ──USB serial──►  LiveSerialSource ─┐
 recordings/*.jsonl.gz ──────────►  ReplaySource     ─┼─► AcquisitionManager ──► ProcessingEngine ──► ActivityResult per link
 seeded generator (explicit) ────►  SyntheticSource  ─┘        │   (one source,        │  (buffer → gap-aware window →
                                                                │    explicitly         │   quality → features → baseline
                                                                │    selected)          │   comparison → hysteresis)
                                                                ▼                       ▼
                                                   Recorder (opt-in, consented,   ZonePredictor (DISABLED unless the
                                                   bounded) + SQLite metadata     enablement criteria passed) · PoseGate (DISABLED)
                                                                                        │
                                                    AppRuntime ──► SystemStatus ──► FastAPI (/api/*, /api/ws) ──► React + three.js UI
```

Rules that are enforced in code and covered by tests:

1. **Only one source is ever active, and the user always picks it.** When live
   hardware disconnects, the source goes to `DISCONNECTED` and the affected
   links go to `SENSOR_OFFLINE`. It never falls back to replay or simulation.
2. Every derived output carries a `Provenance`: source mode, session, links,
   window, config version, calibration ID, model version, and age.
3. Stale data or low quality produces `UNKNOWN` or `SENSOR_OFFLINE`, never
   `NO_MOTION_DETECTED`.
4. Zone output (capability C) is `DISABLED` until a model has passed the
   predefined criteria in `configs/zone_enablement.toml` on held-out sessions.
   The hardware signature and room hash must also still match. Pose output
   (capability D) is `DISABLED` unless the gate in `inference/pose/gate.py`
   passes, and no model in this release passes it.
5. Room geometry is `USER_PROVIDED` or `EXAMPLE`. It is never inferred from Wi-Fi.

## Shared modules (owned by the orchestrator; other modules import them)

* `roomsense/__init__.py`: `SCHEMA_VERSION`, `PARSER_VERSION`
* `roomsense/schemas.py`: every shared record type (see the docstrings)
* `roomsense/config.py`: `AppConfig`, `load_config()`, `api_token()`
* `roomsense/csi_layouts.py`: documented CSI layouts, `resolve_layout()`, `extract_complex()`
* `docs/SERIAL_PROTOCOL.md`: the firmware ↔ host wire format

## Module contracts

### `roomsense/acquisition/`

```python
# rollover.py
class CounterUnwrapper:            # generic N-bit wrapping counter
    def __init__(self, bits: int = 32, reset_threshold: int | None = None): ...
    def update(self, raw: int) -> tuple[int, list[str]]  # (unwrapped, QualityFlag values)
    # flags: COUNTER_ROLLOVER (wrapped), COUNTER_RESET (device reboot: big backwards jump
    # not explained by wrap), COUNTER_GAP (forward jump > 1)
class TimestampUnwrapper:          # 32-bit microsecond timestamps, wraps ~71.6 min
    def update(self, raw_us: int) -> tuple[int, list[str]]  # TIMESTAMP_ROLLOVER / TIMESTAMP_NON_MONOTONIC

# parser.py (never uses eval; strict ints; bounded sizes)
class ParseError(Exception): code: str; detail: str
@dataclass class HelloRecord: ...   # fields of RSHELLO
@dataclass class StatRecord: ...    # fields of RSSTAT
@dataclass class CsiLineRecord: ... # format-neutral parsed CSI line (all metadata + values)
@dataclass class DiagnosticLine: text: str
def parse_line(line: bytes | str, fmt: InputFormat, *, max_line_bytes: int, max_csi_values: int
               ) -> CsiLineRecord | HelloRecord | StatRecord | DiagnosticLine   # raises ParseError
class FrameBuilder:                 # parsed record + receiver context -> CsiFrame
    def __init__(self, receiver: ReceiverConfig, *, session_id: str, source_mode: SourceMode,
                 keep_raw_lines: bool = True, max_raw_line_chars: int = 4096): ...
    def on_hello(self, hello: HelloRecord) -> list[str]   # returns change notices (e.g. IDENTITY_CHANGED)
    def build(self, rec: CsiLineRecord, *, host_monotonic_ns: int | None, host_unix_ns: int | None,
              raw_line: str | None = None) -> CsiFrame

# base.py
@dataclass class FrameEvent: frame: CsiFrame
@dataclass class LinkEvent: link_id: str; receiver_id: str; kind: str; detail: str = ""
    # kind: CONNECTED, DISCONNECTED, RECONNECTING, ERROR, HELLO, STAT, PARSE_ERROR, DIAGNOSTIC
@dataclass class EndOfStream: reason: str
SourceEvent = FrameEvent | LinkEvent | EndOfStream
class FrameSource(ABC):
    mode: SourceMode; session_id: str
    def start(self, sink: Callable[[SourceEvent], None]) -> None
    def stop(self, timeout_s: float = 5.0) -> None
    def link_ids(self) -> list[str]
    def describe(self) -> dict            # human readable detail for the UI

# serial_source.py
class LiveSerialSource(FrameSource):      # mode = LIVE, one reader thread per receiver
    def __init__(self, receivers: list[ReceiverConfig], acq: AcquisitionConfig, storage: StorageConfig,
                 session_id: str, serial_factory: Callable[..., Any] | None = None): ...
def list_serial_ports() -> list[dict]     # pyserial list_ports; flags known USB-UART bridge VIDs

# replay_source.py
class ReplaySource(FrameSource):          # mode = REPLAY; preserves original gaps; re-stamps host time
    def __init__(self, recording_path: Path, *, session_id: str, speed: float = 1.0): ...

# synthetic.py
@dataclass class Episode: t_start_s: float; t_end_s: float; kind: str; params: dict
    # kind: EMPTY, MOTION, STILL_PERSON, DOOR, NEAR_SENSOR_OUTSIDE, DISCONNECT, PACKET_LOSS
@dataclass class SyntheticScenario: name: str; duration_s: float; rate_hz: float; links: list[tuple[str,str]]; episodes: list[Episode]; seed: int
    def ground_truth(self) -> list[dict]  # labelled episodes (for software tests only)
def builtin_scenarios() -> dict[str, SyntheticScenario]
class SyntheticSource(FrameSource):       # mode = SIMULATION; every frame flagged SYNTHETIC
    def __init__(self, scenario: SyntheticScenario, *, session_id: str, realtime: bool = True,
                 speed: float = 1.0): ...
def generate_frames(scenario, session_id) -> Iterator[CsiFrame]   # offline, deterministic

# manager.py
class AcquisitionManager:
    def __init__(self, cfg: AppConfig): ...
    def add_consumer(self, fn: Callable[[SourceEvent], None]) -> None
    def start(self, source: FrameSource) -> None      # stops any previous source first
    def stop(self) -> None
    @property def active(self) -> FrameSource | None
    def source_state(self) -> SourceState
    def link_statuses(self, now_monotonic_ns: int | None = None) -> list[LinkStatus]

# alignment.py
class ClockModel:     # per receiver: robust fit host_time ≈ a + (1+drift)*device_time
    def update(self, device_ts_us: int, host_monotonic_ns: int) -> None
    def offset_ms(self) -> float | None; def drift_ppm(self) -> float | None; def residual_ms(self) -> float | None
def align_link_windows(window_ends_ns: dict[str, int], tolerance_s: float) -> tuple[bool, str]
```

### `roomsense/processing/`

```python
# amplitude.py
@dataclass class AmplitudeSample: t_ns: int; counter: int | None; k: np.ndarray; amp: np.ndarray; valid: np.ndarray; rssi: int | None; flags: tuple[str, ...]; layout_id: str
def frame_to_amplitude(frame: CsiFrame) -> AmplitudeSample | None   # None => rejected (reason via frame flags)

# windows.py
@dataclass class Window: link_id; t_start_ns; t_end_ns; t: np.ndarray (s, relative); amp: np.ndarray (frames×subcarriers); valid: np.ndarray (subcarriers); counters; rssi; n_frames; gaps: list[tuple[int,int]]
@dataclass class WindowRejection: link_id; reason: str; detail: str
def build_window(samples: Sequence[AmplitudeSample], *, end_ns: int, cfg: ProcessingConfig, link_id: str) -> Window | WindowRejection

# quality.py
def assess_quality(window: Window | None, *, expected_rate_hz: float | None, cfg: ProcessingConfig, rejected_frames: int = 0) -> QualityReport

# features.py
@dataclass class FeatureVector: link_id; t_end_ns; names: tuple[str,...]; values: np.ndarray; per_subcarrier: dict[str, np.ndarray]; profile: np.ndarray; k: np.ndarray
def extract_features(window: Window, cfg: ProcessingConfig) -> FeatureVector

# baseline.py
@dataclass class Baseline: link_id; calibration_id; feature_median: dict[str,float]; feature_scale: dict[str,float]; per_subcarrier_median: dict[str, np.ndarray]; profile: np.ndarray; k: np.ndarray; n_windows; duration_s; layout_id; hardware_signature; config_version
    def to_dict(self) -> dict; @classmethod def from_dict(cls, d) -> "Baseline"
class BaselineRecorder:
    def __init__(self, link_id: str, cfg: DetectionConfig): ...
    def add(self, fv: FeatureVector, quality: QualityReport) -> None
    def finalize(self, *, calibration_id: str, hardware_signature: str, config_version: str) -> tuple[Baseline | None, list[str]]  # (None, reasons) if rejected

# detector.py
class MotionDetector:
    def __init__(self, link_id: str, cfg: DetectionConfig, config_version: str): ...
    def set_baseline(self, baseline: Baseline | None) -> None
    def set_calibrating(self, on: bool) -> None
    def update(self, fv: FeatureVector | None, quality: QualityReport, provenance: Provenance, now_ns: int) -> ActivityResult
    def tick(self, now_ns: int, last_frame_age_s: float | None, provenance: Provenance) -> ActivityResult | None  # staleness

# pipeline.py
class ProcessingEngine:
    def __init__(self, cfg: AppConfig): ...
    def on_event(self, ev: SourceEvent) -> None            # consumer registered on AcquisitionManager
    def step(self, now_ns: int | None = None) -> list[ActivityResult]   # compute due windows + staleness
    def latest(self) -> dict[str, ActivityResult]
    def history(self, link_id: str, seconds: float) -> list[ActivityResult]
    def feature_history(self, link_id: str, seconds: float) -> list[FeatureVector]
    def signal_snapshot(self, link_id: str, seconds: float) -> dict   # see SignalSnapshot below
    def start_baseline(self, link_ids: list[str] | None = None) -> None
    def stop_baseline(self) -> dict[str, tuple[Baseline | None, list[str]]]
    def cancel_baseline(self) -> None
    def set_baselines(self, baselines: dict[str, Baseline], calibration_id: str | None) -> None
    def hardware_signature(self) -> str | None             # from channel/layout/firmware identity/link set
    def reset(self, session_id: str, source_mode: SourceMode) -> None
```

### `roomsense/storage/`, `roomsense/inference/zone/`, `roomsense/validation/`

```python
# storage/db.py (sqlite3, WAL, secure_delete=ON, schema migrations table)
class Database:
    def __init__(self, path: Path): ...;  def close(self) -> None
    # sessions, recordings, consents, calibrations(+baselines json), events (labels), activity_log,
    # validation_runs, zone_models, room_versions: add_*/get_*/list_*/delete_* methods
    def delete_recording(self, recording_id) -> bool   # + its label events + its consent once no recording uses it
    def checkpoint(self, mode: str = "TRUNCATE") -> tuple[int, int, int]   # PRAGMA wal_checkpoint: (busy, log, done)
# storage/recordings.py
class Recorder:   # opt-in; refuses to start without a consent record; bounded bytes/seconds/total quota
    def start(self, *, consent: ConsentRecord, label: str, scenario: str | None, session_id: str,
              source_mode: SourceMode, notes: str | None = None, max_seconds: float | None = None) -> RecordingInfo
    def write(self, frame: CsiFrame) -> None   # thread-safe; auto-stops at limits with status TRUNCATED_LIMIT
    def stop(self) -> RecordingInfo | None
def iter_recording(path: Path) -> Iterator[CsiFrame]
def quota_used_bytes(data_dir) -> int   # recordings + export zips (incl. .partial): what max_total_recording_bytes limits
def purge_recording(db, data_dir, recording_id, *, registry=None) -> RecordingDeletion
    # RecordingDeletion(recording_id, deleted, removed_models: tuple[str, ...], wal_checkpoint_complete)
    # removes: zone models trained on it (first), the file, its exports and hidden .partial exports,
    # the DB rows (recording, label events, consent once unused), then wal_checkpoint(TRUNCATE)
def delete_recording(db, data_dir, recording_id) -> bool   # purge_recording(...).deleted
# storage/exports.py
def export_recording(db, data_dir, recording_id, *, now_ns=None, max_total_bytes=None, reserved_bytes=0) -> Path
    # zip: manifest.json, frames.jsonl.gz, events.csv, PROVENANCE.txt. With max_total_bytes the export is refused
    # (ExportError.code == "QUOTA_EXCEEDED") if recordings + exports + this zip (minus the export it replaces)
    # + reserved_bytes (what an active recording may still write) would exceed it. Other refusals: "EXPORT_REFUSED".

# inference/zone/criteria.py  -- loads configs/zone_enablement.toml (hashed => criteria_version)
# inference/zone/dataset.py   -- recordings -> per-window feature matrices, labels, session ids (via replay through the same pipeline)
# inference/zone/train.py     -- session-level splits; fit scaler/selection on train only; tune abstention on validation; test once
# inference/zone/evaluate.py  -- counts, class balance, confusion matrix, per-zone errors, abstention, later-session performance
# inference/zone/predictor.py -- ZonePredictor.predict(features_by_link, room_hash, hw_signature) -> ZonePrediction (DISABLED/ABSTAIN/ESTIMATE)
# inference/zone/registry.py  -- ZoneModelRegistry(data_dir, db).save/load/list_bindings/delete;
#                                models_trained_on(recording_id) / delete_models_trained_on(recording_id) -> [model ids]
#                                (read from each model's report: dataset.sessions[].recording_id and the split assignment)

# validation/metrics.py -- false alarms/hour (+exact Poisson CI), event recall (+Wilson CI), latency, missed events
# validation/protocol.py -- the through-wall scenario list
# validation/report.py  -- markdown/JSON report; "NOT MEASURED" wherever there is no data
```

### `roomsense/inference/pose/` and `roomsense/capabilities.py`

```python
class PoseModel(Protocol): manifest: dict; def infer(self, window) -> dict   # EXPERIMENTAL outputs only
def evaluate_pose_gate(manifest_path: Path, runtime_hw: dict) -> PoseStatus  # DISABLED + missing requirements
def build_capabilities(ctx: CapabilityContext) -> list[CapabilityStatus]
```

## HTTP API (FastAPI, bound to 127.0.0.1 by default)

Access rules (`roomsense/api/security.py`, `roomsense/api/app.py`):

* **Bind and token.** A non-loopback bind requires `server.allow_non_loopback = true`
  *and* the `ROOMSENSE_API_TOKEN` environment variable. When a token is configured (on any
  bind address), every `/api/*` request must send `Authorization: Bearer <token>`
  (401 `UNAUTHORIZED` otherwise) and the WebSocket must present it (see "WebSocket auth"
  below). A `ROOMSENSE_API_TOKEN` that is **set but unusable** (empty, whitespace only,
  shorter than 16 characters, or containing whitespace, control or non-ASCII characters)
  makes the server refuse to start on **every** bind address, loopback included
  (`StartupRefused`; `roomsense serve` exits with code 2). The token is never printed or
  logged, and never read from query strings.
* **Host header (DNS rebinding).** While bound to loopback, a request whose `Host` is not
  a loopback name (`127.0.0.1`, `localhost`, `::1`, with any port) gets **421**
  `HOST_NOT_ALLOWED` (WebSocket: close 1008).
* **Origin.** WebSocket handshakes and `POST`/`PUT`/`PATCH`/`DELETE` requests that carry a
  foreign `Origin` are refused: **403** `ORIGIN_NOT_ALLOWED` (WebSocket: close **1008**).
  Allowed origins are the server itself (the `Origin` authority equals the `Host` header,
  same host:port) and `server.cors_dev_origins` (the Vite dev server). `null` origins and
  non-HTTP schemes are refused. Requests without `Origin` (curl, the CLI, scripts) are
  unaffected; the token rule still applies. Implemented in
  `roomsense.api.security.origin_allowed` and `SecurityMiddleware`.
* **CORS** is allowed only for `server.cors_dev_origins`; a refused preflight answers 400
  `CORS_REJECTED`.
* **OpenAPI.** `/api/openapi.json` is served. The interactive docs (`/docs`, `/redoc`) are
  disabled, because they load scripts from a CDN and this app makes no external requests.
* **Literal-true acknowledgements.** `acknowledge_simulated`, `confirm_room_empty` and
  `consent.all_participants_consented` are strict booleans: only JSON `true` counts. Any
  other type (`"true"`, `1`, `"yes"`) is a 422 `VALIDATION_ERROR`; `false` or a missing
  optional flag is refused by the runtime with an explanation
  (`SIMULATION_NOT_ACKNOWLEDGED`, `ROOM_NOT_CONFIRMED_EMPTY`, `CONSENT_INVALID`, all 422).
* **Receivers sent over HTTP.** `POST /api/source/live` with a `receivers` body may only
  name ports that are configured in `[[acquisition.receivers]]` or currently listed by
  `GET /api/serial/ports`; any other port is 422 `PORT_NOT_AVAILABLE`, so the API cannot
  open arbitrary device files. Ports are never guessed.

**Error body.** Every error response is JSON
`{"detail": "CODE: human text", "code": "CODE"}` (match on `code`, or on the `detail`
prefix). That covers runtime refusals (`OperationRefused`), route errors
(`PORT_NOT_AVAILABLE`, `SERIAL_ENUMERATION_FAILED`, 503 `NOT_READY` while the runtime starts
or stops), framework errors (404 `NOT_FOUND`, 405 `METHOD_NOT_ALLOWED` with its `Allow`
header), security refusals (401/403/421) and 500 `INTERNAL_ERROR` (exception type only).
Request-validation errors (422) are
`{"detail": "VALIDATION_ERROR: <field path>: <message>; ... (+n more)", "code": "VALIDATION_ERROR",
"errors": [{type, loc, msg}, ...]}`; field paths look like `body.consent.participant_count`
or `query.seconds`, and the rejected input values are never echoed back.

| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/health` | `{status, version, schema_version, uptime_s, source_state, bind_host, websocket_support}` (`websocket_support` is false when uvicorn has no WebSocket library, i.e. `/api/ws` cannot be served) |
| GET | `/api/status` | `SystemStatus` |
| WS | `/api/ws` | pushes `{"type":"status","data":SystemStatus}` at `websocket_push_hz`, plus `{"type":"signal","link_id":..,"data":SignalSnapshot}` for each link |
| GET | `/api/signal?link_id=&seconds=60` | `SignalSnapshot` (404 `NO_SOURCE` / `UNKNOWN_LINK`) |
| GET | `/api/capabilities` | `CapabilityStatus[]` (A, B, C, D; same as `SystemStatus.capabilities`) |
| GET | `/api/capabilities/unsupported` | `UnsupportedCapability[]` `{id, claim, reason}` (claims the app never makes) |
| GET | `/api/serial/ports` | `[{device, description, hwid, vid, pid, likely_usb_uart_bridge}]` (listed, never opened; 503 `SERIAL_ENUMERATION_FAILED`) |
| GET | `/api/source/receivers` | the configured receivers: `[{receiver_id, port, baud, input_format, transmitter_id, transmitter_mac, declared_chip, declared_board, ltf_config, link_id}]` (exactly these keys; nothing else from the config) |
| POST | `/api/source/live` | `{receivers?: ReceiverConfig[]}` → `SystemStatus` (409 `NO_RECEIVERS_CONFIGURED`; 422 `PORT_NOT_AVAILABLE`, see above) |
| POST | `/api/source/replay` | `{recording_id, speed?}` → `SystemStatus` (404 `RECORDING_NOT_FOUND`, 409 `RECORDING_ACTIVE`) |
| POST | `/api/source/simulation` | `{scenario, seed?, acknowledge_simulated: true}` → `SystemStatus` (422 `SIMULATION_NOT_ACKNOWLEDGED` without the acknowledgement) |
| POST | `/api/source/stop` | → `SystemStatus` |
| GET | `/api/simulation/scenarios` | `[{name, duration_s, description, rate_hz, links, simulated: true}]` |
| GET | `/api/calibration` | `{active, history, in_progress, walk_test_active, last_walk_test, note}` |
| POST | `/api/calibration/baseline/start` | `{link_ids?, confirm_room_empty: true}` |
| POST | `/api/calibration/baseline/stop` | → `CalibrationRecord` (valid or rejected with reasons) |
| POST | `/api/calibration/baseline/cancel` | → `{cancelled}` |
| POST | `/api/calibration/walk-test/start` / `stop` | `{link_ids?}` → `{started, note}` / `WalkTestReport` |
| POST | `/api/calibration/invalidate` | `{reason}` → `{invalidated, reason}` |
| GET | `/api/room` | `RoomGeometry` (the EXAMPLE room if the user has not provided one) |
| PUT | `/api/room` | `RoomGeometry` (the provenance is forced to USER_PROVIDED) → `{room, invalidated}` |
| GET | `/api/room/example` | EXAMPLE `RoomGeometry` |
| GET | `/api/recordings` | `RecordingInfo[]` |
| GET | `/api/recordings/consent-statement` | `{version, text}`: the statement the UI shows; the server fills it into the consent record |
| POST | `/api/recordings/start` | `{consent:{all_participants_consented:true, participant_count, purpose, statement_version}, label, scenario?, notes?, max_seconds?}` → `RecordingInfo` (409 `QUOTA_EXCEEDED` once recordings + exports reach `max_total_recording_bytes`) |
| POST | `/api/recordings/stop` | → `RecordingInfo` |
| DELETE | `/api/recordings/{id}` | → `{deleted: true, removed_models: [model_id, ...]}`: deletes the file, its exports (and hidden `.partial` leftovers), its label events, its consent record once no other recording uses it, and **every zone model trained on it** (files + DB rows, listed in `removed_models`), then truncates the SQLite WAL. 409 `RECORDING_ACTIVE` / `RECORDING_IN_USE` (being replayed, or being read by a zone training), 404 `RECORDING_NOT_FOUND` |
| GET | `/api/recordings/{id}/export` | zip download. Exports count toward `max_total_recording_bytes`; 409 `QUOTA_EXCEEDED` if this one would exceed it (what an active recording may still write is reserved), 409 `EXPORT_REFUSED` otherwise |
| GET/POST | `/api/events` | labelled events `{label, kind: MARK/START/END, t_unix_ns?, notes?}` (GET: `?session_id=&limit=`) |
| GET | `/api/validation/protocol` | scenario list |
| GET | `/api/validation/runs` | `ValidationRun[]` |
| POST | `/api/validation/runs`, `/api/validation/runs/{id}/stop` | start/stop a validation run with placement metadata → `ValidationRun` |
| GET | `/api/validation/report` | JSON report (`/api/validation/report.md` for markdown) |
| GET | `/api/zone/status` | `{state, reasons, model_id, criteria_version, criteria, report}` |
| POST | `/api/zone/train` | `{sessions:[{recording_id, label, split?}]}` → training/evaluation report (the model is only enabled if the criteria pass) |
| GET | `/api/pose/status` | `PoseStatus` |
| GET | `/api/hardware?refresh=true` | hardware inspection JSON (non-destructive) with `cache_age_s`; cached ~30 s, `refresh=true` re-inspects; 503 `HARDWARE_MODULE_UNAVAILABLE`, never made-up data |

`SignalSnapshot` (timestamps in Unix **milliseconds**; `null` marks gaps, which the UI must not bridge):

```json
{
  "link_id": "tx1->rx1", "source_mode": "LIVE", "seconds": 60,
  "score": {"t": [..], "v": [..|null], "state": [..]},
  "enter_threshold": 4.0, "exit_threshold": 2.5,
  "rate_hz": {"t": [..], "v": [..|null]},
  "rssi_dbm": {"t": [..], "v": [..|null]},
  "amplitude": {"t": [..], "k": [..], "v": [[..|null], ..]},
  "latest_profile": {"t": ..|null, "k": [..], "amp": [..|null]},
  "gaps": [{"start": .., "end": ..}]
}
```

## Runtime contract (`roomsense/runtime.py`)

```python
class OperationRefused(Exception):     # the API answers {"detail": "CODE: detail", "code": code} with http_status
    def __init__(self, code: str, detail: str, http_status: int = 409): ...
class AppRuntime:                      # owns DB, manager, engine, recorder, zone predictor, pose gate, room
    def __init__(self, cfg: AppConfig, *, serial_factory=None, clock=None, background_processing=True): ...
    def start(self) -> None            # idempotent; crash recovery, then the processing thread
    def shutdown(self) -> None         # idempotent: stop source (closes ports), finalise recording, stop thread, close DB
    def pump(self, max_events=None) -> int   # background_processing=False only: process queued events now
    # start_live/start_replay/start_simulation/stop_source, calibration, recordings, events, validation,
    # zone training, build_status() -> SystemStatus; every public method is thread-safe
@contextmanager
def data_dir_lock(data_dir: Path) -> Iterator[Path]   # the same lock, for tools that run without a runtime
```

* **Data folder.** The metadata database is `<storage.data_dir>/roomsense.sqlite3`
  (default `data/roomsense.sqlite3`); recordings are `data/recordings/<id>.jsonl.gz`,
  exports `data/exports/`, zone models `data/models/`. One process per data folder: the
  runtime holds an exclusive lock on `data/.roomsense.lock` for its lifetime. A second
  runtime (a second server, `roomsense capture`) is refused with **409 `DATA_DIR_LOCKED`**;
  `roomsense recordings delete|export` and `roomsense zone-train` take the same lock and
  refuse while a server runs ("stop the server or use the UI", exit code 1).
  `roomsense recordings list` and `validate-report` only read and do not need it.
* **Crash recovery.** On `start()`, validation runs left `RUNNING` by a crashed process become
  `ABORTED` with zero duration (they never count as evidence) and sessions without an end
  are closed. Recordings left `RECORDING` are marked `ERROR` (interrupted) when the runtime's
  recorder is constructed.
* **Host queue policy.** The acquisition consumer only enqueues into a bounded queue
  (`acquisition.frame_queue_size`). A LIVE source never waits: when the queue is full the
  newest event is dropped and counted per link (`HOST_QUEUE_DROPS` note). Replay and
  simulation wait up to 1 s (`NON_LIVE_PUT_TIMEOUT_S`) for space before dropping and
  counting in the same way. Nothing is dropped silently.
* **Processing cadence.** One processing thread drains the queue and calls
  `ProcessingEngine.step()` whenever the processing clock (host clock, or the newest data
  timestamp when a non-realtime simulation runs ahead) advanced by `0.05 * hop_s`
  (`STEP_FRACTION`), i.e. every 0.05·hop_s of data time.
* **`SystemStatus.hardware_required`** = NOT (the active source is LIVE AND at least one
  live link has delivered measured frames with a documented CSI layout within
  `acquisition.stale_after_s` and has not reported a disconnect since;
  `AcquisitionManager.live_layout_links()`). It is true with no source, under replay and
  simulation, before the first documented frame, and again after a board is unplugged. It
  is not sticky for the process.
* **Stale results.** `SystemStatus.activity` shows the newest result per link of the
  current session only. A result whose `provenance.computed_at_unix_ns` is older than
  `detection.clear_stale_after_s` (processing stalled or stopped) is replaced by state
  `UNKNOWN`, reason `PROCESSING_STALLED: ...`, no score, quality `UNAVAILABLE`, with its
  original provenance; a `PROCESSING_STALLED` note says how many. An old MOTION /
  NO_MOTION decision is never shown as current. (Keep `hop_s` well below
  `clear_stale_after_s`.)
* **Notes.** Notes about a session (`RECORDING_STOPPED`, `HOST_QUEUE_DROPS`,
  `END_OF_STREAM`, `FRAMES_REJECTED_SOURCE_MISMATCH`) are cleared when a new source or session
  starts. Runtime-health notes (`PROCESSING_ERRORS`, `PROCESSING_STOPPED`,
  `ACTIVITY_LOG_INCOMPLETE`) and configuration notes (`ROOM_EXAMPLE`, evidence errors) stay.
* **Simulated data.** `SystemStatus.simulated` and `CapabilityContext.simulated` are true for
  SIMULATION and for a REPLAY of simulated data. For such a replay `source_banner` stays
  `"RECORDED REPLAY"` while `simulated` is true, and the UI shows both (the replay banner
  and the SIMULATED DATA banner). Capability texts never call simulated data "recorded
  measurements", and zone output stays DISABLED.
* **Deleting a recording** also deletes every zone model trained on it and refuses
  (`RECORDING_IN_USE`) while it is replayed or read by a zone training in progress.

## Conventions settled during implementation

### Link events and end of stream

* `LinkEvent.data` for each kind:

  | kind | data |
  |---|---|
  | `PARSE_ERROR` | `{code, count, summary?}`. Always sum `count`, because events are rate-limited to 20/s per receiver and aggregated. |
  | `HELLO` | the `HelloRecord` fields (including `rate_hz`), `notices` (`IDENTITY_CHANGED`, `DECLARED_CHIP_MISMATCH`, `LTF_CONFIG_MISMATCH`, `TX_MAC_FILTER_MISMATCH`) and `changed_fields` |
  | `STAT` | the `StatRecord` fields |
  | `DIAGNOSTIC` | `{looks_like?}` or `{suppressed: n}` |
  | `RECONNECTING` | `{delay_s}` |
  | replayed events | `{replayed: true, recorded_host_unix_ns}` |
  | simulated events | `{simulated: true}` |

* `EndOfStream.reason` is one of `END_OF_RECORDING`, `END_OF_SCENARIO` or `ERROR: <detail>`.
  The manager maps `ERROR…` to `SourceState.ERROR` and everything else to `FINISHED`.

### Event delivery and the host queue

* `AcquisitionManager` delivers events synchronously on the source thread, one event
  at a time. The runtime therefore puts events on its own bounded queue, sized by
  `acquisition.frame_queue_size`, and processes them on a single processing thread.
* If that queue ever overflows, the dropped events are **counted and reported**
  (`HOST_QUEUE_DROPS` notes), never dropped silently. A live source drops the newest event
  at once; replay and simulation wait up to 1 s first (see "Runtime contract").

### Simulated flag

* `SystemStatus.simulated` is true when the active source mode is `SIMULATION`.
* It is also true when `active.describe()["simulated"]` is true. That covers a REPLAY of
  a simulated recording, and a recording of such a replay. `source_banner` then stays
  `"RECORDED REPLAY"`; the UI shows it together with the SIMULATED DATA banner.
* `CapabilityContext.simulated` carries the same value into the capability builder.

### `SignalSnapshot`

* `amplitude.v` is indexed `[time][subcarrier]`, aligned with `amplitude.t` and
  `amplitude.k`. A gap marker is a row of nulls.
* `score`, `rate_hz` and `rssi_dbm` share the results time axis.
* `score.state` is null at gap markers.

### Reason strings

* Reasons have the form `"CODE: human text"`, with the primary code first.
  Match on the prefix.

### Additive public APIs (beyond the contract above)

* **Synthetic:** `with_seed(scenario, seed)`; `SyntheticScenario.{description, node_positions, room_size_m, zones, params}`;
  `SYNTHETIC_ZONES`; `zone_session_scenario(label, *, seed, duration_s=60, links=None, rate_hz=25, environment_seed=...)`;
  `generate_frames(..., start_unix_ns=None, start_monotonic_ns=None)`.
* **Manager:** `.mode`, `.detail`, `.remove_consumer()`, `.live_layout_links(now_ns=None) -> list[str]`
  (links of the active LIVE source with a measured, documented-layout frame within
  `stale_after_s` and no disconnect since; backs `hardware_required`).
* **Replay:** `original_is_synthetic(path)`.
* **Processing:** `ProcessingEngine(cfg, *, clock_ns, clock_unix_ns)`; `start_walk_test(link_ids=None)`; `stop_walk_test()`
  returns `{link_id: {max_score, motion_window_fraction, windows, motion_windows, undecided_windows, offline_windows, detected, reasons}}`;
  `calibration_progress()`, `is_calibrating()`, `baseline_status()`, `drift_status()`, `link_stats()`, `engine_stats()`,
  `link_ids()`, `invalidate_baselines(reason, link_ids=None)`; `convert_frame()` returns a sample or a `FrameRejection`.
* **Offline replays:** windows follow the data timeline. Call `ProcessingEngine.step()` at least once per `hop_s` of data time.

### Storage, validation and pose (as implemented)

* **`Recorder.start`** also accepts `link_ids`, `config_version` and `original_source_mode`.
  `write()` and `write_event()` return bool.
* **Other recorder APIs:** `check_limits()`, `last_finished`, `status()`, `recover_interrupted()`,
  `iter_recording_events(path)`.
* **`RecordingRefused(code, detail)`** codes: `ALREADY_RECORDING`, `CONSENT_INVALID`,
  `INVALID_ARGUMENT`, `QUOTA_EXCEEDED`, `RECORDING_ACTIVE`.
* Create one `Recorder` per data directory per process. Its constructor marks
  interrupted recordings as ERROR.
* **Session and recording IDs** must match `^[A-Za-z0-9_-]{1,64}$`.
* **Validation:** `build_validation_report(db, criteria_path, *, now_ns=None, software_tested=None) -> dict`,
  `render_markdown(report)`, `protocol_as_dict()`.
  * START/END event labels: `MOVING`, `STILL`, `OUTSIDE_MOTION`, `DOOR`, `INTERFERENCE`,
    `DISCONNECT`, `DEGRADED`.
  * `activity_log` needs **one row per ActivityResult** (`Database.add_activity_result`),
    because observation time is derived from it.
  * Cache the report; do not rebuild it at the status push rate.
* **Pose gate:** `evaluate_pose_gate(manifest_path, runtime_hw, *, data_dir=None, available_backends=None) -> PoseStatus`.
  * Build `runtime_hw` with `runtime_hardware_profile(*, layout_id, packet_format, measured_rate_hz, links, phase_available=False)`.
  * Pass `data_dir=cfg.storage.resolved_data_dir()`.
  * Never pass `available_backends` in production.
* **Capabilities:** `CapabilityContext(...)` and `build_capabilities(ctx)`.
  * Load the evidence with `load_verification_evidence()`. On `EvidenceError`, pass `{}`.
  * `UNSUPPORTED_CAPABILITIES` is exposed as `SystemStatus.unsupported_capabilities`
    and as `GET /api/capabilities/unsupported`.
* **Model research:** `docs/MODEL_COMPATIBILITY.md`.

### Frontend expectations the API must honour

* **WebSocket auth in LAN mode.** Browsers cannot set an `Authorization` header on a
  WebSocket, so the UI sends `Sec-WebSocket-Protocol: roomsense.v1, bearer.<token>`.
  When a token is configured, the server takes the token from that header, compares it
  in constant time, and accepts with `subprotocol="roomsense.v1"`. Tokens are never put
  in query strings, because those end up in access logs. On loopback without a token,
  no subprotocol is required.
* **`POST /api/validation/runs`.** Body: `{scenario_id, placement, wall_description,
  channel: int|null, conditions, notes}`. Returns a `ValidationRun` with its `run_id`.
  `POST /api/validation/runs/{run_id}/stop` returns the updated `ValidationRun`.
* **`POST /api/recordings/start`.** The body carries consent as
  `{all_participants_consented: true, participant_count, purpose, statement_version:
  "consent-v1"}`, without `statement_text`. The server fills the text in from its own
  statement table. The UI shows the server's text via
  `GET /api/recordings/consent-statement`, which returns `{version, text}`.
* **`GET /api/hardware`.** Returns top-level keys `detected` and `recommended`, plus any
  further detail.
* **`GET /api/zone/status`.** The report names the confusion matrix `confusion_matrix`
  (rows = true class, columns = predicted class) with a sibling `labels` list, and
  includes `abstention_rate`.
* **`POST /api/calibration/walk-test/stop`.** Returns `WalkTestReport`.

### Hardware inspection (`roomsense/hardware.py`)

The inspection is non-destructive. It never opens serial ports, and it runs only
commands on an allow-list, each with a timeout.

* `inspect_host(*, redact=True) -> dict` with `format="roomsense-hardware-report-v1"`. Top-level keys:
  `generated_at_utc`, `generator`, `scope`, `identifiers_redacted`, `host`, `virtualisation`,
  `detected{serial_ports, serial_port_error, serial_permissions, network_interfaces, wifi_interface_listings}`,
  `software`, `pc_csi_research_paths`, `recommended`, `commands_run`, `errors`, `assessment`.
* `assess(report) -> {csi_path_available, csi_path_confirmed (always false; only RSHELLO in LIVE mode confirms), hardware_required, confidence, candidate_ports, reasons, next_steps}`.
* `render_markdown(report) -> str`.
* `GET /api/hardware` calls it from a sync endpoint, so it runs in the threadpool.
