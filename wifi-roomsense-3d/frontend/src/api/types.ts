/**
 * TypeScript mirror of the API-facing pydantic models in
 * backend/roomsense/schemas.py (and ReceiverConfig from config.py).
 *
 * Keep in sync BY HAND with the backend. Conventions carried over:
 *  - pydantic serialises enums as their string values -> string-literal unions.
 *  - pydantic serialises every field (defaults included), so fields that have
 *    a default in Python are still required here; `null` means
 *    "unavailable / not measured" and must never be replaced by a number.
 *  - wall-clock times are integer nanoseconds since the Unix epoch unless the
 *    field name says otherwise (SignalSnapshot uses milliseconds).
 *  - floor coordinates are metres: x east, y north, z up (see lib/coords.ts).
 */

// ---------------------------------------------------------------------------
// Source / provenance
// ---------------------------------------------------------------------------

export type SourceMode = 'LIVE' | 'REPLAY' | 'SIMULATION';

/** Exact banner strings the backend sends in SystemStatus.source_banner. */
export type SourceBannerText = 'LIVE MEASUREMENTS' | 'RECORDED REPLAY' | 'SIMULATION' | 'NO SOURCE';

export const SOURCE_BANNER_TEXTS: readonly SourceBannerText[] = [
  'LIVE MEASUREMENTS',
  'RECORDED REPLAY',
  'SIMULATION',
  'NO SOURCE',
];

export type InputFormat =
  | 'esp-csi-upstream-classic-v1'
  | 'esp-csi-upstream-c5c6-v1'
  | 'roomsense-rscsi-v1'
  | 'synthetic-v1';

export const INPUT_FORMATS: readonly InputFormat[] = [
  'roomsense-rscsi-v1',
  'esp-csi-upstream-classic-v1',
  'esp-csi-upstream-c5c6-v1',
];

export type QualityFlag =
  | 'SYNTHETIC'
  | 'REPLAYED'
  | 'FIRST_WORD_INVALID'
  | 'UNKNOWN_LAYOUT'
  | 'UNDOCUMENTED_LAYOUT_ASSUMPTION'
  | 'MAC_NOT_CONFIGURED'
  | 'LAYOUT_MISMATCH'
  | 'GAIN_COMPENSATED_UPSTREAM'
  | 'ALL_ZERO_CSI'
  | 'SATURATED_VALUES'
  | 'COUNTER_GAP'
  | 'COUNTER_ROLLOVER'
  | 'COUNTER_RESET'
  | 'TIMESTAMP_ROLLOVER'
  | 'TIMESTAMP_NON_MONOTONIC'
  | 'HOST_TIMESTAMP_UNAVAILABLE'
  | 'DEVICE_TIMESTAMP_UNAVAILABLE'
  | 'RX_STATE_ERROR'
  | 'FIRMWARE_QUEUE_DROPS';

// ---------------------------------------------------------------------------
// Activity detection
// ---------------------------------------------------------------------------

export type ActivityState =
  | 'UNKNOWN'
  | 'CALIBRATING'
  | 'NO_MOTION_DETECTED'
  | 'MOTION_DETECTED'
  | 'SENSOR_OFFLINE';

export type QualityLevel = 'GOOD' | 'DEGRADED' | 'BAD' | 'UNAVAILABLE';

export interface QualityReport {
  level: QualityLevel;
  packet_rate_hz: number | null;
  expected_rate_hz: number | null;
  loss_fraction: number | null;
  max_gap_s: number | null;
  timing_jitter_ms: number | null;
  rssi_dbm_median: number | null;
  valid_subcarrier_fraction: number | null;
  frames_in_window: number;
  rejected_frames: number;
  /** Values are QualityFlag strings or other backend flag strings. */
  flags: string[];
}

export interface Provenance {
  source_mode: SourceMode;
  session_id: string;
  link_ids: string[];
  window_start_unix_ns: number | null;
  window_end_unix_ns: number | null;
  window_frame_count: number;
  config_version: string;
  model_version: string | null;
  calibration_id: string | null;
  computed_at_unix_ns: number;
  /** Age of the newest contributing frame at compute time (server side). */
  measurement_age_s: number | null;
}

/** Spread of the heuristic score. Not a confidence interval on presence. */
export interface ScoreUncertainty {
  method: string;
  low: number;
  high: number;
  n_subcarriers: number;
  n_frames: number;
}

export interface ActivityResult {
  link_id: string;
  state: ActivityState;
  /** Unitless heuristic (robust deviation from the quiet baseline). NOT a probability. */
  activity_score: number | null;
  enter_threshold: number;
  exit_threshold: number;
  /** Always null in this release: no statistically calibrated model exists. */
  calibrated_probability: null;
  uncertainty: ScoreUncertainty | null;
  quality: QualityReport;
  reasons: string[];
  provenance: Provenance;
}

// ---------------------------------------------------------------------------
// Capabilities
// ---------------------------------------------------------------------------

export type CapabilityId = 'A_ACQUISITION' | 'B_MOTION' | 'C_ZONE' | 'D_POSE';

export const CAPABILITY_IDS: readonly CapabilityId[] = ['A_ACQUISITION', 'B_MOTION', 'C_ZONE', 'D_POSE'];

export type CapabilityState =
  | 'ENABLED'
  | 'DISABLED'
  | 'HARDWARE_REQUIRED'
  | 'REQUIRES_CALIBRATION'
  | 'REQUIRES_VALIDATION'
  | 'UNSUPPORTED';

/** Evidence levels, kept separate on purpose. */
export interface VerificationStatus {
  software_tested: boolean;
  firmware_compiled: boolean;
  hardware_tested: boolean;
  through_wall_validated: boolean;
  notes: string[];
}

export interface CapabilityStatus {
  capability: CapabilityId;
  title: string;
  state: CapabilityState;
  reasons: string[];
  verification: VerificationStatus;
}

// ---------------------------------------------------------------------------
// Link / source status
// ---------------------------------------------------------------------------

export type SourceState = 'NO_SOURCE' | 'CONNECTING' | 'RUNNING' | 'DISCONNECTED' | 'FINISHED' | 'ERROR';

export interface LinkStatus {
  link_id: string;
  receiver_id: string;
  transmitter_id: string;
  connected: boolean;
  /** Age of the newest frame at server_time_unix_ns. */
  last_frame_age_s: number | null;
  /** Measured over the last few seconds (not the configured rate). */
  acquisition_rate_hz: number | null;
  frames_total: number;
  frames_rejected: number;
  parse_errors: number;
  firmware_drops: number | null;
  layout_id: string | null;
  channel: number | null;
  device: Record<string, unknown> | null;
  clock_offset_ms: number | null;
  clock_drift_ppm: number | null;
}

// ---------------------------------------------------------------------------
// Room geometry (always USER_PROVIDED or EXAMPLE; never Wi-Fi reconstructed)
// ---------------------------------------------------------------------------

export type GeometryProvenance = 'USER_PROVIDED' | 'EXAMPLE';

export interface Vec2 {
  x: number;
  y: number;
}

export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

export interface Wall {
  id: string;
  start: Vec2;
  end: Vec2;
  height_m: number;
  thickness_m: number;
  material: string | null;
  is_target_room_boundary: boolean;
}

export interface Door {
  id: string;
  wall_id: string;
  /** Distance from the wall start, metres. */
  offset_m: number;
  width_m: number;
  height_m: number;
}

export type NodeRole = 'TX' | 'RX' | 'ROUTER';

export const NODE_ROLES: readonly NodeRole[] = ['TX', 'RX', 'ROUTER'];

export interface SensorNode {
  id: string;
  role: NodeRole;
  label: string;
  /** User-measured mounting position. */
  position: Vec3;
  inside_target_room: boolean | null;
  device_mac: string | null;
}

export interface LinkDef {
  link_id: string;
  transmitter_id: string;
  receiver_id: string;
}

export type ZoneKind = 'TARGET_ROOM_ZONE' | 'OUTSIDE_TARGET_ROOM';

export interface Zone {
  id: string;
  label: string;
  polygon: Vec2[];
  kind: ZoneKind;
}

export interface RoomGeometry {
  geometry_id: string;
  provenance: GeometryProvenance;
  name: string;
  width_m: number;
  depth_m: number;
  height_m: number;
  walls: Wall[];
  doors: Door[];
  nodes: SensorNode[];
  links: LinkDef[];
  zones: Zone[];
  target_room_polygon: Vec2[];
  notes: string | null;
}

// ---------------------------------------------------------------------------
// Calibration
// ---------------------------------------------------------------------------

export type CalibrationKind = 'QUIET_BASELINE' | 'WALK_TEST';

export interface CalibrationRecord {
  calibration_id: string;
  kind: CalibrationKind;
  created_at_unix_ns: number;
  session_id: string;
  source_mode: SourceMode;
  link_ids: string[];
  hardware_signature: string;
  room_config_hash: string | null;
  processing_config_version: string;
  duration_s: number;
  frame_count: number;
  window_count: number;
  valid: boolean;
  invalidated_reason: string | null;
  summary: Record<string, unknown>;
}

/** Per-link walk-test summary (same-room check; NOT through-wall evidence). */
export interface WalkTestLinkReport {
  link_id: string;
  /** Heuristic score maximum (unitless), not a probability. */
  max_score: number | null;
  motion_window_fraction: number | null;
  windows: number;
  motion_windows: number;
  undecided_windows: number;
  offline_windows: number;
  detected: boolean;
  reasons: string[];
}

export interface WalkTestReport {
  session_id: string | null;
  source_mode: SourceMode | null;
  started_at_unix_ns: number | null;
  ended_at_unix_ns: number | null;
  links: WalkTestLinkReport[];
  note: string;
}

// ---------------------------------------------------------------------------
// Zone estimation (capability C) and pose (capability D)
// ---------------------------------------------------------------------------

export type ZoneState = 'DISABLED' | 'ABSTAIN' | 'ESTIMATE';

export interface ZonePrediction {
  state: ZoneState;
  zone_id: string | null;
  zone_label: string | null;
  /** Classifier scores; explicitly NOT claimed to be calibrated probabilities. */
  model_scores: Record<string, number>;
  /** Zone centroid for DISPLAY ONLY. */
  display_anchor: Vec2 | null;
  display_anchor_note: string;
  reasons: string[];
  model_id: string | null;
  criteria_version: string | null;
  provenance: Provenance | null;
}

export interface PoseStatus {
  enabled: boolean;
  label: 'EXPERIMENTAL';
  model_id: string | null;
  missing_requirements: string[];
  manifest: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Whole-system status
// ---------------------------------------------------------------------------

export type ThroughWallStatus = 'UNVERIFIED' | 'VALIDATED' | 'NOT_DISTINGUISHABLE';

/** A claim this system explicitly does NOT make (shown in the UI). */
export interface UnsupportedCapability {
  id: string;
  claim: string;
  reason: string;
}

export interface SystemStatus {
  server_time_unix_ns: number;
  /** null when no source is selected. */
  source_mode: SourceMode | null;
  source_banner: string;
  /** true => the UI must show the large SIMULATED DATA banner. */
  simulated: boolean;
  source_state: SourceState;
  source_detail: string | null;
  session_id: string | null;
  hardware_required: boolean;
  capabilities: CapabilityStatus[];
  links: LinkStatus[];
  activity: ActivityResult[];
  zone: ZonePrediction;
  pose: PoseStatus;
  calibration: CalibrationRecord | null;
  calibration_valid: boolean;
  calibration_detail: string | null;
  localization_status: string;
  through_wall_status: ThroughWallStatus;
  through_wall_detail: string | null;
  recording_active: boolean;
  recording_id: string | null;
  operating_scope: string;
  stale_clear_timeout_s: number;
  unsupported_capabilities: UnsupportedCapability[];
  notes: string[];
}

// ---------------------------------------------------------------------------
// Receiver configuration (backend/roomsense/config.py ReceiverConfig)
// ---------------------------------------------------------------------------

export type LtfConfig = 'lltf_only' | 'lltf_htltf_stbc' | 'c5_default';

export interface ReceiverConfig {
  receiver_id: string;
  /** Never auto-guessed: the user picks it. */
  port: string;
  baud?: number;
  input_format?: InputFormat;
  transmitter_id?: string;
  transmitter_mac?: string | null;
  declared_chip?: string | null;
  declared_board?: string | null;
  ltf_config?: LtfConfig | null;
  allow_undocumented_layout_assumption?: boolean;
}

// ---------------------------------------------------------------------------
// HTTP API payloads (docs/ARCHITECTURE.md "HTTP API")
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: string;
  version: string;
  schema_version: string;
  uptime_s: number;
  source_state: SourceState;
  bind_host: string;
}

export interface SerialPortInfo {
  device: string;
  description: string | null;
  hwid: string | null;
  vid: number | null;
  pid: number | null;
  likely_usb_uart_bridge: boolean;
}

export interface SimulationScenario {
  name: string;
  duration_s: number;
  description: string;
}

/** A time series; `v[i] === null` marks a gap that must not be bridged. */
export interface TimeSeries {
  /** Unix milliseconds. */
  t: number[];
  v: (number | null)[];
}

export interface GapInterval {
  /** Unix milliseconds. */
  start: number;
  end: number;
}

/** GET /api/signal and WS {"type":"signal"} payload. Timestamps in Unix ms. */
export interface SignalSnapshot {
  link_id: string;
  source_mode: SourceMode | null;
  seconds: number;
  score: TimeSeries & { state: (ActivityState | null)[] };
  enter_threshold: number | null;
  exit_threshold: number | null;
  rate_hz: TimeSeries;
  rssi_dbm: TimeSeries;
  /**
   * `v` is indexed [time][subcarrier]. A `null` row (or a null cell) marks a
   * gap that plots must not bridge (see lib/plotting.ts amplitudeSeries).
   */
  amplitude: { t: number[]; k: number[]; v: ((number | null)[] | null)[] };
  latest_profile: { t: number | null; k: number[]; amp: (number | null)[] };
  gaps: GapInterval[];
}

export type WsMessage =
  | { type: 'status'; data: SystemStatus }
  | { type: 'signal'; link_id: string; data: SignalSnapshot };

export interface ConsentPayload {
  all_participants_consented: true;
  participant_count: number;
  purpose: string;
  statement_version: string;
}

export interface RecordingStartRequest {
  consent: ConsentPayload;
  label: string;
  scenario?: string | null;
  notes?: string | null;
  max_seconds?: number | null;
}

/** Mirrors backend/roomsense/storage/models.py RecordingStatus. */
export type RecordingStatus = 'RECORDING' | 'COMPLETE' | 'TRUNCATED_LIMIT' | 'ERROR';

/** Mirrors backend/roomsense/storage/models.py RecordingInfo. */
export interface RecordingInfo {
  recording_id: string;
  session_id: string;
  created_at_unix_ns: number;
  ended_at_unix_ns: number | null;
  source_mode: SourceMode;
  /** Mode of the data as originally captured; null = could not be determined. */
  original_source_mode: SourceMode | null;
  label: string;
  scenario: string | null;
  status: RecordingStatus;
  frames: number;
  /** Compressed size on disk. */
  bytes: number;
  duration_s: number;
  link_ids: string[];
  consent_id: string;
  notes: string | null;
  /** True when any part of the data is synthetic (never validation evidence). */
  synthetic: boolean;
  config_version: string | null;
  stop_reason: string | null;
}

export interface CalibrationOverview {
  active: CalibrationRecord | null;
  history: CalibrationRecord[];
  in_progress: Record<string, unknown> | null;
}

export interface RoomSaveResponse {
  room: RoomGeometry;
  /** What the new geometry invalidated (calibrations, zone models, ...). */
  invalidated: unknown;
}

export type EventKind = 'MARK' | 'START' | 'END';

export interface LabelledEventRequest {
  label: string;
  kind: EventKind;
  t_unix_ns?: number;
  notes?: string | null;
}

/** Body for POST /api/validation/runs (fields of storage ValidationRun the operator fills in). */
export interface ValidationRunRequest {
  scenario_id: string;
  placement: string;
  wall_description: string;
  channel: number | null;
  conditions: string;
  notes: string;
}

export type ValidationRunStatus = 'RUNNING' | 'COMPLETE' | 'ABORTED';

/** Mirrors backend/roomsense/storage/models.py ValidationRun. */
export interface ValidationRun {
  run_id: string;
  scenario_id: string;
  session_id: string;
  recording_id: string | null;
  source_mode: SourceMode;
  started_at_unix_ns: number;
  ended_at_unix_ns: number | null;
  placement: string;
  wall_description: string;
  channel: number | null;
  conditions: string;
  notes: string;
  status: ValidationRunStatus;
  link_ids: string[];
}

/** Mirrors backend/roomsense/validation/protocol.py ProtocolScenario.to_dict(). */
export interface ProtocolScenario {
  scenario_id: string;
  title: string;
  purpose: string;
  instructions: string[];
  required_metadata: string[];
  min_duration_s: number;
  interval_labels: string[];
  measured: string[];
  pass_fail_meaning: string;
  counts_toward_status: boolean;
}

/** GET /api/validation/protocol (validation/protocol.py protocol_as_dict()). */
export interface ValidationProtocol {
  protocol_version: string;
  general_rules: string[];
  required_metadata: { field: string; help: string }[];
  scenarios: ProtocolScenario[];
}

/** GET /api/zone/status */
export interface ZoneStatusResponse {
  state: ZoneState | string;
  reasons: string[];
  criteria: Record<string, unknown> | null;
  report?: Record<string, unknown> | null;
}

/** Arbitrary JSON as returned by endpoints whose shape is owned elsewhere. */
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
