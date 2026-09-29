"""Configuration loading (TOML) with validated, documented defaults.

The processing/detection sections are hashed into ``config_version`` so that
every derived output can say exactly which thresholds produced it. Changing a
threshold therefore changes the version and invalidates calibrations that were
recorded under a different version.

No secrets are read from the config file. The optional API token for LAN
exposure comes only from the ``ROOMSENSE_API_TOKEN`` environment variable.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import InputFormat, canonical_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "roomsense.toml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "configs" / "roomsense.example.toml"
API_TOKEN_ENV = "ROOMSENSE_API_TOKEN"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Cfg):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    # Binding to a non-loopback address additionally requires
    # ROOMSENSE_API_TOKEN to be set; the server refuses to start otherwise.
    allow_non_loopback: bool = False
    cors_dev_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"]
    )
    serve_frontend: bool = True
    websocket_push_hz: float = Field(default=4.0, gt=0, le=20)

    def is_loopback(self) -> bool:
        if self.host in ("localhost",):
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


class StorageConfig(_Cfg):
    data_dir: str = "data"  # relative paths resolve against the repo root
    max_recording_bytes: int = Field(default=200_000_000, gt=0)
    max_recording_seconds: float = Field(default=3600.0, gt=0)
    max_total_recording_bytes: int = Field(default=2_000_000_000, gt=0)
    keep_raw_lines: bool = True  # store the raw serial line with each frame (debugging)
    max_raw_line_chars: int = Field(default=4096, gt=0)

    def resolved_data_dir(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else (REPO_ROOT / p)


class ReceiverConfig(_Cfg):
    """One live serial receiver (one ESP32 board). Each receiver observes one
    transmitter, forming one sensing link ``<transmitter_id>-><receiver_id>``."""

    receiver_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    port: str  # e.g. /dev/ttyUSB0, /dev/cu.usbserial-0001, COM5. Never auto-guessed.
    baud: int = Field(default=921600, gt=0)
    input_format: InputFormat = InputFormat.ROOMSENSE_RSCSI_V1
    transmitter_id: str = Field(default="tx1", pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    # Only CSI from this transmitter MAC is accepted (the firmware filters too).
    transmitter_mac: str | None = Field(default=None, pattern=r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
    declared_chip: str | None = None  # required for upstream formats (no hello line)
    declared_board: str | None = None
    # CSI config the firmware uses. RoomSense firmware reports it in RSHELLO;
    # for upstream esp-csi firmware it must be declared:
    #   csi_recv_router (classic chips) -> "lltf_only"
    #   csi_recv (classic chips)        -> "lltf_htltf_stbc"
    #   ESP32-C5 examples               -> "c5_default"
    ltf_config: Literal["lltf_only", "lltf_htltf_stbc", "c5_default"] | None = None
    # ESP32-C6/C61 layouts are not documented in ESP-IDF v5.5.5. Opting in
    # reuses the ESP32-C5 table and flags every frame as an assumption.
    allow_undocumented_layout_assumption: bool = False

    @property
    def link_id(self) -> str:
        return f"{self.transmitter_id}->{self.receiver_id}"


class AcquisitionConfig(_Cfg):
    receivers: list[ReceiverConfig] = Field(default_factory=list)
    expected_rate_hz: float = Field(default=25.0, gt=0, le=1000)
    stale_after_s: float = Field(default=2.0, gt=0)  # no frames this long => SENSOR_OFFLINE
    max_line_bytes: int = Field(default=8192, ge=256, le=65536)
    max_csi_values: int = Field(default=612, ge=64, le=2048)  # largest documented buffer
    reconnect_initial_s: float = Field(default=1.0, gt=0)
    reconnect_max_s: float = Field(default=30.0, gt=0)
    frame_queue_size: int = Field(default=4096, ge=64)
    # Multi-receiver alignment tolerance: windows from different receivers are
    # only combined if their end times differ by at most this much.
    alignment_tolerance_s: float = Field(default=0.25, gt=0)

    @model_validator(mode="after")
    def _unique(self) -> "AcquisitionConfig":
        ids = [r.receiver_id for r in self.receivers]
        if len(set(ids)) != len(ids):
            raise ValueError("receiver_id values must be unique")
        return self


class ProcessingConfig(_Cfg):
    window_s: float = Field(default=2.0, gt=0.2, le=30)
    hop_s: float = Field(default=0.5, gt=0.05, le=10)
    min_frames_per_window: int = Field(default=20, ge=4)
    # Gaps longer than this split a window; we never interpolate across them.
    max_gap_s: float = Field(default=0.25, gt=0)
    # Resampling only fills gaps shorter than max_gap_s (linear, amplitude only).
    resample_hz: float | None = Field(default=None, gt=0)
    hampel_window: int = Field(default=5, ge=0)  # 0 disables outlier filter
    hampel_sigmas: float = Field(default=3.0, gt=0)
    min_valid_subcarrier_fraction: float = Field(default=0.6, ge=0, le=1)
    use_phase: bool = False  # phase processing disabled until justified/tested per hardware


class DetectionConfig(_Cfg):
    enter_threshold: float = Field(default=4.0, gt=0)
    exit_threshold: float = Field(default=2.5, gt=0)
    min_motion_hold_s: float = Field(default=2.0, ge=0)
    min_quiet_hold_s: float = Field(default=3.0, ge=0)
    baseline_min_duration_s: float = Field(default=60.0, gt=0)
    baseline_min_windows: int = Field(default=30, ge=5)
    # Detections are cleared (state -> SENSOR_OFFLINE) once data is this stale.
    clear_stale_after_s: float = Field(default=5.0, gt=0)
    min_quality_for_decision: Literal["GOOD", "DEGRADED"] = "DEGRADED"
    # Drift monitor: correlation between current static amplitude profile and
    # the baseline profile. Below this for drift_hold_s => calibration suspect.
    drift_min_profile_correlation: float = Field(default=0.8, ge=-1, le=1)
    drift_hold_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _hysteresis(self) -> "DetectionConfig":
        if self.exit_threshold >= self.enter_threshold:
            raise ValueError("exit_threshold must be < enter_threshold (hysteresis)")
        return self


class ZoneConfig(_Cfg):
    criteria_file: str = "configs/zone_enablement.toml"
    min_receivers: int = Field(default=3, ge=1)


class PoseConfig(_Cfg):
    manifest_file: str = "configs/pose_model_manifest.json"


class RoomConfig(_Cfg):
    # Path to a user-provided room JSON. If missing, the EXAMPLE room is used
    # and labelled EXAMPLE everywhere.
    geometry_file: str = "data/room.json"


class AppConfig(_Cfg):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    acquisition: AcquisitionConfig = Field(default_factory=AcquisitionConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    zone: ZoneConfig = Field(default_factory=ZoneConfig)
    pose: PoseConfig = Field(default_factory=PoseConfig)
    room: RoomConfig = Field(default_factory=RoomConfig)

    def config_version(self) -> str:
        """Hash of every setting that changes derived outputs."""
        return "cfg-" + canonical_hash(
            {
                "processing": self.processing.model_dump(mode="json"),
                "detection": self.detection.model_dump(mode="json"),
            }
        )

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (REPO_ROOT / path)


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load config from ``path``, else configs/roomsense.toml, else defaults."""
    candidate = Path(path) if path else DEFAULT_CONFIG_PATH
    if path and not candidate.exists():
        raise FileNotFoundError(f"config file not found: {candidate}")
    if candidate.exists():
        with candidate.open("rb") as fh:
            data = tomllib.load(fh)
        return AppConfig.model_validate(data)
    return AppConfig()


def api_token() -> str | None:
    """Token for non-loopback exposure. Never log this value."""
    tok = os.environ.get(API_TOKEN_ENV)
    return tok if tok and len(tok) >= 16 else None
