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
# storage/db.py (sqlite3, WAL, schema migrations table)
class Database:
    def __init__(self, path: Path): ...;  def close(self) -> None
    # sessions, recordings, consents, calibrations(+baselines json), events (labels), activity_log,
    # validation_runs, zone_models, room_versions: add_*/get_*/list_*/delete_* methods
# storage/recordings.py
class Recorder:   # opt-in; refuses to start without a consent record; bounded bytes/seconds/total quota
    def start(self, *, consent: ConsentRecord, label: str, scenario: str | None, session_id: str,
              source_mode: SourceMode, notes: str | None = None, max_seconds: float | None = None) -> RecordingInfo
    def write(self, frame: CsiFrame) -> None   # thread-safe; auto-stops at limits with status TRUNCATED_LIMIT
    def stop(self) -> RecordingInfo | None
def iter_recording(path: Path) -> Iterator[CsiFrame]
def delete_recording(db, data_dir, recording_id) -> bool
# storage/exports.py
def export_recording(db, data_dir, recording_id) -> Path   # zip: manifest.json, frames.jsonl.gz, events.csv, PROVENANCE.txt

# inference/zone/criteria.py  -- loads configs/zone_enablement.toml (hashed => criteria_version)
# inference/zone/dataset.py   -- recordings -> per-window feature matrices, labels, session ids (via replay through the same pipeline)
# inference/zone/train.py     -- session-level splits; fit scaler/selection on train only; tune abstention on validation; test once
# inference/zone/evaluate.py  -- counts, class balance, confusion matrix, per-zone errors, abstention, later-session performance
# inference/zone/predictor.py -- ZonePredictor.predict(features_by_link, room_hash, hw_signature) -> ZonePrediction (DISABLED/ABSTAIN/ESTIMATE)

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

A non-loopback bind requires the `ROOMSENSE_API_TOKEN` environment variable.
Every `/api/*` request must then send `Authorization: Bearer <token>`.

| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/health` | `{status, version, schema_version, uptime_s, source_state, bind_host}` |
| GET | `/api/status` | `SystemStatus` |
| WS | `/api/ws` | pushes `{"type":"status","data":SystemStatus}` at `websocket_push_hz`, plus `{"type":"signal","link_id":..,"data":SignalSnapshot}` for each link |
| GET | `/api/signal?link_id=&seconds=60` | `SignalSnapshot` |
| GET | `/api/serial/ports` | `[{device, description, hwid, vid, pid, likely_usb_uart_bridge}]` |
| POST | `/api/source/live` | `{receivers?: ReceiverConfig[]}` → `SystemStatus` (409 if the receivers are not configured) |
| POST | `/api/source/replay` | `{recording_id, speed?}` → `SystemStatus` |
| POST | `/api/source/simulation` | `{scenario, seed?, acknowledge_simulated: true}` → `SystemStatus` (422 without the acknowledgement) |
| POST | `/api/source/stop` | → `SystemStatus` |
| GET | `/api/simulation/scenarios` | `[{name, duration_s, description}]` |
| GET | `/api/calibration` | `{active, history, in_progress}` |
| POST | `/api/calibration/baseline/start` | `{link_ids?, confirm_room_empty: true}` |
| POST | `/api/calibration/baseline/stop` | → `CalibrationRecord` (valid or rejected with reasons) |
| POST | `/api/calibration/baseline/cancel` | |
| POST | `/api/calibration/walk-test/start` / `stop` | → `WalkTestReport` |
| POST | `/api/calibration/invalidate` | `{reason}` |
| GET | `/api/room` | `RoomGeometry` (the EXAMPLE room if the user has not provided one) |
| PUT | `/api/room` | `RoomGeometry` (the provenance is forced to USER_PROVIDED) → `{room, invalidated}` |
| GET | `/api/room/example` | EXAMPLE `RoomGeometry` |
| GET | `/api/recordings` | `RecordingInfo[]` |
| POST | `/api/recordings/start` | `{consent:{all_participants_consented:true, participant_count, purpose, statement_version}, label, scenario?, notes?, max_seconds?}` |
| POST | `/api/recordings/stop` | → `RecordingInfo` |
| DELETE | `/api/recordings/{id}` | → `{deleted:true}` |
| GET | `/api/recordings/{id}/export` | zip download |
| GET/POST | `/api/events` | labelled events `{label, kind: MARK/START/END, t_unix_ns?, notes?}` |
| GET | `/api/validation/protocol` | scenario list |
| POST | `/api/validation/runs`, `/api/validation/runs/{id}/stop` | start/stop a validation run with placement metadata |
| GET | `/api/validation/report` | JSON report (`/api/validation/report.md` for markdown) |
| GET | `/api/zone/status` | `{state, reasons, criteria, report?}` |
| POST | `/api/zone/train` | `{sessions:[{recording_id, label, split?}]}` → training/evaluation report (the model is only enabled if the criteria pass) |
| GET | `/api/pose/status` | `PoseStatus` |
| GET | `/api/hardware` | hardware inspection JSON (non-destructive) |

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
