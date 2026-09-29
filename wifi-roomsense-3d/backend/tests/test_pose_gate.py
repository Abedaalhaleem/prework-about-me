"""Tests for the capability-D (pose research) gate.

All manifests below except the shipped one are FAKE test fixtures written to a
temporary directory. The fake weights are a few fixed bytes; they are
not a model. Nothing here says anything about pose-estimation accuracy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from roomsense.config import REPO_ROOT
from roomsense.inference.pose import gate as gate_mod
from roomsense.inference.pose.gate import (
    AVAILABLE_INFERENCE_BACKENDS,
    REQUIREMENT_IDS,
    evaluate_pose_gate,
    runtime_hardware_profile,
)
from roomsense.inference.pose.manifest import MANIFEST_SCHEMA, load_requirements

SHIPPED_MANIFEST = REPO_ROOT / "configs" / "pose_model_manifest.json"
REQUIREMENTS_FILE = REPO_ROOT / "configs" / "pose_requirements.json"
CLASSIC_LAYOUT = "classic.lltf_only.sec_none.total128.LLTF64"
FAKE_WEIGHTS = b"FAKE-TEST-WEIGHTS-not-a-model\x00\x01\x02"


def esp32_runtime(rate_hz: float = 25.0, links: int = 1) -> dict[str, Any]:
    return dict(
        runtime_hardware_profile(
            layout_id=CLASSIC_LAYOUT, packet_format="roomsense-rscsi-v1", measured_rate_hz=rate_hz, links=links
        )
    )


def ids_of(status) -> set[str]:
    return {m.split(":", 1)[0] for m in status.missing_requirements}


def write_json(path: Path, obj: Any) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "models" / "pose").mkdir(parents=True)
    return d


def fake_manifest(data_dir: Path, *, weights_name: str = "fake.safetensors", **overrides: Any) -> dict[str, Any]:
    """A FAKE manifest that matches the ESP32 runtime exactly, with measured,
    independent validation. Tests break one thing at a time from here."""
    weights = data_dir / "models" / "pose" / weights_name
    weights.write_bytes(FAKE_WEIGHTS)
    m: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "installed": True,
        "model_id": "fake-test-model",
        "source_url": "https://example.invalid/fake-test-model",
        "source_commit": "0123456789abcdef0123456789abcdef01234567",
        "license": "MIT",
        "weights_path": f"models/pose/{weights_name}",
        "weights_format": "safetensors",
        "weights_sha256": hashlib.sha256(FAKE_WEIGHTS).hexdigest(),
        "input": {
            "csi_source": "esp32-classic-lltf",
            "packet_format": "roomsense-rscsi-v1",
            "tx_antennas": 1,
            "rx_antennas": 1,
            "links": 1,
            "subcarriers": 52,
            "sample_rate_hz": 25.0,
            "sample_rate_tolerance_fraction": 0.1,
            "phase_required": False,
            "window_frames": 50,
        },
        "output_keypoints": ["head", "l_hip", "r_hip"],
        "coordinate_frame": "fake-normalised-2d",
        "training_domain": "FAKE fixture for tests; never trained.",
        "validation": {
            "independent_test": True,
            "dataset": "fake-held-out-sessions",
            "split_description": "by session, person and day",
            "metrics": {"pck_at_0_2": 0.5},
            "measured_in_this_environment": True,
            "uses_synthetic_data": False,
            "report_path": None,
        },
        "compute": {"device": "cpu", "min_memory_mb": 256, "notes": None},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(m.get(key), dict):
            m[key] = {**m[key], **value}
        else:
            m[key] = value
    return m


def evaluate(tmp_path: Path, data_dir: Path, manifest: dict[str, Any], runtime=None, backends=frozenset()):
    path = write_json(tmp_path / "manifest.json", manifest)
    return evaluate_pose_gate(
        path, esp32_runtime() if runtime is None else runtime, data_dir=data_dir, available_backends=backends
    )


# ---------------------------------------------------------------------------
# The shipped state: no model, always disabled
# ---------------------------------------------------------------------------


def test_shipped_manifest_is_not_installed_and_gate_is_disabled():
    status = evaluate_pose_gate(SHIPPED_MANIFEST, esp32_runtime())
    assert status.enabled is False
    assert status.label == "EXPERIMENTAL"
    assert status.model_id is None
    # Every requirement is listed, in order, starting with "no model installed".
    assert [m.split(":", 1)[0] for m in status.missing_requirements] == list(REQUIREMENT_IDS[1:])
    assert "no pose model is installed" in status.missing_requirements[0]
    assert status.manifest is not None and status.manifest["installed"] is False
    assert len(status.manifest["manifest_sha256"]) == 64


def test_default_arguments_use_shipped_manifest_and_stay_disabled():
    status = evaluate_pose_gate(None, None)
    assert status.enabled is False
    assert "R02_MODEL_INSTALLED" in ids_of(status)


def test_release_ships_no_inference_backend():
    assert AVAILABLE_INFERENCE_BACKENDS == frozenset()


def test_missing_manifest_file_disables_with_every_requirement_listed(tmp_path: Path, data_dir: Path):
    status = evaluate_pose_gate(tmp_path / "does-not-exist.json", esp32_runtime(), data_dir=data_dir)
    assert status.enabled is False
    assert ids_of(status) == set(REQUIREMENT_IDS)
    assert "MISSING" in status.missing_requirements[0]
    assert status.manifest is None


def test_installed_false_manifest_disables(tmp_path: Path, data_dir: Path):
    path = write_json(tmp_path / "m.json", {"schema": MANIFEST_SCHEMA, "installed": False, "reason": "test"})
    status = evaluate_pose_gate(path, esp32_runtime(), data_dir=data_dir)
    assert status.enabled is False
    assert ids_of(status) == set(REQUIREMENT_IDS[1:])
    assert "test" in status.missing_requirements[0]


@pytest.mark.parametrize(
    "text, code",
    [
        ("{not json", "INVALID_JSON"),
        ("[1, 2]", "NOT_AN_OBJECT"),
        ('{"schema": "roomsense-pose-manifest-v1", "installed": false, "installed": true, "reason": "x"}', "DUPLICATE_KEY"),
        ('{"schema": "roomsense-pose-manifest-v1", "installed": false, "reason": "x", "n": NaN}', "NON_FINITE_NUMBER"),
        ('{"schema": "other-v9", "installed": false, "reason": "x"}', "WRONG_SCHEMA"),
        ('{"schema": "roomsense-pose-manifest-v1", "installed": 0, "reason": "x"}', "INSTALLED_FLAG"),
        ('{"schema": "roomsense-pose-manifest-v1", "installed": "false", "reason": "x"}', "INSTALLED_FLAG"),
        ('{"schema": "roomsense-pose-manifest-v1", "installed": false, "reason": "x", "extra": 1}', "SCHEMA_INVALID"),
    ],
)
def test_malformed_manifests_are_rejected(tmp_path: Path, data_dir: Path, text: str, code: str):
    path = tmp_path / "m.json"
    path.write_text(text, encoding="utf-8")
    status = evaluate_pose_gate(path, esp32_runtime(), data_dir=data_dir)
    assert status.enabled is False
    assert status.missing_requirements[0].startswith("R01_MANIFEST_VALID")
    assert code in status.missing_requirements[0]


def test_oversized_manifest_is_rejected_without_parsing(tmp_path: Path, data_dir: Path):
    path = tmp_path / "m.json"
    path.write_text('{"pad": "' + "x" * (300 * 1024) + '"}', encoding="utf-8")
    status = evaluate_pose_gate(path, esp32_runtime(), data_dir=data_dir)
    assert "TOO_LARGE" in status.missing_requirements[0]


def test_boolean_is_not_accepted_as_an_antenna_count(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, input={"rx_antennas": True})
    status = evaluate(tmp_path, data_dir, m)
    assert status.missing_requirements[0].startswith("R01_MANIFEST_VALID")
    assert "SCHEMA_INVALID" in status.missing_requirements[0]


def test_pickle_based_weights_format_cannot_be_declared(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, weights_format="pytorch")
    status = evaluate(tmp_path, data_dir, m)
    assert "R01_MANIFEST_VALID" in ids_of(status)


# ---------------------------------------------------------------------------
# Hardware mismatches: every one is listed
# ---------------------------------------------------------------------------


def test_intel5300_style_manifest_lists_each_hardware_mismatch(tmp_path: Path, data_dir: Path):
    # Shaped like the published 3x3-antenna, 30-subcarrier, 100 Hz, phase-using setups.
    m = fake_manifest(
        data_dir,
        input={
            "csi_source": "intel5300-csitool",
            "packet_format": "csitool-bfee",
            "tx_antennas": 3,
            "rx_antennas": 3,
            "links": 3,
            "subcarriers": 30,
            "sample_rate_hz": 100.0,
            "phase_required": True,
        },
    )
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert status.enabled is False
    assert status.model_id == "fake-test-model"
    expected = {
        "R07_HW_CSI_SOURCE",
        "R08_HW_PACKET_FORMAT",
        "R09_HW_TX_ANTENNAS",
        "R10_HW_RX_ANTENNAS",
        "R11_HW_LINKS",
        "R12_HW_SUBCARRIERS",
        "R13_HW_SAMPLE_RATE",
        "R14_HW_PHASE",
    }
    assert ids_of(status) == expected
    text = "\n".join(status.missing_requirements)
    assert "tx_antennas=1; model requires 3" in text
    assert "rx_antennas=1; model requires 3" in text
    assert "subcarriers=52; model requires 30" in text


def test_only_antenna_and_subcarrier_mismatch_listed_individually(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, input={"rx_antennas": 3, "subcarriers": 56})
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert ids_of(status) == {"R10_HW_RX_ANTENNAS", "R12_HW_SUBCARRIERS"}


def test_unavailable_runtime_values_never_match(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir)
    status = evaluate(tmp_path, data_dir, m, runtime={}, backends=frozenset({"safetensors"}))
    assert {
        "R07_HW_CSI_SOURCE",
        "R08_HW_PACKET_FORMAT",
        "R09_HW_TX_ANTENNAS",
        "R10_HW_RX_ANTENNAS",
        "R11_HW_LINKS",
        "R12_HW_SUBCARRIERS",
        "R13_HW_SAMPLE_RATE",
    } <= ids_of(status)
    assert "unavailable" in "\n".join(status.missing_requirements)


def test_malformed_runtime_values_are_treated_as_unavailable(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir)
    rt = esp32_runtime()
    rt.update(tx_antennas=True, rx_antennas="1", sample_rate_hz=float("nan"), links=1.0)
    status = evaluate(tmp_path, data_dir, m, runtime=rt, backends=frozenset({"safetensors"}))
    assert {"R09_HW_TX_ANTENNAS", "R10_HW_RX_ANTENNAS", "R11_HW_LINKS", "R13_HW_SAMPLE_RATE"} == ids_of(status)


@pytest.mark.parametrize("rate, ok", [(25.0, True), (22.6, True), (27.4, True), (22.4, False), (27.6, False), (100.0, False)])
def test_sample_rate_tolerance(tmp_path: Path, data_dir: Path, rate: float, ok: bool):
    m = fake_manifest(data_dir)  # 25 Hz +-10%
    status = evaluate(tmp_path, data_dir, m, runtime=esp32_runtime(rate_hz=rate), backends=frozenset({"safetensors"}))
    assert ("R13_HW_SAMPLE_RATE" in ids_of(status)) is (not ok)


def test_phase_required_but_unavailable(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, input={"phase_required": True})
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert ids_of(status) == {"R14_HW_PHASE"}


# ---------------------------------------------------------------------------
# Weights: location, integrity, format
# ---------------------------------------------------------------------------


def test_wrong_sha256_disables(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, weights_sha256="0" * 64)
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert status.enabled is False
    assert ids_of(status) == {"R05_WEIGHTS_SHA256"}
    assert "SHA-256 mismatch" in status.missing_requirements[0]


def test_missing_weights_file_disables(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir)
    (data_dir / "models" / "pose" / "fake.safetensors").unlink()
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert ids_of(status) == {"R05_WEIGHTS_SHA256"}
    assert "not found" in status.missing_requirements[0]


def test_weights_outside_data_dir_are_refused(tmp_path: Path, data_dir: Path):
    outside = tmp_path / "elsewhere.safetensors"
    outside.write_bytes(FAKE_WEIGHTS)
    for weights_path in (str(outside), "../elsewhere.safetensors"):
        m = fake_manifest(data_dir, weights_path=weights_path)
        status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
        assert {"R04_WEIGHTS_INSIDE_DATA_DIR", "R05_WEIGHTS_SHA256"} == ids_of(status), weights_path


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlink_escaping_data_dir_is_refused(tmp_path: Path, data_dir: Path):
    outside = tmp_path / "elsewhere.safetensors"
    outside.write_bytes(FAKE_WEIGHTS)
    link = data_dir / "models" / "pose" / "link.safetensors"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("cannot create symlink here")
    m = fake_manifest(data_dir)
    m["weights_path"] = "models/pose/link.safetensors"
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert "R04_WEIGHTS_INSIDE_DATA_DIR" in ids_of(status)


def test_pickle_suffix_is_refused_even_with_declared_safe_format(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, weights_name="model.pth")
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors", "onnx"}))
    assert ids_of(status) == {"R06_SAFE_WEIGHTS_FORMAT_AND_RUNTIME"}
    assert "pickle" in status.missing_requirements[0]


def test_suffix_must_match_declared_format(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, weights_name="model.onnx")  # declared safetensors
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors", "onnx"}))
    assert ids_of(status) == {"R06_SAFE_WEIGHTS_FORMAT_AND_RUNTIME"}


@pytest.mark.parametrize("lic", ["unknown", "UNKNOWN", "", "  ", "NOASSERTION", "none"])
def test_unknown_licence_disables(tmp_path: Path, data_dir: Path, lic: str):
    m = fake_manifest(data_dir, license=lic)
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert ids_of(status) == {"R03_LICENSE"}


def test_changed_weights_file_is_rehashed(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir)
    assert evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"})).enabled is True
    (data_dir / "models" / "pose" / "fake.safetensors").write_bytes(FAKE_WEIGHTS + b"tampered")
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert ids_of(status) == {"R05_WEIGHTS_SHA256"}


# ---------------------------------------------------------------------------
# Validation evidence
# ---------------------------------------------------------------------------


def test_fully_matching_manifest_without_measured_validation_is_disabled(tmp_path: Path, data_dir: Path):
    # Hardware, weights, licence and runtime all match; only the evidence is missing.
    m = fake_manifest(
        data_dir, validation={"independent_test": False, "measured_in_this_environment": False}
    )
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert status.enabled is False
    assert ids_of(status) == {"R15_VALIDATION_INDEPENDENT_TEST", "R16_VALIDATION_MEASURED_HERE"}


def test_published_numbers_alone_do_not_count(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, validation={"measured_in_this_environment": False})
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert ids_of(status) == {"R16_VALIDATION_MEASURED_HERE"}


def test_synthetic_or_empty_evidence_does_not_count(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, validation={"uses_synthetic_data": True, "metrics": {}, "dataset": None})
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert ids_of(status) == {"R17_VALIDATION_REAL_DATA"}
    text = status.missing_requirements[0]
    assert "synthetic" in text and "no metrics" in text and "no evaluation dataset" in text


def test_non_finite_metric_is_rejected(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir)
    path = tmp_path / "m.json"
    raw = json.dumps(m).replace('"pck_at_0_2": 0.5', '"pck_at_0_2": Infinity')
    path.write_text(raw, encoding="utf-8")
    status = evaluate_pose_gate(path, esp32_runtime(), data_dir=data_dir, available_backends=frozenset({"safetensors"}))
    assert "NON_FINITE_NUMBER" in status.missing_requirements[0]


def test_everything_matching_is_still_disabled_in_this_release(tmp_path: Path, data_dir: Path):
    # Even a perfect FAKE manifest stays closed: no inference runtime ships.
    m = fake_manifest(data_dir)
    status = evaluate(tmp_path, data_dir, m, backends=None)  # type: ignore[arg-type]
    assert status.enabled is False
    assert ids_of(status) == {"R06_SAFE_WEIGHTS_FORMAT_AND_RUNTIME"}
    assert "no inference runtime" in status.missing_requirements[0]


def test_gate_opens_only_when_every_requirement_holds(tmp_path: Path, data_dir: Path):
    # Test-only: an explicit backend set shows the gate logic is not simply
    # hard-wired off. Production code never passes available_backends.
    m = fake_manifest(data_dir)
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert status.enabled is True
    assert status.missing_requirements == []
    assert status.label == "EXPERIMENTAL"
    assert status.manifest is not None and len(status.manifest["manifest_sha256"]) == 64
    # ...and breaking any single part closes it again.
    for key, value in [
        ("license", "unknown"),
        ("weights_sha256", "f" * 64),
        ("input", {"rx_antennas": 2}),
        ("validation", {"independent_test": False}),
    ]:
        broken = copy.deepcopy(m)
        broken[key] = {**broken[key], **value} if isinstance(value, dict) else value
        assert evaluate(tmp_path, data_dir, broken, backends=frozenset({"safetensors"})).enabled is False, key


def test_concurrent_evaluation_is_consistent(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, input={"rx_antennas": 3})
    path = write_json(tmp_path / "manifest.json", m)
    results: list[tuple[bool, tuple[str, ...]]] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            st = evaluate_pose_gate(path, esp32_runtime(), data_dir=data_dir, available_backends=frozenset({"safetensors"}))
            with lock:
                results.append((st.enabled, tuple(st.missing_requirements)))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 40
    assert len(set(results)) == 1 and results[0][0] is False


def test_hash_cache_is_bounded(tmp_path: Path, data_dir: Path):
    for i in range(gate_mod._HASH_CACHE_MAX + 5):
        f = data_dir / f"w{i}.bin"
        f.write_bytes(bytes([i]))
        gate_mod._sha256_file(f)
    assert len(gate_mod._hash_cache) <= gate_mod._HASH_CACHE_MAX


# ---------------------------------------------------------------------------
# Runtime profile and the requirements document
# ---------------------------------------------------------------------------


def test_runtime_profile_for_classic_lltf():
    hw = esp32_runtime(rate_hz=25.3)
    assert hw == {
        "csi_source": "esp32-classic-lltf",
        "packet_format": "roomsense-rscsi-v1",
        "tx_antennas": 1,
        "rx_antennas": 1,
        "links": 1,
        "subcarriers": 52,
        "sample_rate_hz": 25.3,
        "phase_available": False,
    }


def test_runtime_profile_unknown_layout_reports_unavailable():
    hw = runtime_hardware_profile(layout_id="bogus", packet_format=None, measured_rate_hz=None, links=None)
    assert hw["csi_source"] is None and hw["subcarriers"] is None and hw["rx_antennas"] is None


def test_requirements_file_matches_gate_requirement_ids():
    doc = load_requirements(REQUIREMENTS_FILE)
    assert [r.id for r in doc.requirements] == list(REQUIREMENT_IDS)
    hw = doc.project_hardware
    assert (hw.tx_antennas_per_link, hw.rx_antennas_per_link) == (1, 1)
    assert hw.valid_subcarriers == esp32_runtime()["subcarriers"]
    assert hw.phase_available is False and hw.antenna_array is False
    assert doc.missing_for_this_hardware


# ---------------------------------------------------------------------------
# Hardening: malformed inputs never raise out of the gate
# ---------------------------------------------------------------------------


def test_nul_byte_in_weights_path_is_refused(tmp_path: Path, data_dir: Path):
    m = fake_manifest(data_dir, weights_path="models/pose/fa\u0000ke.safetensors")
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert status.enabled is False
    assert "R04_WEIGHTS_INSIDE_DATA_DIR" in ids_of(status)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlink_loop_is_refused(tmp_path: Path, data_dir: Path):
    a = data_dir / "models" / "pose" / "loop_a.safetensors"
    b = data_dir / "models" / "pose" / "loop_b.safetensors"
    try:
        os.symlink(b, a)
        os.symlink(a, b)
    except OSError:
        pytest.skip("cannot create symlinks here")
    m = fake_manifest(data_dir)
    m["weights_path"] = "models/pose/loop_a.safetensors"
    status = evaluate(tmp_path, data_dir, m, backends=frozenset({"safetensors"}))
    assert status.enabled is False
    assert ids_of(status) & {"R04_WEIGHTS_INSIDE_DATA_DIR", "R05_WEIGHTS_SHA256"}


def test_manifest_path_that_is_a_directory(tmp_path: Path, data_dir: Path):
    status = evaluate_pose_gate(tmp_path, esp32_runtime(), data_dir=data_dir)
    assert "NOT_A_FILE" in status.missing_requirements[0]


def test_manifest_that_is_not_utf8(tmp_path: Path, data_dir: Path):
    path = tmp_path / "m.json"
    path.write_bytes(b'{"schema": "\xff\xfe"}')
    status = evaluate_pose_gate(path, esp32_runtime(), data_dir=data_dir)
    assert "NOT_UTF8" in status.missing_requirements[0]


@pytest.mark.parametrize("runtime", [None, [], "esp32", 42, {"csi_source": 7, "links": -1}])
def test_junk_runtime_description_never_raises(tmp_path: Path, data_dir: Path, runtime: Any):
    m = fake_manifest(data_dir)
    path = write_json(tmp_path / "manifest.json", m)
    status = evaluate_pose_gate(path, runtime, data_dir=data_dir, available_backends=frozenset({"safetensors"}))
    assert status.enabled is False
    assert "R07_HW_CSI_SOURCE" in ids_of(status)
