"""Compatibility manifest for an optional pose research model (capability D).

A pose model may only be considered if a manifest describes exactly where it
came from, what it was trained on, what radio hardware it expects, and what
independent evidence says about it. The manifest is *data about* a model; it
never contains or loads the model itself.

Two manifest shapes are accepted (``configs/pose_model_manifest.json``):

* ``{"schema": "roomsense-pose-manifest-v1", "installed": false, "reason": ...}``
  — the state shipped in this release: no pose model is installed.
* ``{"schema": "roomsense-pose-manifest-v1", "installed": true, ...}`` — a full
  :class:`PoseModelManifest`.

Loading is deliberately strict: size-limited, JSON only (never ``eval`` or
pickle), duplicate keys and non-finite numbers rejected, unknown fields
rejected, and booleans are never accepted where a count is expected (strict
mode stops ``true`` from silently becoming ``1`` antenna).

``configs/pose_requirements.json`` is loaded here too. It lists, for humans and
for the UI, the requirements a future model has to satisfy; the gate in
:mod:`roomsense.inference.pose.gate` checks the same requirement ids.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "MANIFEST_SCHEMA",
    "REQUIREMENTS_SCHEMA",
    "MAX_MANIFEST_BYTES",
    "UNACCEPTABLE_LICENSES",
    "PICKLE_BASED_WEIGHT_SUFFIXES",
    "WEIGHTS_FORMAT_SUFFIX",
    "ManifestError",
    "InputRequirements",
    "ValidationEvidence",
    "ComputeRequirements",
    "PoseModelManifest",
    "NotInstalledManifest",
    "AnyManifest",
    "LoadedManifest",
    "RequirementItem",
    "ProjectHardwareProfile",
    "PoseRequirementsDoc",
    "load_manifest",
    "load_requirements",
    "read_strict_json",
]

MANIFEST_SCHEMA = "roomsense-pose-manifest-v1"
REQUIREMENTS_SCHEMA = "roomsense-pose-requirements-v1"

# A manifest is a small metadata document. Anything bigger is not a manifest,
# and refusing it early keeps a malformed file from costing memory.
MAX_MANIFEST_BYTES = 256 * 1024

# Licence strings that mean "we do not actually know the terms". A model whose
# redistribution/use terms are unknown cannot be installed.
UNACCEPTABLE_LICENSES = frozenset(
    {"", "unknown", "none", "unlicensed", "noassertion", "n/a", "na", "tbd", "todo", "proprietary-unknown"}
)

# Loading these formats in the usual way (torch.load, pickle, joblib) can run
# arbitrary code embedded in the file. They are never accepted, whatever the
# manifest claims.
PICKLE_BASED_WEIGHT_SUFFIXES = frozenset({".pt", ".pth", ".pkl", ".pickle", ".joblib", ".ckpt", ".bin", ".h5", ".npz"})

# Only formats that are plain tensor containers are allowed.
WEIGHTS_FORMAT_SUFFIX: dict[str, str] = {"safetensors": ".safetensors", "onnx": ".onnx"}


class ManifestError(ValueError):
    """Raised when a manifest or requirements file cannot be accepted."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Strict(BaseModel):
    # strict=True: no silent coercion (e.g. "3" -> 3 or true -> 1).
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


def _finite(v: float, name: str) -> float:
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite")
    return v


class InputRequirements(_Strict):
    """Radio input a model was trained on. Every field is required: a manifest
    that cannot say what hardware it needs cannot be matched to hardware."""

    # e.g. "intel5300-csitool", "atheros-csitool", "esp32-classic-lltf"
    csi_source: str = Field(min_length=1, max_length=128)
    # Wire/record format of the CSI stream, e.g. "roomsense-rscsi-v1".
    packet_format: str = Field(min_length=1, max_length=128)
    tx_antennas: int = Field(ge=1, le=64)  # antennas per transmitter
    rx_antennas: int = Field(ge=1, le=64)  # antennas per receiver
    links: int = Field(ge=1, le=64)  # synchronised transmitter->receiver links
    subcarriers: int = Field(ge=1, le=4096)  # valid subcarriers per antenna pair per packet
    sample_rate_hz: float = Field(gt=0, le=10_000)
    # Allowed relative deviation of the measured packet rate.
    sample_rate_tolerance_fraction: float = Field(gt=0, le=0.5)
    phase_required: bool  # True => needs calibrated/sanitised CSI phase
    window_frames: int = Field(ge=1, le=100_000)  # packets per model input window

    @field_validator("sample_rate_hz", "sample_rate_tolerance_fraction")
    @classmethod
    def _is_finite(cls, v: float) -> float:
        return _finite(v, "value")


class ValidationEvidence(_Strict):
    """What independent evidence exists for the model's accuracy.

    ``independent_test`` means the test data shares no session, person or day
    with the training data (a random or within-clip split does not qualify).
    ``measured_in_this_environment`` means the metrics were measured with the
    user's own hardware in the user's own room, not copied from a paper.
    """

    independent_test: bool
    dataset: str | None = Field(default=None, max_length=512)
    split_description: str | None = Field(default=None, max_length=2048)
    metrics: dict[str, float] = Field(default_factory=dict)
    measured_in_this_environment: bool
    uses_synthetic_data: bool
    report_path: str | None = Field(default=None, max_length=1024)

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(cls, v: dict[str, float]) -> dict[str, float]:
        if len(v) > 64:
            raise ValueError("too many metrics")
        for k, x in v.items():
            if not k or len(k) > 64:
                raise ValueError("metric names must be 1..64 characters")
            _finite(x, f"metric {k!r}")
        return v


class ComputeRequirements(_Strict):
    device: Literal["cpu", "gpu"]
    min_memory_mb: int | None = Field(default=None, ge=1)
    notes: str | None = Field(default=None, max_length=2048)


class PoseModelManifest(_Strict):
    """Full description of an installed pose research model."""

    schema_id: Literal["roomsense-pose-manifest-v1"] = Field(alias="schema")
    installed: Literal[True]
    model_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    source_url: str = Field(pattern=r"^https://\S{4,500}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    license: str = Field(max_length=128)
    weights_path: str = Field(min_length=1, max_length=1024)
    weights_format: Literal["safetensors", "onnx"]
    weights_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: InputRequirements
    output_keypoints: list[str] = Field(min_length=1, max_length=128)
    coordinate_frame: str = Field(min_length=1, max_length=128)
    training_domain: str = Field(min_length=1, max_length=4096)
    validation: ValidationEvidence
    compute: ComputeRequirements
    notes: list[str] = Field(default_factory=list, max_length=64)


class NotInstalledManifest(_Strict):
    """The manifest shipped in this release: nothing installed, and why."""

    schema_id: Literal["roomsense-pose-manifest-v1"] = Field(alias="schema")
    installed: Literal[False]
    reason: str = Field(min_length=1, max_length=4096)
    research: str | None = Field(default=None, max_length=1024)
    reviewed_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


AnyManifest = Union[PoseModelManifest, NotInstalledManifest]


@dataclass(frozen=True)
class LoadedManifest:
    manifest: AnyManifest
    raw: dict[str, Any]
    file_sha256: str  # hash of the exact manifest bytes, recorded in provenance

    @property
    def installed(self) -> bool:
        return isinstance(self.manifest, PoseModelManifest)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ManifestError("DUPLICATE_KEY", f"duplicate JSON key {k!r}")
        out[k] = v
    return out


def _reject_constant(token: str) -> Any:
    raise ManifestError("NON_FINITE_NUMBER", f"{token} is not allowed")


def read_strict_json(path: Path, *, max_bytes: int = MAX_MANIFEST_BYTES) -> tuple[dict[str, Any], str]:
    """Read a small JSON object file strictly. Returns ``(obj, sha256_hex)``.

    Raises :class:`ManifestError` with a stable ``code`` on any problem.
    """
    p = Path(path)
    if not p.exists():
        raise ManifestError("MISSING", f"file not found: {p}")
    if not p.is_file():
        raise ManifestError("NOT_A_FILE", f"not a regular file: {p}")
    size = p.stat().st_size
    if size > max_bytes:
        raise ManifestError("TOO_LARGE", f"{size} bytes exceeds the {max_bytes}-byte limit")
    with p.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:  # file grew between stat and read
        raise ManifestError("TOO_LARGE", f"exceeds the {max_bytes}-byte limit")
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError("NOT_UTF8", str(exc)) from exc
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except ManifestError:
        raise
    except json.JSONDecodeError as exc:
        raise ManifestError("INVALID_JSON", str(exc)) from exc
    if not isinstance(obj, dict):
        raise ManifestError("NOT_AN_OBJECT", "top-level JSON value must be an object")
    return obj, digest


def _summarise_validation_error(exc: ValidationError, limit: int = 8) -> str:
    parts = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc or '<root>'}: {err.get('msg')}")
    more = len(exc.errors()) - limit
    if more > 0:
        parts.append(f"... and {more} more")
    return "; ".join(parts)


def load_manifest(path: Path) -> LoadedManifest:
    """Load and validate a pose model manifest.

    Raises :class:`ManifestError` (codes: MISSING, NOT_A_FILE, TOO_LARGE,
    NOT_UTF8, INVALID_JSON, DUPLICATE_KEY, NON_FINITE_NUMBER, NOT_AN_OBJECT,
    WRONG_SCHEMA, INSTALLED_FLAG, SCHEMA_INVALID).
    """
    obj, digest = read_strict_json(path)
    if obj.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError("WRONG_SCHEMA", f"expected schema {MANIFEST_SCHEMA!r}, got {obj.get('schema')!r}")
    installed = obj.get("installed")
    # Dispatch on the exact JSON boolean; 0/1/"false" are not booleans here.
    if installed is True:
        model_cls: type[_Strict] = PoseModelManifest
    elif installed is False:
        model_cls = NotInstalledManifest
    else:
        raise ManifestError("INSTALLED_FLAG", "'installed' must be the JSON boolean true or false")
    try:
        manifest = model_cls.model_validate(obj)
    except ValidationError as exc:
        raise ManifestError("SCHEMA_INVALID", _summarise_validation_error(exc)) from exc
    return LoadedManifest(manifest=manifest, raw=obj, file_sha256=digest)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Requirements document (configs/pose_requirements.json)
# ---------------------------------------------------------------------------


class RequirementItem(_Strict):
    id: str = Field(pattern=r"^R\d{2}_[A-Z0-9_]+$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)


class ProjectHardwareProfile(_Strict):
    """What this project's hardware provides, as declared by the project
    (not measured by this file)."""

    csi_source: str
    tx_antennas_per_link: int = Field(ge=1)
    rx_antennas_per_link: int = Field(ge=1)
    subcarrier_positions: int = Field(ge=1)
    valid_subcarriers: int = Field(ge=1)
    configured_rate_hz_range: list[float] = Field(min_length=2, max_length=2)
    phase_available: bool
    antenna_array: bool
    notes: list[str] = Field(default_factory=list)


class PoseRequirementsDoc(_Strict):
    schema_id: Literal["roomsense-pose-requirements-v1"] = Field(alias="schema")
    summary: str = Field(min_length=1, max_length=4000)
    research: str = Field(min_length=1, max_length=1024)
    requirements: list[RequirementItem] = Field(min_length=1, max_length=64)
    project_hardware: ProjectHardwareProfile
    missing_for_this_hardware: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("requirements")
    @classmethod
    def _unique_ids(cls, v: list[RequirementItem]) -> list[RequirementItem]:
        ids = [r.id for r in v]
        if len(set(ids)) != len(ids):
            raise ValueError("requirement ids must be unique")
        return v


def load_requirements(path: Path) -> PoseRequirementsDoc:
    """Load ``configs/pose_requirements.json`` strictly."""
    obj, _ = read_strict_json(path)
    try:
        return PoseRequirementsDoc.model_validate(obj)
    except ValidationError as exc:
        raise ManifestError("SCHEMA_INVALID", _summarise_validation_error(exc)) from exc
