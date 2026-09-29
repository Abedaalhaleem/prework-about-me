"""Predefined enablement criteria for zone estimation (capability C).

The criteria live in ``configs/zone_enablement.toml``. They were written
before any zone data or model existed, and this module only *reads* them.
Nothing here can relax a threshold.

``criteria_version`` is the first 16 hex characters of the SHA-256 of the
file's exact bytes. The bytes are read once, hashed, and the same bytes are
parsed, so the version always describes what was actually loaded. Any edit
to the file, even a comment, changes the version. Every model is bound to the
version it was evaluated under and goes back to ``DISABLED`` (reason
``CRITERIA_CHANGED``) when the file changes.

Loading is strict: unknown sections or keys, missing keys, wrong types
(``true`` is not a number, ``1`` is not a boolean) and out-of-range values
are all errors. A criteria file that cannot be loaded never enables anything.
"""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from ...config import AppConfig

__all__ = [
    "EMPTY_LABEL",
    "OUTSIDE_LABEL",
    "NON_ZONE_LABELS",
    "CRITERIA_VERSION_HEX_CHARS",
    "MAX_CRITERIA_FILE_BYTES",
    "CriteriaError",
    "ZoneCriteria",
    "criteria_version_of_bytes",
    "default_criteria_path",
    "load_criteria",
    "parse_criteria_bytes",
    "current_criteria_version",
]

EMPTY_LABEL = "EMPTY"
OUTSIDE_LABEL = "OUTSIDE_TARGET_ROOM"
# The code gives these two labels a fixed meaning ("no target zone").
NON_ZONE_LABELS: tuple[str, ...] = (EMPTY_LABEL, OUTSIDE_LABEL)

CRITERIA_VERSION_HEX_CHARS = 16
MAX_CRITERIA_FILE_BYTES = 64 * 1024


class CriteriaError(ValueError):
    """The criteria file is missing, unreadable or not exactly as expected."""


@dataclass(frozen=True)
class ZoneCriteria:
    """Parsed, validated contents of ``zone_enablement.toml``."""

    criteria_version: str
    source_path: str
    # [scope]
    max_participants: int
    scope_description: str
    # [data]
    required_non_zone_classes: tuple[str, ...]
    min_zones: int
    min_sessions_per_class_train: int
    min_sessions_per_class_validation: int
    min_sessions_per_class_test: int
    require_test_after_train_and_validation: bool
    min_test_windows_per_class: int
    trim_session_edges_s: float
    allow_synthetic_sessions: bool
    min_receivers: int
    # [thresholds]
    min_test_balanced_accuracy_non_abstained: float
    min_test_accuracy_wilson_lower_95: float
    max_test_abstention_rate: float
    max_empty_predicted_as_zone_rate: float
    max_outside_predicted_as_zone_rate: float
    min_per_zone_recall: float
    # [validation_tuning]
    threshold_grid: tuple[float, ...]
    max_validation_abstention_rate: float

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["required_non_zone_classes"] = list(self.required_non_zone_classes)
        d["threshold_grid"] = list(self.threshold_grid)
        return d


def criteria_version_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:CRITERIA_VERSION_HEX_CHARS]


def default_criteria_path(cfg: AppConfig | None = None) -> Path:
    cfg = cfg if cfg is not None else AppConfig()
    return cfg.resolve_path(cfg.zone.criteria_file)


# ---------------------------------------------------------------------------
# Strict field readers
# ---------------------------------------------------------------------------

_EXPECTED_KEYS: dict[str, frozenset[str]] = {
    "scope": frozenset({"max_participants", "description"}),
    "data": frozenset(
        {
            "required_non_zone_classes",
            "min_zones",
            "min_sessions_per_class_train",
            "min_sessions_per_class_validation",
            "min_sessions_per_class_test",
            "require_test_after_train_and_validation",
            "min_test_windows_per_class",
            "trim_session_edges_s",
            "allow_synthetic_sessions",
            "min_receivers",
        }
    ),
    "thresholds": frozenset(
        {
            "min_test_balanced_accuracy_non_abstained",
            "min_test_accuracy_wilson_lower_95",
            "max_test_abstention_rate",
            "max_empty_predicted_as_zone_rate",
            "max_outside_predicted_as_zone_rate",
            "min_per_zone_recall",
        }
    ),
    "validation_tuning": frozenset({"threshold_grid", "max_validation_abstention_rate"}),
}


def _int(sec: Mapping[str, Any], section: str, key: str, *, minimum: int) -> int:
    v = sec[key]
    # bool is a subclass of int in Python; a boolean is never a count.
    if isinstance(v, bool) or not isinstance(v, int):
        raise CriteriaError(f"[{section}].{key} must be an integer, got {type(v).__name__}")
    if v < minimum:
        raise CriteriaError(f"[{section}].{key} must be >= {minimum}, got {v}")
    return int(v)


def _float(sec: Mapping[str, Any], section: str, key: str, *, lo: float, hi: float) -> float:
    v = sec[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CriteriaError(f"[{section}].{key} must be a number, got {type(v).__name__}")
    f = float(v)
    if not math.isfinite(f) or not (lo <= f <= hi):
        raise CriteriaError(f"[{section}].{key} must be in [{lo}, {hi}], got {v}")
    return f


def _bool(sec: Mapping[str, Any], section: str, key: str) -> bool:
    v = sec[key]
    if not isinstance(v, bool):
        raise CriteriaError(f"[{section}].{key} must be a boolean, got {type(v).__name__}")
    return v


def _str(sec: Mapping[str, Any], section: str, key: str) -> str:
    v = sec[key]
    if not isinstance(v, str):
        raise CriteriaError(f"[{section}].{key} must be a string, got {type(v).__name__}")
    return v


def parse_criteria_bytes(data: bytes, *, source_path: str = "<bytes>") -> ZoneCriteria:
    """Parse and validate criteria from the exact file bytes."""
    if len(data) > MAX_CRITERIA_FILE_BYTES:
        raise CriteriaError(f"criteria file larger than {MAX_CRITERIA_FILE_BYTES} bytes")
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CriteriaError(f"criteria file is not valid UTF-8 TOML: {exc}") from exc

    unknown_sections = sorted(set(doc) - set(_EXPECTED_KEYS))
    if unknown_sections:
        raise CriteriaError(f"unknown section(s) in criteria file: {unknown_sections}")
    for section, keys in _EXPECTED_KEYS.items():
        sec = doc.get(section)
        if not isinstance(sec, dict):
            raise CriteriaError(f"criteria file needs a [{section}] table")
        missing = sorted(keys - set(sec))
        extra = sorted(set(sec) - keys)
        if missing:
            raise CriteriaError(f"[{section}] is missing key(s): {missing}")
        if extra:
            raise CriteriaError(f"[{section}] has unknown key(s): {extra}")

    scope, dat, thr, tun = doc["scope"], doc["data"], doc["thresholds"], doc["validation_tuning"]

    max_participants = _int(scope, "scope", "max_participants", minimum=1)
    if max_participants != 1:
        # The software only supports a single moving participant; a file that
        # claims more would describe something this code does not do.
        raise CriteriaError("[scope].max_participants must be 1 (single moving participant)")

    classes = dat["required_non_zone_classes"]
    if not isinstance(classes, list) or not all(isinstance(c, str) for c in classes):
        raise CriteriaError("[data].required_non_zone_classes must be a list of strings")
    if len(set(classes)) != len(classes) or set(classes) != set(NON_ZONE_LABELS):
        raise CriteriaError(
            f"[data].required_non_zone_classes must be exactly {list(NON_ZONE_LABELS)} (the code gives "
            f"these labels a fixed meaning), got {classes}"
        )

    grid_raw = tun["threshold_grid"]
    if not isinstance(grid_raw, list) or not grid_raw:
        raise CriteriaError("[validation_tuning].threshold_grid must be a non-empty list")
    grid: list[float] = []
    for i, g in enumerate(grid_raw):
        if isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(float(g)):
            raise CriteriaError(f"[validation_tuning].threshold_grid[{i}] must be a finite number")
        if not 0.0 <= float(g) <= 1.0:
            raise CriteriaError(f"[validation_tuning].threshold_grid[{i}] must be in [0, 1]")
        grid.append(float(g))
    if any(b <= a for a, b in zip(grid, grid[1:])):
        raise CriteriaError("[validation_tuning].threshold_grid must be strictly increasing")

    return ZoneCriteria(
        criteria_version=criteria_version_of_bytes(data),
        source_path=source_path,
        max_participants=max_participants,
        scope_description=_str(scope, "scope", "description"),
        required_non_zone_classes=tuple(classes),
        min_zones=_int(dat, "data", "min_zones", minimum=2),
        min_sessions_per_class_train=_int(dat, "data", "min_sessions_per_class_train", minimum=1),
        min_sessions_per_class_validation=_int(dat, "data", "min_sessions_per_class_validation", minimum=1),
        min_sessions_per_class_test=_int(dat, "data", "min_sessions_per_class_test", minimum=1),
        require_test_after_train_and_validation=_bool(dat, "data", "require_test_after_train_and_validation"),
        min_test_windows_per_class=_int(dat, "data", "min_test_windows_per_class", minimum=1),
        trim_session_edges_s=_float(dat, "data", "trim_session_edges_s", lo=0.0, hi=3600.0),
        allow_synthetic_sessions=_bool(dat, "data", "allow_synthetic_sessions"),
        min_receivers=_int(dat, "data", "min_receivers", minimum=1),
        min_test_balanced_accuracy_non_abstained=_float(
            thr, "thresholds", "min_test_balanced_accuracy_non_abstained", lo=0.0, hi=1.0),
        min_test_accuracy_wilson_lower_95=_float(thr, "thresholds", "min_test_accuracy_wilson_lower_95",
                                                 lo=0.0, hi=1.0),
        max_test_abstention_rate=_float(thr, "thresholds", "max_test_abstention_rate", lo=0.0, hi=1.0),
        max_empty_predicted_as_zone_rate=_float(thr, "thresholds", "max_empty_predicted_as_zone_rate",
                                                lo=0.0, hi=1.0),
        max_outside_predicted_as_zone_rate=_float(thr, "thresholds", "max_outside_predicted_as_zone_rate",
                                                  lo=0.0, hi=1.0),
        min_per_zone_recall=_float(thr, "thresholds", "min_per_zone_recall", lo=0.0, hi=1.0),
        threshold_grid=tuple(grid),
        max_validation_abstention_rate=_float(tun, "validation_tuning", "max_validation_abstention_rate",
                                              lo=0.0, hi=1.0),
    )


def load_criteria(path: Path | str | None = None) -> ZoneCriteria:
    """Load ``path`` (default: ``configs/zone_enablement.toml``) strictly."""
    p = Path(path) if path is not None else default_criteria_path()
    try:
        with p.open("rb") as fh:
            data = fh.read(MAX_CRITERIA_FILE_BYTES + 1)
    except OSError as exc:
        raise CriteriaError(f"cannot read criteria file {p}: {exc.strerror or exc}") from exc
    return parse_criteria_bytes(data, source_path=str(p))


def current_criteria_version(path: Path | str | None = None) -> str | None:
    """Version of the criteria file as it is *now*, or ``None`` if it cannot
    be loaded (which keeps every model disabled)."""
    try:
        return load_criteria(path).criteria_version
    except CriteriaError:
        return None
