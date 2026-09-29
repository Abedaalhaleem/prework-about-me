"""Typed, versioned record schemas shared by every RoomSense module.

Two families of types live here:

* Hot-path internal records (``CsiFrame``, ``DeviceIdentity``) are frozen
  ``dataclasses`` with ``slots`` for speed. They serialise to plain dicts with
  an explicit ``schema_version`` via ``to_record()`` / ``from_record()``.
* API-facing records are pydantic v2 models so FastAPI validates and documents
  them. The TypeScript mirror lives in ``frontend/src/api/types.ts`` and must be
  kept in sync by hand (see docs/ARCHITECTURE.md).

Conventions
-----------
* ``None`` always means *unavailable / not measured*. Never substitute a
  default number for missing metadata.
* All wall-clock times are integer nanoseconds since the Unix epoch (UTC)
  unless the field name says otherwise.
* Floor-plane coordinates are metres: ``x`` east, ``y`` north, ``z`` height
  above the floor. (The frontend converts to three.js y-up.)
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import PARSER_VERSION, SCHEMA_VERSION

__all__ = [
    "SCHEMA_VERSION",
    "PARSER_VERSION",
    "SourceMode",
    "SOURCE_MODE_BANNER",
    "InputFormat",
    "QualityFlag",
    "DeviceIdentity",
    "CsiFrame",
    "ActivityState",
    "QualityLevel",
    "QualityReport",
    "Provenance",
    "ScoreUncertainty",
    "ActivityResult",
    "CapabilityId",
    "CapabilityState",
    "VerificationStatus",
    "CapabilityStatus",
    "SourceState",
    "LinkStatus",
    "GeometryProvenance",
    "Vec2",
    "Vec3",
    "Wall",
    "Door",
    "NodeRole",
    "SensorNode",
    "LinkDef",
    "Zone",
    "RoomGeometry",
    "CalibrationKind",
    "CalibrationRecord",
    "WalkTestLinkReport",
    "WalkTestReport",
    "ZoneState",
    "ZonePrediction",
    "PoseStatus",
    "UnsupportedCapability",
    "SystemStatus",
    "canonical_hash",
]


def canonical_hash(obj: Any, length: int = 16) -> str:
    """Stable short SHA-256 of a JSON-serialisable object (sorted keys)."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# Source / provenance enums
# ---------------------------------------------------------------------------


class SourceMode(str, enum.Enum):
    """Where measurements come from. Shown permanently in the UI."""

    LIVE = "LIVE"
    REPLAY = "REPLAY"
    SIMULATION = "SIMULATION"


SOURCE_MODE_BANNER: dict[SourceMode, str] = {
    SourceMode.LIVE: "LIVE MEASUREMENTS",
    SourceMode.REPLAY: "RECORDED REPLAY",
    SourceMode.SIMULATION: "SIMULATION",
}


class InputFormat(str, enum.Enum):
    """Wire/record formats the parser understands. Anything else is rejected."""

    # esp-csi examples/get-started (csi_recv, csi_recv_router) on
    # ESP32 / ESP32-S2 / ESP32-S3 / ESP32-C3: 25 CSV columns.
    UPSTREAM_CLASSIC_V1 = "esp-csi-upstream-classic-v1"
    # esp-csi examples/get-started on ESP32-C5 / ESP32-C6 / ESP32-C61: 15 columns.
    UPSTREAM_C5C6_V1 = "esp-csi-upstream-c5c6-v1"
    # This project's firmware (firmware/esp32): RSCSI/RSHELLO/RSSTAT lines.
    ROOMSENSE_RSCSI_V1 = "roomsense-rscsi-v1"
    # Synthetic generator (roomsense.acquisition.synthetic). Never hardware.
    SYNTHETIC_V1 = "synthetic-v1"


class QualityFlag(str, enum.Enum):
    """Per-frame quality flags. A frame may carry several."""

    SYNTHETIC = "SYNTHETIC"
    REPLAYED = "REPLAYED"
    FIRST_WORD_INVALID = "FIRST_WORD_INVALID"
    UNKNOWN_LAYOUT = "UNKNOWN_LAYOUT"
    UNDOCUMENTED_LAYOUT_ASSUMPTION = "UNDOCUMENTED_LAYOUT_ASSUMPTION"
    MAC_NOT_CONFIGURED = "MAC_NOT_CONFIGURED"
    LAYOUT_MISMATCH = "LAYOUT_MISMATCH"
    GAIN_COMPENSATED_UPSTREAM = "GAIN_COMPENSATED_UPSTREAM"
    ALL_ZERO_CSI = "ALL_ZERO_CSI"
    SATURATED_VALUES = "SATURATED_VALUES"
    COUNTER_GAP = "COUNTER_GAP"
    COUNTER_ROLLOVER = "COUNTER_ROLLOVER"
    COUNTER_RESET = "COUNTER_RESET"
    TIMESTAMP_ROLLOVER = "TIMESTAMP_ROLLOVER"
    TIMESTAMP_NON_MONOTONIC = "TIMESTAMP_NON_MONOTONIC"
    HOST_TIMESTAMP_UNAVAILABLE = "HOST_TIMESTAMP_UNAVAILABLE"
    DEVICE_TIMESTAMP_UNAVAILABLE = "DEVICE_TIMESTAMP_UNAVAILABLE"
    RX_STATE_ERROR = "RX_STATE_ERROR"
    FIRMWARE_QUEUE_DROPS = "FIRMWARE_QUEUE_DROPS"


# ---------------------------------------------------------------------------
# Internal hot-path records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Identity of the *receiving* board. Unknown parts stay ``None``.

    ``identity_source`` records where the identity came from so that a
    user-declared board is never presented as firmware-reported.
    """

    receiver_id: str
    chip: str | None = None  # e.g. "esp32s3" (firmware-reported or declared)
    board: str | None = None  # e.g. "ESP32-S3-DevKitC-1U" (user-declared only)
    firmware_name: str | None = None
    firmware_version: str | None = None
    idf_version: str | None = None
    station_mac: str | None = None  # the receiver's own MAC, if reported
    identity_source: Literal["firmware_hello", "user_config", "synthetic", "unavailable"] = "unavailable"

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "DeviceIdentity":
        return cls(**rec)


@dataclass(frozen=True, slots=True)
class CsiFrame:
    """One parsed CSI measurement for one sensing link.

    ``raw_csi`` holds the CSI values exactly as the device emitted them
    (imaginary, real, imaginary, real, ... per ESP-IDF docs). They are signed
    8-bit for ESP-IDF's ``wifi_csi_info_t.buf``; the upstream esp-csi example
    firmware may emit gain-compensated int16 values instead, which is flagged
    with ``values_are_gain_compensated=True`` and
    ``QualityFlag.GAIN_COMPENSATED_UPSTREAM``.

    ``layout_id`` / ``valid_subcarriers`` come from
    :mod:`roomsense.csi_layouts`. A frame whose layout cannot be identified is
    kept for debugging but must never be padded/truncated into a model input.
    """

    # --- provenance ---
    source_mode: SourceMode
    session_id: str
    receiver_id: str
    transmitter_id: str
    link_id: str
    input_format: InputFormat
    device: DeviceIdentity
    # --- counters & time ---
    frame_counter: int | None  # raw receiver-side record sequence number as reported
    frame_counter_unwrapped: int | None  # monotonically unwrapped by host
    transmitter_counter: int | None  # sequence number sent by a RoomSense transmitter (ESP-NOW mode)
    device_timestamp_us: int | None  # raw rx_ctrl.timestamp (32-bit, wraps ~71.6 min)
    device_timestamp_unwrapped_us: int | None
    host_arrival_monotonic_ns: int | None  # time.monotonic_ns() at line receipt
    host_arrival_unix_ns: int | None  # time.time_ns() at line receipt
    # --- radio metadata (None = not reported by this format/chip) ---
    transmitter_mac: str | None
    channel: int | None
    secondary_channel: int | None  # 0 none, 1 above, 2 below (classic chips)
    bandwidth_mhz: int | None  # derived from rx_ctrl.cwb when available
    sig_mode: int | None  # classic chips: 0 non-HT, 1 HT, 3 VHT
    bb_format: int | None  # ESP32-C5/C6/C61: rx_ctrl.cur_bb_format
    mcs: int | None
    rate: int | None
    stbc: int | None
    rssi_dbm: int | None
    noise_floor_dbm: int | None
    agc_gain: int | None
    fft_gain: int | None
    sig_len: int | None
    rx_state: int | None
    antenna: int | None
    first_word_invalid: bool | None
    # --- CSI payload ---
    csi_len: int  # number of values in raw_csi as declared by the device
    raw_csi: tuple[int, ...]
    values_are_gain_compensated: bool | None
    layout_id: str | None
    valid_subcarriers: tuple[int, ...] | None  # logical subcarrier indices kept
    # --- bookkeeping ---
    quality_flags: tuple[str, ...] = ()
    firmware_drop_count: int | None = None  # cumulative drops reported by firmware
    recorded_host_arrival_unix_ns: int | None = None  # original time when replayed
    raw_line: str | None = None  # bounded copy of the source line for debugging
    schema_version: str = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION

    def to_record(self) -> dict[str, Any]:
        """Plain-JSON dict (enums as values, tuples as lists)."""
        rec = asdict(self)
        rec["source_mode"] = self.source_mode.value
        rec["input_format"] = self.input_format.value
        rec["raw_csi"] = list(self.raw_csi)
        rec["valid_subcarriers"] = None if self.valid_subcarriers is None else list(self.valid_subcarriers)
        rec["quality_flags"] = list(self.quality_flags)
        return rec

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "CsiFrame":
        """Inverse of :meth:`to_record`. Rejects unknown major schema versions."""
        version = str(rec.get("schema_version", ""))
        if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise ValueError(f"incompatible CsiFrame schema_version {version!r}; expected {SCHEMA_VERSION}")
        data = dict(rec)
        data["source_mode"] = SourceMode(data["source_mode"])
        data["input_format"] = InputFormat(data["input_format"])
        data["device"] = DeviceIdentity.from_record(data["device"])
        data["raw_csi"] = tuple(int(v) for v in data["raw_csi"])
        vs = data.get("valid_subcarriers")
        data["valid_subcarriers"] = None if vs is None else tuple(int(v) for v in vs)
        data["quality_flags"] = tuple(data.get("quality_flags", ()))
        return cls(**data)


# ---------------------------------------------------------------------------
# Activity detection outputs
# ---------------------------------------------------------------------------


class ActivityState(str, enum.Enum):
    """Per-link motion state. Low-quality/stale data is never "room empty"."""

    UNKNOWN = "UNKNOWN"
    CALIBRATING = "CALIBRATING"
    NO_MOTION_DETECTED = "NO_MOTION_DETECTED"
    MOTION_DETECTED = "MOTION_DETECTED"
    SENSOR_OFFLINE = "SENSOR_OFFLINE"


class QualityLevel(str, enum.Enum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    BAD = "BAD"
    UNAVAILABLE = "UNAVAILABLE"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class QualityReport(_Model):
    """Signal/acquisition quality for one measurement window. Separate from
    the activity score and from any probability."""

    level: QualityLevel
    packet_rate_hz: float | None = None  # measured over the window
    expected_rate_hz: float | None = None  # configured transmit/ping rate
    loss_fraction: float | None = None  # from device counter gaps, if counters exist
    max_gap_s: float | None = None
    timing_jitter_ms: float | None = None
    rssi_dbm_median: float | None = None
    valid_subcarrier_fraction: float | None = None
    frames_in_window: int = 0
    rejected_frames: int = 0
    flags: list[str] = Field(default_factory=list)


class Provenance(_Model):
    """Attached to every derived output (activity, zone, pose, validation)."""

    source_mode: SourceMode
    session_id: str
    link_ids: list[str]
    window_start_unix_ns: int | None
    window_end_unix_ns: int | None
    window_frame_count: int
    config_version: str  # canonical hash of processing+detection config
    model_version: str | None = None
    calibration_id: str | None = None
    computed_at_unix_ns: int
    measurement_age_s: float | None = None  # age of newest contributing frame at compute time


class ScoreUncertainty(_Model):
    """Spread of the heuristic score. Not a confidence interval on presence."""

    method: str  # e.g. "subcarrier_iqr"
    low: float
    high: float
    n_subcarriers: int
    n_frames: int


class ActivityResult(_Model):
    """Output of the motion detector for one link and one window.

    ``activity_score`` is a unitless heuristic (robust deviation from the
    quiet baseline). It is NOT a probability of presence. ``calibrated_probability``
    stays ``None`` unless a statistically calibrated model exists (none does in
    this release).
    """

    link_id: str
    state: ActivityState
    activity_score: float | None
    enter_threshold: float
    exit_threshold: float
    calibrated_probability: float | None = None
    uncertainty: ScoreUncertainty | None = None
    quality: QualityReport
    reasons: list[str] = Field(default_factory=list)
    provenance: Provenance

    @field_validator("calibrated_probability")
    @classmethod
    def _no_uncalibrated_probability(cls, v: float | None) -> float | None:
        if v is not None:
            raise ValueError(
                "calibrated_probability must stay None: no statistically calibrated "
                "presence model exists in this release"
            )
        return v


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class CapabilityId(str, enum.Enum):
    A_ACQUISITION = "A_ACQUISITION"  # live CSI acquisition + diagnostics
    B_MOTION = "B_MOTION"  # motion/activity detection
    C_ZONE = "C_ZONE"  # experimental single-person zone estimation
    D_POSE = "D_POSE"  # research: body pose (gated on hardware+weights+validation)


class CapabilityState(str, enum.Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    HARDWARE_REQUIRED = "HARDWARE_REQUIRED"
    REQUIRES_CALIBRATION = "REQUIRES_CALIBRATION"
    REQUIRES_VALIDATION = "REQUIRES_VALIDATION"
    UNSUPPORTED = "UNSUPPORTED"


class VerificationStatus(_Model):
    """Evidence levels, kept separate on purpose. Only set True with evidence."""

    software_tested: bool = False  # automated tests executed (synthetic/fixture data)
    firmware_compiled: bool = False  # built with the pinned ESP-IDF for a real target
    hardware_tested: bool = False  # exercised with real boards
    through_wall_validated: bool = False  # validated behind a real wall per protocol
    notes: list[str] = Field(default_factory=list)


class CapabilityStatus(_Model):
    capability: CapabilityId
    title: str
    state: CapabilityState
    reasons: list[str] = Field(default_factory=list)
    verification: VerificationStatus = Field(default_factory=VerificationStatus)


# ---------------------------------------------------------------------------
# Link / source status
# ---------------------------------------------------------------------------


class SourceState(str, enum.Enum):
    NO_SOURCE = "NO_SOURCE"
    CONNECTING = "CONNECTING"
    RUNNING = "RUNNING"
    DISCONNECTED = "DISCONNECTED"  # live hardware lost; will NOT fall back to replay/sim
    FINISHED = "FINISHED"  # replay reached end of file
    ERROR = "ERROR"


class LinkStatus(_Model):
    link_id: str
    receiver_id: str
    transmitter_id: str
    connected: bool
    last_frame_age_s: float | None
    acquisition_rate_hz: float | None  # measured, over the last few seconds
    frames_total: int = 0
    frames_rejected: int = 0
    parse_errors: int = 0
    firmware_drops: int | None = None
    layout_id: str | None = None
    channel: int | None = None
    device: dict[str, Any] | None = None
    clock_offset_ms: float | None = None  # device clock vs host clock (see alignment)
    clock_drift_ppm: float | None = None


# ---------------------------------------------------------------------------
# Room geometry (always USER PROVIDED or EXAMPLE; never Wi-Fi reconstructed)
# ---------------------------------------------------------------------------


class GeometryProvenance(str, enum.Enum):
    USER_PROVIDED = "USER_PROVIDED"
    EXAMPLE = "EXAMPLE"


class Vec2(_Model):
    x: float
    y: float


class Vec3(_Model):
    x: float
    y: float
    z: float


class Wall(_Model):
    id: str
    start: Vec2
    end: Vec2
    height_m: float = Field(default=2.5, gt=0, le=20)
    thickness_m: float = Field(default=0.12, gt=0, le=2)
    material: str | None = None  # free text, user provided (e.g. "drywall")
    is_target_room_boundary: bool = False


class Door(_Model):
    id: str
    wall_id: str
    offset_m: float = Field(ge=0)  # distance from wall start
    width_m: float = Field(gt=0, le=5)
    height_m: float = Field(default=2.0, gt=0, le=5)


class NodeRole(str, enum.Enum):
    TX = "TX"
    RX = "RX"
    ROUTER = "ROUTER"  # a router used as the CSI source (router-ping mode)


class SensorNode(_Model):
    id: str
    role: NodeRole
    label: str
    position: Vec3  # user-measured mounting position
    inside_target_room: bool | None = None
    device_mac: str | None = None


class LinkDef(_Model):
    link_id: str
    transmitter_id: str
    receiver_id: str


class Zone(_Model):
    id: str
    label: str
    polygon: list[Vec2] = Field(min_length=3)
    kind: Literal["TARGET_ROOM_ZONE", "OUTSIDE_TARGET_ROOM"] = "TARGET_ROOM_ZONE"

    def display_anchor(self) -> Vec2:
        """Polygon centroid used ONLY as a display anchor, never as a measured
        position."""
        pts = self.polygon
        a = cx = cy = 0.0
        n = len(pts)
        for i in range(n):
            x0, y0 = pts[i].x, pts[i].y
            x1, y1 = pts[(i + 1) % n].x, pts[(i + 1) % n].y
            cross = x0 * y1 - x1 * y0
            a += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        if abs(a) < 1e-12:
            return Vec2(x=sum(p.x for p in pts) / n, y=sum(p.y for p in pts) / n)
        a *= 0.5
        return Vec2(x=cx / (6 * a), y=cy / (6 * a))


class RoomGeometry(_Model):
    """Room model for display and configuration. Always entered by the user
    (USER_PROVIDED) or shipped as a sample (EXAMPLE). It is never inferred from
    Wi-Fi measurements."""

    geometry_id: str
    provenance: GeometryProvenance
    name: str
    width_m: float = Field(gt=0, le=100)  # extent along x
    depth_m: float = Field(gt=0, le=100)  # extent along y
    height_m: float = Field(default=2.5, gt=0, le=20)
    walls: list[Wall] = Field(default_factory=list)
    doors: list[Door] = Field(default_factory=list)
    nodes: list[SensorNode] = Field(default_factory=list)
    links: list[LinkDef] = Field(default_factory=list)
    zones: list[Zone] = Field(default_factory=list)
    target_room_polygon: list[Vec2] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _check_references(self) -> "RoomGeometry":
        node_ids = {n.id for n in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("duplicate sensor node id")
        wall_ids = {w.id for w in self.walls}
        for d in self.doors:
            if d.wall_id not in wall_ids:
                raise ValueError(f"door {d.id} references unknown wall {d.wall_id}")
        for link in self.links:
            if link.transmitter_id not in node_ids or link.receiver_id not in node_ids:
                raise ValueError(f"link {link.link_id} references unknown node")
        zone_ids = [z.id for z in self.zones]
        if len(set(zone_ids)) != len(zone_ids):
            raise ValueError("duplicate zone id")
        return self

    def config_hash(self) -> str:
        """Hash of everything that affects calibration validity (node
        positions, links, zones, walls). Changing any of these invalidates
        calibrations and zone models tied to the old hash."""
        d = self.model_dump(mode="json")
        d.pop("name", None)
        d.pop("notes", None)
        d.pop("geometry_id", None)
        return canonical_hash(d)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class CalibrationKind(str, enum.Enum):
    QUIET_BASELINE = "QUIET_BASELINE"
    WALK_TEST = "WALK_TEST"


class CalibrationRecord(_Model):
    calibration_id: str
    kind: CalibrationKind
    created_at_unix_ns: int
    session_id: str
    source_mode: SourceMode
    link_ids: list[str]
    hardware_signature: str
    room_config_hash: str | None
    processing_config_version: str
    duration_s: float
    frame_count: int
    window_count: int
    valid: bool
    invalidated_reason: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class WalkTestLinkReport(_Model):
    """Per-link walk-test summary (same-room check; NOT through-wall evidence)."""

    link_id: str
    max_score: float | None
    motion_window_fraction: float | None
    windows: int
    motion_windows: int = 0
    undecided_windows: int = 0
    offline_windows: int = 0
    detected: bool
    reasons: list[str] = Field(default_factory=list)


class WalkTestReport(_Model):
    session_id: str | None
    source_mode: SourceMode | None
    started_at_unix_ns: int | None
    ended_at_unix_ns: int | None
    links: list[WalkTestLinkReport] = Field(default_factory=list)
    note: str = "A same-room walk test does not establish behind-wall performance."


# ---------------------------------------------------------------------------
# Zone estimation (capability C) and pose (capability D)
# ---------------------------------------------------------------------------


class ZoneState(str, enum.Enum):
    DISABLED = "DISABLED"  # capability not enabled (criteria not met / no model)
    ABSTAIN = "ABSTAIN"  # enabled but refuses to estimate for this window
    ESTIMATE = "ESTIMATE"


class ZonePrediction(_Model):
    state: ZoneState
    zone_id: str | None = None
    zone_label: str | None = None
    # Classifier scores from the validated model. Named "scores" on purpose:
    # they are not claimed to be calibrated probabilities.
    model_scores: dict[str, float] = Field(default_factory=dict)
    display_anchor: Vec2 | None = None  # zone centroid for DISPLAY ONLY
    display_anchor_note: str = "Zone centre is a display anchor, not a measured position."
    reasons: list[str] = Field(default_factory=list)
    model_id: str | None = None
    criteria_version: str | None = None
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ZonePrediction":
        if self.state != ZoneState.ESTIMATE and self.zone_id is not None:
            raise ValueError("zone_id may only be set when state == ESTIMATE")
        if self.state == ZoneState.ESTIMATE and (self.zone_id is None or self.provenance is None):
            raise ValueError("an ESTIMATE requires zone_id and provenance")
        return self


class PoseStatus(_Model):
    enabled: bool = False
    label: Literal["EXPERIMENTAL"] = "EXPERIMENTAL"
    model_id: str | None = None
    missing_requirements: list[str] = Field(default_factory=list)
    manifest: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Whole-system status (drives the permanent UI banners)
# ---------------------------------------------------------------------------


class UnsupportedCapability(_Model):
    """A claim this system explicitly does NOT make (shown in the UI)."""

    id: str
    claim: str
    reason: str


class SystemStatus(_Model):
    """Snapshot of everything the UI shows (``GET /api/status``, pushed on ``/api/ws``).

    * ``source_banner`` names the selected source mode. ``simulated`` is True
      for SIMULATION *and* for a REPLAY of simulated data: the banner then
      stays ``"RECORDED REPLAY"`` while ``simulated`` is True, and the UI
      shows both.
    * ``hardware_required`` = NOT (the active source is LIVE AND at least one
      live link has delivered measured frames with a documented CSI layout
      within ``acquisition.stale_after_s`` and has not reported a disconnect
      since). It is therefore True with no source, during replay and
      simulation, before the first documented frame, and again once a board
      is unplugged or its link goes stale. It is never sticky for the
      process.
    * ``activity`` holds the newest result per link of the current session
      only. A result computed longer ago than ``detection.clear_stale_after_s``
      (processing stalled or stopped) is replaced by state ``UNKNOWN`` with
      the reason ``"PROCESSING_STALLED: ..."``, no score, quality
      ``UNAVAILABLE`` and its original provenance; an old MOTION / NO_MOTION
      decision is never shown as current.
    * ``notes`` are ``"CODE: text"`` lines. Notes about a session
      (``RECORDING_STOPPED``, ``HOST_QUEUE_DROPS``, ``END_OF_STREAM``,
      ``FRAMES_REJECTED_SOURCE_MISMATCH``) are cleared when a new source or
      session starts.
    """

    server_time_unix_ns: int
    source_mode: SourceMode | None  # None when no source is selected
    source_banner: str  # "LIVE MEASUREMENTS" / "RECORDED REPLAY" / "SIMULATION" / "NO SOURCE"
    simulated: bool  # True => UI must show the large SIMULATED DATA banner
    source_state: SourceState
    source_detail: str | None = None
    session_id: str | None = None
    hardware_required: bool = True  # see the class docstring; never sticky for the process
    capabilities: list[CapabilityStatus] = Field(default_factory=list)
    links: list[LinkStatus] = Field(default_factory=list)
    activity: list[ActivityResult] = Field(default_factory=list)
    zone: ZonePrediction
    pose: PoseStatus
    calibration: CalibrationRecord | None = None
    calibration_valid: bool = False
    calibration_detail: str | None = None
    localization_status: str  # human readable, e.g. "DISABLED: no validated zone model"
    through_wall_status: Literal["UNVERIFIED", "VALIDATED", "NOT_DISTINGUISHABLE"] = "UNVERIFIED"
    through_wall_detail: str | None = None
    recording_active: bool = False
    recording_id: str | None = None
    operating_scope: str = (
        "One moving participant. A motionless person can remain undetected. "
        "Not a people counter, not identification, not continuous tracking."
    )
    stale_clear_timeout_s: float = 5.0
    unsupported_capabilities: list[UnsupportedCapability] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
