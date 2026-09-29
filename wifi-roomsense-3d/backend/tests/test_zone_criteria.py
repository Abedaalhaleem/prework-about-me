"""Zone enablement criteria: strict loading and criteria_version hashing.

Software tests only. They check that the predefined file is read exactly as
written and that any change to it changes the version models are bound to.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from roomsense.config import AppConfig
from roomsense.inference.zone.criteria import (
    CriteriaError,
    current_criteria_version,
    default_criteria_path,
    load_criteria,
    parse_criteria_bytes,
)

REAL = default_criteria_path()


def _copy(tmp_path: Path, text: str | None = None) -> Path:
    p = tmp_path / "zone_enablement.toml"
    p.write_bytes(REAL.read_bytes() if text is None else text.encode("utf-8"))
    return p


def test_default_path_is_the_predefined_file():
    assert REAL == AppConfig().resolve_path("configs/zone_enablement.toml")
    assert REAL.is_file()


def test_real_file_loads_with_the_predefined_values():
    c = load_criteria()
    # Values as written in configs/zone_enablement.toml (read, never changed here).
    assert c.max_participants == 1
    assert c.required_non_zone_classes == ("EMPTY", "OUTSIDE_TARGET_ROOM")
    assert c.min_zones == 2
    assert (c.min_sessions_per_class_train, c.min_sessions_per_class_validation,
            c.min_sessions_per_class_test) == (3, 1, 1)
    assert c.require_test_after_train_and_validation is True
    assert c.min_test_windows_per_class == 30
    assert c.trim_session_edges_s == 5.0
    assert c.allow_synthetic_sessions is False
    assert c.min_receivers == 3
    assert c.min_test_balanced_accuracy_non_abstained == 0.80
    assert c.min_test_accuracy_wilson_lower_95 == 0.70
    assert c.max_test_abstention_rate == 0.30
    assert c.max_empty_predicted_as_zone_rate == 0.05
    assert c.max_outside_predicted_as_zone_rate == 0.10
    assert c.min_per_zone_recall == 0.60
    assert c.threshold_grid == (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
    assert c.max_validation_abstention_rate == 0.30


def test_criteria_version_is_sha256_prefix_of_the_exact_bytes():
    data = REAL.read_bytes()
    assert load_criteria().criteria_version == hashlib.sha256(data).hexdigest()[:16]
    assert current_criteria_version(REAL) == load_criteria().criteria_version


def test_tampering_changes_the_criteria_version(tmp_path):
    original = load_criteria().criteria_version
    text = REAL.read_text("utf-8")
    # Lowering a threshold after seeing results...
    lowered = _copy(tmp_path, text.replace("min_test_balanced_accuracy_non_abstained = 0.80",
                                           "min_test_balanced_accuracy_non_abstained = 0.50"))
    assert load_criteria(lowered).min_test_balanced_accuracy_non_abstained == 0.50
    assert load_criteria(lowered).criteria_version != original
    # ...and even a comment-only edit changes the version.
    commented = tmp_path / "commented.toml"
    commented.write_text(text + "\n# harmless looking comment\n", "utf-8")
    assert load_criteria(commented).criteria_version != original
    # An identical copy keeps the version.
    assert load_criteria(_copy(tmp_path)).criteria_version == original


def test_missing_or_unreadable_file_gives_no_version(tmp_path):
    assert current_criteria_version(tmp_path / "nope.toml") is None
    with pytest.raises(CriteriaError):
        load_criteria(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    "old,new,msg",
    [
        ("min_zones = 2", "min_zones = 2\nsurprise = 1", "unknown key"),
        ("min_zones = 2\n", "", "missing key"),
        ("allow_synthetic_sessions = false", "allow_synthetic_sessions = 0", "must be a boolean"),
        ("min_receivers = 3", "min_receivers = true", "must be an integer"),
        ("min_receivers = 3", 'min_receivers = "3"', "must be an integer"),
        ("max_test_abstention_rate = 0.30", "max_test_abstention_rate = 1.5", "must be in"),
        ("min_per_zone_recall = 0.60", "min_per_zone_recall = true", "must be a number"),
        ("max_participants = 1", "max_participants = 2", "must be 1"),
        ('required_non_zone_classes = ["EMPTY", "OUTSIDE_TARGET_ROOM"]', 'required_non_zone_classes = ["EMPTY"]',
         "exactly"),
        ("threshold_grid = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]", "threshold_grid = [0.5, 0.3]",
         "strictly increasing"),
        ("threshold_grid = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]", "threshold_grid = []", "non-empty"),
        ("[scope]", "[extra]\nx = 1\n\n[scope]", "unknown section"),
    ],
)
def test_strict_loading_rejects_malformed_files(tmp_path, old, new, msg):
    text = REAL.read_text("utf-8")
    assert old in text
    p = _copy(tmp_path, text.replace(old, new, 1))
    with pytest.raises(CriteriaError, match=msg):
        load_criteria(p)
    assert current_criteria_version(p) is None


def test_not_toml_is_rejected():
    with pytest.raises(CriteriaError, match="TOML"):
        parse_criteria_bytes(b"this is = = not toml")
    with pytest.raises(CriteriaError, match="UTF-8"):
        parse_criteria_bytes(b"\xff\xfe\x00")
