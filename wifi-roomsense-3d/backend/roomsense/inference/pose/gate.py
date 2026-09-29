"""The capability-D (pose research) gate.

:func:`evaluate_pose_gate` answers one question: may a pose research model run
on the current hardware? It returns ``enabled=True`` only if **every**
requirement below holds; otherwise it returns ``enabled=False`` and lists every
unmet requirement, so the UI can say exactly what is missing.

=====================================  =======================================
Requirement id                         Holds when
=====================================  =======================================
R01_MANIFEST_VALID                     the manifest file exists and passes strict validation
R02_MODEL_INSTALLED                    the manifest says ``installed: true``
R03_LICENSE                            a licence is named and it is not "unknown"
R04_WEIGHTS_INSIDE_DATA_DIR            the weights file resolves inside the app data directory
R05_WEIGHTS_SHA256                     the weights file exists and its SHA-256 matches the manifest
R06_SAFE_WEIGHTS_FORMAT_AND_RUNTIME    the format is not pickle-based and this build has a runtime for it
R07_HW_CSI_SOURCE                      the runtime CSI source equals the trained one
R08_HW_PACKET_FORMAT                   the runtime record format equals the trained one
R09_HW_TX_ANTENNAS                     transmit antenna count matches
R10_HW_RX_ANTENNAS                     receive antenna count matches
R11_HW_LINKS                           number of synchronised links matches
R12_HW_SUBCARRIERS                     valid subcarrier count matches
R13_HW_SAMPLE_RATE                     measured packet rate is within tolerance of the trained rate
R14_HW_PHASE                           calibrated phase is available if the model needs it
R15_VALIDATION_INDEPENDENT_TEST        metrics come from an independent (session/person/day) test set
R16_VALIDATION_MEASURED_HERE           metrics were measured with this hardware in this environment
R17_VALIDATION_REAL_DATA               evidence names a dataset and metrics and is not synthetic
=====================================  =======================================

**This release ships no pose model** (``configs/pose_model_manifest.json`` has
``installed: false``) and **no pose inference runtime**
(:data:`AVAILABLE_INFERENCE_BACKENDS` is empty), so the gate is always closed.
See ``docs/MODEL_COMPATIBILITY.md`` for the research behind that.

Thread safety: the function is safe to call from several threads. Its only
shared state is a small weights-hash cache guarded by a lock, so a status
poll does not rehash a large file that has not changed.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping, TypedDict

from ...config import AppConfig
from ...csi_layouts import layout_from_id
from ...schemas import PoseStatus
from .manifest import (
    PICKLE_BASED_WEIGHT_SUFFIXES,
    UNACCEPTABLE_LICENSES,
    WEIGHTS_FORMAT_SUFFIX,
    LoadedManifest,
    ManifestError,
    PoseModelManifest,
    load_manifest,
)

__all__ = [
    "REQUIREMENT_IDS",
    "AVAILABLE_INFERENCE_BACKENDS",
    "RuntimeHardware",
    "default_manifest_path",
    "default_data_dir",
    "runtime_hardware_profile",
    "evaluate_pose_gate",
]

REQUIREMENT_IDS: tuple[str, ...] = (
    "R01_MANIFEST_VALID",
    "R02_MODEL_INSTALLED",
    "R03_LICENSE",
    "R04_WEIGHTS_INSIDE_DATA_DIR",
    "R05_WEIGHTS_SHA256",
    "R06_SAFE_WEIGHTS_FORMAT_AND_RUNTIME",
    "R07_HW_CSI_SOURCE",
    "R08_HW_PACKET_FORMAT",
    "R09_HW_TX_ANTENNAS",
    "R10_HW_RX_ANTENNAS",
    "R11_HW_LINKS",
    "R12_HW_SUBCARRIERS",
    "R13_HW_SAMPLE_RATE",
    "R14_HW_PHASE",
    "R15_VALIDATION_INDEPENDENT_TEST",
    "R16_VALIDATION_MEASURED_HERE",
    "R17_VALIDATION_REAL_DATA",
)

# Weight formats this build can execute. Deliberately empty: no ONNX or
# safetensors inference engine is a dependency of this release, and adding
# one is a reviewed change (pyproject.toml), not something a manifest can
# switch on.
AVAILABLE_INFERENCE_BACKENDS: frozenset[str] = frozenset()


class RuntimeHardware(TypedDict, total=False):
    """What the *current* acquisition setup provides. A missing key or a
    ``None`` value means "unavailable" and never matches a requirement."""

    csi_source: str | None
    packet_format: str | None
    tx_antennas: int | None
    rx_antennas: int | None
    links: int | None
    subcarriers: int | None
    sample_rate_hz: float | None  # measured, not configured
    phase_available: bool | None


def default_manifest_path() -> Path:
    cfg = AppConfig()
    return cfg.resolve_path(cfg.pose.manifest_file)


def default_data_dir() -> Path:
    return AppConfig().storage.resolved_data_dir()


def runtime_hardware_profile(
    *,
    layout_id: str | None,
    packet_format: str | None,
    measured_rate_hz: float | None,
    links: int | None,
    phase_available: bool = False,
) -> RuntimeHardware:
    """Describe the current ESP32 setup in gate terms.

    Antenna counts come from the chip family: the documented ESP32 CSI
    layouts (classic and ESP32-C5) describe one receive chain, so each link is
    one TX antenna to one RX antenna. Subcarriers are the occupied (valid)
    subcarriers of the documented layout. Phase defaults to unavailable:
    separate ESP32 boards share no RF clock, and the processing pipeline does
    not use phase (``processing.use_phase = False``).
    """
    layout = layout_from_id(layout_id)
    if layout is None:
        return RuntimeHardware(
            csi_source=None,
            packet_format=packet_format,
            tx_antennas=None,
            rx_antennas=None,
            links=links,
            subcarriers=None,
            sample_rate_hz=measured_rate_hz,
            phase_available=phase_available,
        )
    return RuntimeHardware(
        csi_source=f"esp32-{layout.family}-{layout.segment_name.lower()}",
        packet_format=packet_format,
        tx_antennas=1,
        rx_antennas=1,
        links=links,
        subcarriers=len(layout.valid_k),
        sample_rate_hz=measured_rate_hz,
        phase_available=phase_available,
    )


# ---------------------------------------------------------------------------
# Weights hashing (streamed, cached by path+size+mtime)
# ---------------------------------------------------------------------------

_HASH_CHUNK = 1 << 20
_HASH_CACHE_MAX = 16
_hash_cache: "OrderedDict[tuple[str, int, int], str]" = OrderedDict()
_hash_lock = threading.Lock()


def _sha256_file(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _hash_lock:
        cached = _hash_cache.get(key)
        if cached is not None:
            _hash_cache.move_to_end(key)
            return cached
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    digest = h.hexdigest()
    with _hash_lock:
        _hash_cache[key] = digest
        while len(_hash_cache) > _HASH_CACHE_MAX:
            _hash_cache.popitem(last=False)
    return digest


# ---------------------------------------------------------------------------
# Runtime value accessors: anything malformed is treated as unavailable.
# ---------------------------------------------------------------------------


def _rt_int(rt: Mapping[str, Any], key: str) -> int | None:
    v = rt.get(key)
    # bool is an int subclass; True antennas is not a count.
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _rt_float(rt: Mapping[str, Any], key: str) -> float | None:
    v = rt.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _rt_str(rt: Mapping[str, Any], key: str) -> str | None:
    v = rt.get(key)
    return v if isinstance(v, str) and v.strip() else None


def _rt_bool(rt: Mapping[str, Any], key: str) -> bool | None:
    v = rt.get(key)
    return v if isinstance(v, bool) else None


# ---------------------------------------------------------------------------
# Individual checks. Each returns None when satisfied, else a reason.
# ---------------------------------------------------------------------------


def _check_license(m: PoseModelManifest) -> str | None:
    lic = m.license.strip()
    if lic.lower() in UNACCEPTABLE_LICENSES:
        return f"licence is {m.license!r}; the model's terms of use must be known"
    return None


def _resolve_weights(m: PoseModelManifest, data_dir: Path) -> tuple[Path | None, str | None]:
    try:
        root = data_dir.resolve()
        raw = Path(m.weights_path)
        candidate = raw if raw.is_absolute() else root / raw
        # resolve() follows symlinks, so a link inside the data dir that points
        # elsewhere is caught here as well.
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        # RuntimeError: symlink loop; ValueError: e.g. an embedded NUL byte.
        return None, f"weights path {m.weights_path!r} cannot be resolved: {exc}"
    if not resolved.is_relative_to(root):
        return None, f"weights path {m.weights_path!r} resolves outside the data directory {root}"
    return resolved, None


def _check_weights_hash(resolved: Path | None, m: PoseModelManifest) -> str | None:
    if resolved is None:
        return "not checked: weights path is not inside the data directory"
    if not resolved.exists():
        return f"weights file not found: {resolved}"
    if not resolved.is_file():
        return f"weights path is not a regular file: {resolved}"
    try:
        actual = _sha256_file(resolved)
    except OSError as exc:
        return f"weights file could not be read: {exc}"
    if actual != m.weights_sha256:
        return f"SHA-256 mismatch: manifest {m.weights_sha256[:16]}..., file {actual[:16]}..."
    return None


def _check_format(m: PoseModelManifest, backends: frozenset[str]) -> str | None:
    suffix = Path(m.weights_path).suffix.lower()
    if suffix in PICKLE_BASED_WEIGHT_SUFFIXES:
        return f"weights suffix {suffix!r} is a pickle-based format, which can execute code when loaded"
    expected = WEIGHTS_FORMAT_SUFFIX[m.weights_format]
    if suffix != expected:
        return f"weights suffix {suffix!r} does not match declared format {m.weights_format!r} ({expected})"
    if m.weights_format not in backends:
        return f"this build has no inference runtime for {m.weights_format!r} (none is shipped in this release)"
    return None


def _check_equal_str(rt: Mapping[str, Any], key: str, required: str) -> str | None:
    have = _rt_str(rt, key)
    if have is None:
        return f"runtime {key} unavailable; model requires {required!r}"
    if have.strip().lower() != required.strip().lower():
        return f"runtime {key} is {have!r}; model requires {required!r}"
    return None


def _check_equal_int(rt: Mapping[str, Any], key: str, required: int) -> str | None:
    have = _rt_int(rt, key)
    if have is None:
        return f"runtime {key} unavailable; model requires {required}"
    if have != required:
        return f"runtime has {key}={have}; model requires {required}"
    return None


def _check_rate(rt: Mapping[str, Any], m: PoseModelManifest) -> str | None:
    need = m.input.sample_rate_hz
    tol = m.input.sample_rate_tolerance_fraction
    have = _rt_float(rt, "sample_rate_hz")
    if have is None:
        return f"measured packet rate unavailable; model requires {need:g} Hz ±{tol:.0%}"
    if abs(have - need) > tol * need:
        return f"measured packet rate {have:.1f} Hz is outside {need:g} Hz ±{tol:.0%}"
    return None


def _check_phase(rt: Mapping[str, Any], m: PoseModelManifest) -> str | None:
    if not m.input.phase_required:
        return None
    have = _rt_bool(rt, "phase_available")
    if have is not True:
        return "model requires calibrated CSI phase; runtime phase is " + (
            "unavailable" if have is None else "not available (no phase synchronisation)"
        )
    return None


def _check_validation(m: PoseModelManifest) -> dict[str, str | None]:
    v = m.validation
    out: dict[str, str | None] = {
        "R15_VALIDATION_INDEPENDENT_TEST": None
        if v.independent_test
        else "no independent test set (test data must not share session, person or day with training data)",
        "R16_VALIDATION_MEASURED_HERE": None
        if v.measured_in_this_environment
        else "metrics were not measured with this hardware in this environment (published numbers do not transfer)",
    }
    problems = []
    if v.uses_synthetic_data:
        problems.append("evidence uses synthetic data, which never counts as validation")
    if not (v.dataset and v.dataset.strip()):
        problems.append("no evaluation dataset named")
    if not v.metrics:
        problems.append("no metrics recorded")
    out["R17_VALIDATION_REAL_DATA"] = "; ".join(problems) if problems else None
    return out


def _closed(reasons: dict[str, str], *, model_id: str | None, manifest: dict[str, Any] | None) -> PoseStatus:
    missing = [f"{rid}: {reasons[rid]}" for rid in REQUIREMENT_IDS if rid in reasons]
    return PoseStatus(enabled=False, model_id=model_id, missing_requirements=missing, manifest=manifest)


def evaluate_pose_gate(
    manifest_path: Path | None,
    runtime_hw: Mapping[str, Any] | None,
    *,
    data_dir: Path | None = None,
    available_backends: frozenset[str] | None = None,
) -> PoseStatus:
    """Evaluate every capability-D requirement.

    ``manifest_path`` defaults to ``configs/pose_model_manifest.json``;
    ``data_dir`` defaults to the configured storage directory;
    ``available_backends`` defaults to :data:`AVAILABLE_INFERENCE_BACKENDS`
    (empty in this release). Tests may pass explicit values; production code
    should not override ``available_backends``.
    """
    path = Path(manifest_path) if manifest_path is not None else default_manifest_path()
    root = Path(data_dir) if data_dir is not None else default_data_dir()
    backends = AVAILABLE_INFERENCE_BACKENDS if available_backends is None else frozenset(available_backends)
    rt: Mapping[str, Any] = runtime_hw if isinstance(runtime_hw, Mapping) else {}

    try:
        loaded: LoadedManifest = load_manifest(path)
    except ManifestError as exc:
        reasons = {"R01_MANIFEST_VALID": f"manifest {path.name} rejected ({exc.code}): {exc.detail}"}
        for rid in REQUIREMENT_IDS[1:]:
            reasons[rid] = "not evaluated: no valid model manifest"
        return _closed(reasons, model_id=None, manifest=None)

    manifest_dict = dict(loaded.raw)
    manifest_dict["manifest_sha256"] = loaded.file_sha256
    if not loaded.installed:
        why = getattr(loaded.manifest, "reason", "no model installed")
        reasons = {"R02_MODEL_INSTALLED": f"no pose model is installed: {why}"}
        for rid in REQUIREMENT_IDS[2:]:
            reasons[rid] = "not evaluated: no model is installed"
        return _closed(reasons, model_id=None, manifest=manifest_dict)

    m = loaded.manifest
    assert isinstance(m, PoseModelManifest)
    reasons_opt: dict[str, str | None] = {}
    reasons_opt["R03_LICENSE"] = _check_license(m)
    resolved, where = _resolve_weights(m, root)
    reasons_opt["R04_WEIGHTS_INSIDE_DATA_DIR"] = where
    reasons_opt["R05_WEIGHTS_SHA256"] = _check_weights_hash(resolved, m)
    reasons_opt["R06_SAFE_WEIGHTS_FORMAT_AND_RUNTIME"] = _check_format(m, backends)
    reasons_opt["R07_HW_CSI_SOURCE"] = _check_equal_str(rt, "csi_source", m.input.csi_source)
    reasons_opt["R08_HW_PACKET_FORMAT"] = _check_equal_str(rt, "packet_format", m.input.packet_format)
    reasons_opt["R09_HW_TX_ANTENNAS"] = _check_equal_int(rt, "tx_antennas", m.input.tx_antennas)
    reasons_opt["R10_HW_RX_ANTENNAS"] = _check_equal_int(rt, "rx_antennas", m.input.rx_antennas)
    reasons_opt["R11_HW_LINKS"] = _check_equal_int(rt, "links", m.input.links)
    reasons_opt["R12_HW_SUBCARRIERS"] = _check_equal_int(rt, "subcarriers", m.input.subcarriers)
    reasons_opt["R13_HW_SAMPLE_RATE"] = _check_rate(rt, m)
    reasons_opt["R14_HW_PHASE"] = _check_phase(rt, m)
    reasons_opt.update(_check_validation(m))

    reasons = {rid: r for rid, r in reasons_opt.items() if r is not None}
    if reasons:
        return _closed(reasons, model_id=m.model_id, manifest=manifest_dict)
    return PoseStatus(enabled=True, model_id=m.model_id, missing_requirements=[], manifest=manifest_dict)
