"""Zone model registry: storage layout, integrity checks, derived enabled flag.

Models here are HAND-BUILT fakes (logistic regression on random numbers) with
hand-written reports; they test file handling and gating only.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from roomsense.inference.zone.dataset import feature_row
from roomsense.inference.zone.registry import MODEL_FORMAT, RegistryError, ZoneModelRegistry
from roomsense.storage.db import Database

from .zone_helpers import LINK_ORDER, features_for, passing_report, save_fake_model


@pytest.fixture
def reg(tmp_path):
    return ZoneModelRegistry(tmp_path / "data")


def _json_path(reg: ZoneModelRegistry, mid: str):
    return reg.models_dir / f"{mid}.json"


def test_round_trip_keeps_binding_and_predictions(reg):
    b = save_fake_model(reg)
    assert (reg.models_dir / f"{b.model_id}.joblib").is_file() and _json_path(reg, b.model_id).is_file()
    assert reg.models_dir == (reg.data_dir / "models").resolve()
    loaded = reg.load(b.model_id)
    assert loaded.binding == b
    assert b.enabled is True and b.enabled_reasons == ()
    row, _ = feature_row(features_for("B"), LINK_ORDER)
    assert row is not None
    assert loaded.pipeline.predict(row[None])[0] == "B"
    doc = json.loads(_json_path(reg, b.model_id).read_text("utf-8"))
    assert doc["format"] == MODEL_FORMAT
    for key in ("hardware_signature", "room_config_hash", "config_version", "criteria_version", "link_order",
                "feature_names", "threshold", "classes", "synthetic_data_used", "joblib_sha256"):
        assert key in doc["binding"]
    assert doc["report"]["model_id"] == b.model_id


def test_enabled_comes_from_the_report_not_the_caller(reg):
    failing = passing_report()
    failing["criteria"][0]["passed"] = False
    b = save_fake_model(reg, report=failing)
    assert b.enabled is False and any(r.startswith("CRITERION_FAILED") for r in b.enabled_reasons)
    # Hand-editing the JSON flag does not enable it either.
    p = _json_path(reg, b.model_id)
    doc = json.loads(p.read_text("utf-8"))
    doc["binding"]["enabled"] = True
    p.write_text(json.dumps(doc), "utf-8")
    assert reg.load_binding(b.model_id).enabled is False


def test_synthetic_training_data_is_never_enabled(tmp_path):
    db = Database(tmp_path / "meta.sqlite3")
    try:
        reg = ZoneModelRegistry(tmp_path / "data", db=db)
        b = save_fake_model(reg, synthetic_data_used=True)  # even with an otherwise passing report
        assert b.enabled is False
        assert any(r.startswith("SYNTHETIC_DATA_NOT_ALLOWED") for r in b.enabled_reasons)
        row = db.get_zone_model(b.model_id)
        assert row is not None and not row.enabled and row.synthetic_data_used
        assert row.artifact_relpath == f"models/{b.model_id}.joblib"
        ok = save_fake_model(reg)
        assert db.get_zone_model(ok.model_id).enabled is True  # type: ignore[union-attr]
    finally:
        db.close()


def test_tampered_model_file_is_not_loaded(reg):
    b = save_fake_model(reg)
    pm = reg.models_dir / f"{b.model_id}.joblib"
    blob = bytearray(pm.read_bytes())
    blob[len(blob) // 2] ^= 0xFF  # one flipped byte
    pm.write_bytes(bytes(blob))
    with pytest.raises(RegistryError) as exc:
        reg.load(b.model_id)
    assert exc.value.code == "MODEL_FILE_MISMATCH"
    pm.write_bytes(bytes(blob) + b"x")  # appended data
    with pytest.raises(RegistryError, match="MODEL_FILE_MISMATCH"):
        reg.load(b.model_id)


def test_only_safe_ids_inside_the_models_dir(reg, tmp_path):
    for bad in ("../x", "a/b", "", "x" * 65, "a.b", "abc\n"):
        with pytest.raises(RegistryError) as exc:
            reg.load(bad)
        assert exc.value.code == "INVALID_MODEL_ID"
    with pytest.raises(RegistryError) as exc:
        reg.load("zm_missing")
    assert exc.value.code == "MODEL_NOT_FOUND"


def test_symlinked_files_are_refused(reg, tmp_path):
    b = save_fake_model(reg)
    pm = reg.models_dir / f"{b.model_id}.joblib"
    outside = tmp_path / "outside.joblib"
    outside.write_bytes(pm.read_bytes())
    pm.unlink()
    os.symlink(outside, pm)
    with pytest.raises(RegistryError) as exc:
        reg.load(b.model_id)
    assert exc.value.code == "MODEL_FILE_INVALID"


def test_list_bindings_newest_first_and_reports_bad_files(reg):
    a = save_fake_model(reg, created_at_unix_ns=1)
    b = save_fake_model(reg, created_at_unix_ns=2)
    (reg.models_dir / "zm_broken.json").write_text("{not json", "utf-8")
    renamed = reg.models_dir / "zm_renamed.json"
    renamed.write_text(_json_path(reg, a.model_id).read_text("utf-8"), "utf-8")
    bindings, errors = reg.list_bindings()
    assert [x.model_id for x in bindings] == [b.model_id, a.model_id]
    assert any("zm_broken" in e for e in errors)
    assert any("zm_renamed" in e and "does not match" in e for e in errors)


def test_save_never_overwrites_and_delete_removes_everything(tmp_path):
    db = Database(tmp_path / "meta.sqlite3")
    try:
        reg = ZoneModelRegistry(tmp_path / "data", db=db)
        b = save_fake_model(reg)
        with pytest.raises(RegistryError) as exc:
            reg.save(np.zeros(1), hardware_signature="x", room_config_hash="r", config_version="c",
                     criteria_version="v", link_order=[], feature_names=[], feature_set_version="f", threshold=0.5,
                     classes=[], zone_ids=[], synthetic_data_used=False, report={}, model_id=b.model_id)
        assert exc.value.code == "MODEL_EXISTS"
        assert reg.delete(b.model_id) is True
        assert not any(reg.models_dir.iterdir())
        assert db.get_zone_model(b.model_id) is None
        assert reg.delete(b.model_id) is False
    finally:
        db.close()
