"""Interface for an optional pose research model (capability D).

Nothing here implements a pose model; this release ships none (see
``docs/MODEL_COMPATIBILITY.md``). The interface exists so that a future,
gated model can only produce outputs that:

* are always labelled ``EXPERIMENTAL``;
* carry full :class:`~roomsense.schemas.Provenance` plus the model id and the
  hash of the manifest that admitted it;
* are routed to a separate research panel and can never claim to belong in
  the validated live view (``validated_live_view`` is the literal ``False``);
* are produced only while :func:`~roomsense.inference.pose.gate.evaluate_pose_gate`
  reports ``enabled=True`` for that same model.

Keypoint ``score`` values are whatever the model emits. They are scores, not
probabilities, and the UI must not present them as confidence in a person
being there.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ...schemas import PoseStatus, Provenance

__all__ = [
    "EXPERIMENTAL_DISCLAIMER",
    "PoseKeypoint",
    "PoseOutput",
    "PoseModel",
    "PoseGateClosedError",
    "PoseOutputRejected",
    "run_experimental_inference",
]

# Defined once as a Literal type so the field below cannot drift from the
# exported constant.
_Disclaimer = Literal[
    "EXPERIMENTAL research output from a gated model. Not validated for this room, "
    "not an image, not identification, not tracking. Never shown in the validated live view."
]
EXPERIMENTAL_DISCLAIMER: str = get_args(_Disclaimer)[0]


class PoseKeypoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    # None = the model produced no value for this joint.
    x: float | None = None
    y: float | None = None
    z: float | None = None
    score: float | None = None  # model score, NOT a probability

    @field_validator("x", "y", "z", "score")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("keypoint values must be finite or null")
        return v


class PoseOutput(BaseModel):
    """One experimental pose output. Every field that marks it as research
    output is a ``Literal`` so it cannot be constructed any other way."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Literal["EXPERIMENTAL"] = "EXPERIMENTAL"
    display_route: Literal["EXPERIMENTAL_RESEARCH_PANEL"] = "EXPERIMENTAL_RESEARCH_PANEL"
    validated_live_view: Literal[False] = False
    model_id: str = Field(min_length=1, max_length=64)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coordinate_frame: str = Field(min_length=1, max_length=128)
    keypoints: list[PoseKeypoint] = Field(max_length=128)
    provenance: Provenance
    disclaimer: _Disclaimer = EXPERIMENTAL_DISCLAIMER


@runtime_checkable
class PoseModel(Protocol):
    """What a pose research model adapter must provide.

    ``manifest`` is the validated manifest dict that admitted the model.
    ``infer`` receives one processing window and returns a plain dict with
    exactly ``{"keypoints": [...], "coordinate_frame": "..."}``. Labelling,
    provenance and routing are added by :func:`run_experimental_inference`,
    never by the model.
    """

    @property
    def manifest(self) -> dict[str, Any]: ...

    def infer(self, window: Any) -> dict[str, Any]: ...


class PoseGateClosedError(RuntimeError):
    """Inference was requested while the capability-D gate is closed."""


class PoseOutputRejected(ValueError):
    """The model returned something that is not a valid experimental output."""


_MODEL_KEYS = frozenset({"keypoints", "coordinate_frame"})


def run_experimental_inference(
    model: PoseModel,
    window: Any,
    *,
    status: PoseStatus,
    provenance: Provenance,
) -> PoseOutput:
    """Run ``model`` on ``window`` only if the gate admitted this model.

    Raises :class:`PoseGateClosedError` when the gate is closed or admitted a
    different model, and :class:`PoseOutputRejected` when the model output
    tries to set anything beyond keypoints and coordinate frame (for example
    a ``validated_live_view`` flag or its own label).
    """
    if not status.enabled:
        raise PoseGateClosedError(
            "capability D is disabled: " + ("; ".join(status.missing_requirements) or "gate closed")
        )
    manifest = model.manifest
    model_id = manifest.get("model_id") if isinstance(manifest, dict) else None
    if model_id is None or model_id != status.model_id:
        raise PoseGateClosedError(f"gate admitted model {status.model_id!r}, not {model_id!r}")
    manifest_sha = (status.manifest or {}).get("manifest_sha256")
    if not isinstance(manifest_sha, str):
        raise PoseGateClosedError("gate status carries no manifest hash")

    raw = model.infer(window)
    if not isinstance(raw, dict):
        raise PoseOutputRejected(f"model returned {type(raw).__name__}, expected dict")
    extra = set(raw) - _MODEL_KEYS
    if extra:
        raise PoseOutputRejected(f"model output may only contain {sorted(_MODEL_KEYS)}; got extra {sorted(extra)}")
    try:
        return PoseOutput(
            model_id=model_id,
            manifest_sha256=manifest_sha,
            coordinate_frame=raw.get("coordinate_frame"),  # type: ignore[arg-type]
            keypoints=raw.get("keypoints"),  # type: ignore[arg-type]
            provenance=provenance.model_copy(update={"model_version": model_id}),
        )
    except ValidationError as exc:
        raise PoseOutputRejected(str(exc)) from exc
