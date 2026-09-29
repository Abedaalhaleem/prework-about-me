"""Capability registry: what the app can honestly claim right now.

:func:`build_capabilities` turns a snapshot of runtime facts
(:class:`CapabilityContext`) into one :class:`~roomsense.schemas.CapabilityStatus`
per capability:

* **A_ACQUISITION** is ENABLED only while a LIVE source is running and has
  delivered frames with a documented CSI layout. Replay and simulation are
  not live acquisition and never enable it.
* **B_MOTION** is ENABLED only while a source is running and a valid quiet
  baseline exists for it. In SIMULATION it is explicitly a software path,
  not a measurement.
* **C_ZONE** is never ENABLED unless the zone module reports that a model
  passed its enablement criteria (``zone_enabled``), and never on simulated
  data.
* **D_POSE** mirrors the pose gate, which is closed in this release.

The ``verification`` part of each status (software tested / firmware compiled /
hardware tested / through-wall validated) comes **only** from
``configs/verification_evidence.json``. Runtime state never upgrades it: a
running live source shows the code works on the user's desk, which is not the
same as recorded evidence.

:data:`UNSUPPORTED_CAPABILITIES` lists claims the app must never make, so the
UI can show them next to the capability list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import REPO_ROOT
from .schemas import (
    CapabilityId,
    CapabilityState,
    CapabilityStatus,
    PoseStatus,
    SourceMode,
    SourceState,
    VerificationStatus,
)

__all__ = [
    "EVIDENCE_SCHEMA",
    "DEFAULT_EVIDENCE_PATH",
    "CAPABILITY_TITLES",
    "SIMULATION_ONLY_REASON",
    "UNSUPPORTED_CAPABILITIES",
    "UnsupportedCapability",
    "CapabilityContext",
    "EvidenceError",
    "load_verification_evidence",
    "verification_for",
    "build_capabilities",
]

EVIDENCE_SCHEMA = "roomsense-verification-evidence-v1"
DEFAULT_EVIDENCE_PATH = REPO_ROOT / "configs" / "verification_evidence.json"
MAX_EVIDENCE_BYTES = 256 * 1024

CAPABILITY_TITLES: dict[CapabilityId, str] = {
    CapabilityId.A_ACQUISITION: "Live CSI acquisition and diagnostics",
    CapabilityId.B_MOTION: "Motion detection from measured CSI (heuristic, per link)",
    CapabilityId.C_ZONE: "Experimental single-person zone estimation",
    CapabilityId.D_POSE: "Pose research (gated; needs compatible hardware, weights and validation)",
}

SIMULATION_ONLY_REASON = "simulation only — software path, not a measurement"

_SCOPE_NOTE = (
    "The activity score is a unitless heuristic, not a probability of presence. "
    "A motionless person can remain undetected."
)


class UnsupportedCapability(TypedDict):
    id: str
    claim: str
    reason: str


# Claims this app does not support and must not suggest. Shown in the UI.
UNSUPPORTED_CAPABILITIES: list[UnsupportedCapability] = [
    {
        "id": "CAMERA_LIKE_IMAGING",
        "claim": "Camera-like imaging, or 'seeing' through walls",
        "reason": "Each ESP32 link yields a few dozen amplitude values per packet from one antenna. "
        "That describes how the channel changed, not what the room looks like.",
    },
    {
        "id": "OBJECT_RECOGNITION_THROUGH_WALLS",
        "claim": "Recognising objects through walls",
        "reason": "Static objects are part of the quiet baseline. The pipeline measures deviation "
        "from that baseline, not what caused it.",
    },
    {
        "id": "IDENTIFYING_INDIVIDUALS",
        "claim": "Identifying or re-identifying individuals",
        "reason": "No identity model exists, and building one would need biometric data the app "
        "does not collect.",
    },
    {
        "id": "PEOPLE_COUNTING",
        "claim": "Counting people",
        "reason": "Motion scores from several people overlap. The operating scope is one moving participant.",
    },
    {
        "id": "RELIABLE_OCCUPANCY_OF_MOTIONLESS_PEOPLE",
        "claim": "Reliable occupancy or presence detection of motionless people",
        "reason": "Detection is based on motion relative to a quiet baseline. A still person can "
        "look exactly like an empty room.",
    },
    {
        "id": "ANATOMICAL_DETAIL",
        "claim": "Anatomical detail, body shape, skeletons or dense pose",
        "reason": "Pose (capability D) is disabled: no compatible model, weights or validation exist "
        "for single-antenna ESP32 CSI (docs/MODEL_COMPATIBILITY.md).",
    },
    {
        "id": "CONTINUOUS_TRACKING",
        "claim": "Continuous tracking of a person's position or path",
        "reason": "At most a coarse zone estimate (capability C), and only after calibration and "
        "independent validation. Zone centres are display anchors, not measured positions.",
    },
    {
        "id": "AOA_TOF_POSITIONING",
        "claim": "Angle-of-arrival or time-of-flight positioning",
        "reason": "Angle-of-arrival needs antenna arrays with phase-coherent receivers; time-of-flight "
        "needs synchronised clocks or wide bandwidth. One antenna per board with no shared RF "
        "clock provides neither.",
    },
]


# ---------------------------------------------------------------------------
# Verification evidence (configs/verification_evidence.json)
# ---------------------------------------------------------------------------


class EvidenceError(ValueError):
    pass


_FLAGS = ("software_tested", "firmware_compiled", "hardware_tested", "through_wall_validated")


class _EvidenceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    software_tested: bool
    firmware_compiled: bool
    hardware_tested: bool
    through_wall_validated: bool
    # Test files (globs under backend/tests) that back software_tested.
    software_test_globs: list[str] = Field(default_factory=list, max_length=64)
    notes: list[str] = Field(default_factory=list, max_length=64)
    # For each flag: what evidence would change it (or what backs it).
    would_change: dict[str, str] = Field(default_factory=dict)

    @field_validator("would_change")
    @classmethod
    def _known_flags(cls, v: dict[str, str]) -> dict[str, str]:
        unknown = set(v) - set(_FLAGS)
        if unknown:
            raise ValueError(f"unknown evidence flags {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def _claims_are_backed(self) -> "_EvidenceEntry":
        # A software_tested claim must say which test files back it, so the
        # claim can be checked (tests/test_capabilities.py does).
        if self.software_tested and not self.software_test_globs:
            raise ValueError("software_tested=true requires software_test_globs")
        return self


class _EvidenceDoc(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)

    schema_id: str = Field(alias="schema")
    reviewed_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    policy: str
    capabilities: dict[str, _EvidenceEntry]


def _parse_evidence(evidence: Mapping[str, Any]) -> _EvidenceDoc:
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise EvidenceError(f"expected schema {EVIDENCE_SCHEMA!r}, got {evidence.get('schema')!r}")
    try:
        doc = _EvidenceDoc.model_validate(dict(evidence))
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise EvidenceError(f"invalid evidence at {loc or '<root>'}: {first.get('msg')}") from exc
    expected = {c.value for c in CapabilityId}
    if set(doc.capabilities) != expected:
        raise EvidenceError(
            f"evidence must cover exactly {sorted(expected)}; got {sorted(doc.capabilities)}"
        )
    return doc


def load_verification_evidence(path: Path | None = None) -> dict[str, Any]:
    """Load the evidence file as a plain dict (for :class:`CapabilityContext`).

    Raises :class:`EvidenceError` if the file is missing, too large, not JSON,
    or fails validation. Callers that must keep running should catch it and
    pass ``{}``: :func:`verification_for` then reports every level as False.
    """
    p = Path(path) if path is not None else DEFAULT_EVIDENCE_PATH
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise EvidenceError(f"evidence file unavailable: {exc}") from exc
    if size > MAX_EVIDENCE_BYTES:
        raise EvidenceError(f"evidence file exceeds {MAX_EVIDENCE_BYTES} bytes")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"evidence file unreadable: {exc}") from exc
    if not isinstance(obj, dict):
        raise EvidenceError("evidence file must contain a JSON object")
    _parse_evidence(obj)  # validate now so errors surface at load time
    return obj


def _verification_map(evidence: Mapping[str, Any] | None) -> dict[CapabilityId, VerificationStatus]:
    """Parse the evidence once and build every capability's VerificationStatus.

    Missing or invalid evidence yields all-False levels with a note saying
    why; it never yields a True.
    """
    if not evidence:
        note = "No verification evidence file loaded; every evidence level is False."
        return {c: VerificationStatus(notes=[note]) for c in CapabilityId}
    try:
        doc = _parse_evidence(evidence)
    except EvidenceError as exc:
        note = f"Verification evidence rejected ({exc}); every evidence level is False."
        return {c: VerificationStatus(notes=[note]) for c in CapabilityId}
    return {c: _entry_to_status(doc, c) for c in CapabilityId}


def verification_for(evidence: Mapping[str, Any] | None, capability: CapabilityId) -> VerificationStatus:
    """Evidence levels for one capability, taken only from the evidence dict."""
    return _verification_map(evidence)[capability]


def _entry_to_status(doc: _EvidenceDoc, capability: CapabilityId) -> VerificationStatus:
    entry = doc.capabilities[capability.value]
    notes = list(entry.notes)
    for flag in _FLAGS:
        text = entry.would_change.get(flag)
        if text:
            state = "true" if getattr(entry, flag) else "false"
            notes.append(f"{flag}={state}: {text}")
    notes.append(f"Evidence reviewed on {doc.reviewed_on} (configs/verification_evidence.json).")
    return VerificationStatus(
        software_tested=entry.software_tested,
        firmware_compiled=entry.firmware_compiled,
        hardware_tested=entry.hardware_tested,
        through_wall_validated=entry.through_wall_validated,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Context and builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityContext:
    """Snapshot of runtime facts. Built by the app runtime once per status tick."""

    source_mode: SourceMode | None
    source_state: SourceState
    live_frames_with_valid_layout: int  # frames from a LIVE source whose layout resolved
    connected_live_links: int
    baseline_valid: bool  # a valid quiet baseline exists for the *current* source/hardware
    calibration_detail: str | None
    zone_enabled: bool  # zone module: a model passed the enablement criteria and still matches
    zone_reasons: list[str] = field(default_factory=list)
    pose_status: PoseStatus = field(default_factory=PoseStatus)
    through_wall_status: str = "UNVERIFIED"
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept plain strings from callers (e.g. "LIVE") but store the enums,
        # so an unknown value fails here instead of producing a wrong status.
        if self.source_mode is not None and not isinstance(self.source_mode, SourceMode):
            object.__setattr__(self, "source_mode", SourceMode(self.source_mode))
        if not isinstance(self.source_state, SourceState):
            object.__setattr__(self, "source_state", SourceState(self.source_state))


def _source_phrase(mode: SourceMode | None) -> str:
    return "no source selected" if mode is None else f"source is {mode.value}"


def _acquisition(ctx: CapabilityContext) -> tuple[CapabilityState, list[str]]:
    mode, state = ctx.source_mode, ctx.source_state
    if mode is None:
        return CapabilityState.HARDWARE_REQUIRED, [
            "No source selected. Live acquisition needs ESP32 receivers connected over USB serial."
        ]
    if mode == SourceMode.REPLAY:
        return CapabilityState.HARDWARE_REQUIRED, [
            "Replay of a recording is not live acquisition; frames are re-played, not measured now.",
            "Connect ESP32 receivers and select the LIVE source to enable live acquisition.",
        ]
    if mode == SourceMode.SIMULATION:
        return CapabilityState.HARDWARE_REQUIRED, [
            "Simulation produces synthetic data; it is not live acquisition and not a measurement.",
            "Connect ESP32 receivers and select the LIVE source to enable live acquisition.",
        ]
    # LIVE
    if state == SourceState.DISCONNECTED:
        return CapabilityState.HARDWARE_REQUIRED, [
            "Live hardware disconnected; links are SENSOR_OFFLINE. The app does not fall back to replay or simulation."
        ]
    if state != SourceState.RUNNING:
        return CapabilityState.HARDWARE_REQUIRED, [f"Live source is {state.value}; no live frames are flowing."]
    if ctx.connected_live_links <= 0:
        return CapabilityState.HARDWARE_REQUIRED, ["Live source running but no receiver link is connected."]
    if ctx.live_frames_with_valid_layout <= 0:
        return CapabilityState.HARDWARE_REQUIRED, [
            "Live source connected but no frame with a documented CSI layout has been received yet "
            "(frames with unknown layouts are kept for debugging only)."
        ]
    return CapabilityState.ENABLED, [
        f"Live acquisition running: {ctx.connected_live_links} link(s) connected, "
        f"{ctx.live_frames_with_valid_layout} frame(s) with a documented CSI layout received."
    ]


def _motion(ctx: CapabilityContext) -> tuple[CapabilityState, list[str]]:
    mode, state = ctx.source_mode, ctx.source_state
    reasons: list[str] = []
    if mode == SourceMode.SIMULATION:
        reasons.append(f"{SIMULATION_ONLY_REASON}.")
    elif mode == SourceMode.REPLAY:
        reasons.append("Replay of recorded measurements, not live.")
    if ctx.calibration_detail:
        reasons.append(f"Calibration: {ctx.calibration_detail}")

    if not ctx.baseline_valid:
        if mode is None:
            reasons.insert(0, "No source selected.")
        reasons.append("Requires a valid quiet-room baseline recorded for the current source and hardware.")
        return CapabilityState.REQUIRES_CALIBRATION, reasons
    if mode is None:
        return CapabilityState.REQUIRES_CALIBRATION, ["No source selected.", *reasons]
    if state != SourceState.RUNNING:
        if mode == SourceMode.LIVE:
            return CapabilityState.HARDWARE_REQUIRED, [
                *reasons,
                f"Live source is {state.value}; links report SENSOR_OFFLINE, not 'no motion'.",
            ]
        return CapabilityState.DISABLED, [*reasons, f"Source is {state.value}; no data is flowing."]

    reasons.append(_SCOPE_NOTE)
    if ctx.through_wall_status != "VALIDATED":
        reasons.append(f"Through-wall behaviour: {ctx.through_wall_status} for this setup.")
    return CapabilityState.ENABLED, reasons


def _zone(ctx: CapabilityContext) -> tuple[CapabilityState, list[str]]:
    zr = list(ctx.zone_reasons)
    if ctx.source_mode == SourceMode.SIMULATION:
        return CapabilityState.DISABLED, [
            f"Zone estimation is never enabled on simulated data ({SIMULATION_ONLY_REASON}).",
            *zr,
        ]
    if not ctx.zone_enabled:
        if ctx.source_mode is None or not ctx.baseline_valid:
            return CapabilityState.DISABLED, [
                f"Disabled: {_source_phrase(ctx.source_mode)}; needs live hardware, a valid baseline, "
                "then a zone model that passes the predefined criteria on held-out sessions.",
                *zr,
            ]
        return CapabilityState.REQUIRES_VALIDATION, [
            "No zone model has passed the predefined enablement criteria on held-out sessions "
            "(configs/zone_enablement.toml).",
            *zr,
        ]
    if ctx.source_mode not in (SourceMode.LIVE, SourceMode.REPLAY) or ctx.source_state != SourceState.RUNNING:
        return CapabilityState.DISABLED, [
            f"A validated zone model exists but {_source_phrase(ctx.source_mode)} "
            f"({ctx.source_state.value}); no estimates are produced.",
            *zr,
        ]
    return CapabilityState.ENABLED, [
        "EXPERIMENTAL single-person zone estimate; the model passed its criteria on held-out sessions "
        "and still matches this hardware and room. It abstains when unsure. Zone centres are display "
        "anchors, not measured positions.",
        *zr,
    ]


def _pose(ctx: CapabilityContext) -> tuple[CapabilityState, list[str]]:
    ps = ctx.pose_status
    if ps.enabled:
        return CapabilityState.ENABLED, [
            f"EXPERIMENTAL research model {ps.model_id!r} passed every gate requirement. Outputs are shown "
            "only in the research panel, never in the validated live view."
        ]
    reasons = ["Disabled: no compatible pose model, weights and validation exist for this hardware "
               "(docs/MODEL_COMPATIBILITY.md)."]
    reasons.extend(ps.missing_requirements)
    return CapabilityState.DISABLED, reasons


_BUILDERS = (
    (CapabilityId.A_ACQUISITION, _acquisition),
    (CapabilityId.B_MOTION, _motion),
    (CapabilityId.C_ZONE, _zone),
    (CapabilityId.D_POSE, _pose),
)


def build_capabilities(ctx: CapabilityContext) -> list[CapabilityStatus]:
    """One status per capability, in A, B, C, D order."""
    verification = _verification_map(ctx.evidence)
    out: list[CapabilityStatus] = []
    for cap, fn in _BUILDERS:
        state, reasons = fn(ctx)
        out.append(
            CapabilityStatus(
                capability=cap,
                title=CAPABILITY_TITLES[cap],
                state=state,
                reasons=reasons,
                verification=verification[cap],
            )
        )
    return out
