"""Capability D: pose research, gated and DISABLED in this release.

No compatible pose model and weights exist for this project's hardware
(single-antenna ESP32 boards, amplitude-only LLTF CSI, no phase
synchronisation, no antenna array). The research is in
``docs/MODEL_COMPATIBILITY.md``; the gate that keeps the capability off, and
lists why, is :func:`evaluate_pose_gate`.
"""

from .gate import (
    AVAILABLE_INFERENCE_BACKENDS,
    REQUIREMENT_IDS,
    RuntimeHardware,
    evaluate_pose_gate,
    runtime_hardware_profile,
)
from .interface import (
    PoseGateClosedError,
    PoseKeypoint,
    PoseModel,
    PoseOutput,
    PoseOutputRejected,
    run_experimental_inference,
)
from .manifest import (
    ManifestError,
    NotInstalledManifest,
    PoseModelManifest,
    PoseRequirementsDoc,
    load_manifest,
    load_requirements,
)

__all__ = [
    "AVAILABLE_INFERENCE_BACKENDS",
    "REQUIREMENT_IDS",
    "RuntimeHardware",
    "evaluate_pose_gate",
    "runtime_hardware_profile",
    "PoseGateClosedError",
    "PoseKeypoint",
    "PoseModel",
    "PoseOutput",
    "PoseOutputRejected",
    "run_experimental_inference",
    "ManifestError",
    "NotInstalledManifest",
    "PoseModelManifest",
    "PoseRequirementsDoc",
    "load_manifest",
    "load_requirements",
]
