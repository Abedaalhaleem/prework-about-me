"""Zone dataset: feature layout, offline fixed-grid windowing, exclusions.

SOFTWARE TESTS ONLY on SIMULATED sessions (zone_session_scenario) and
hand-built feature vectors. Nothing here measures zone accuracy.
"""

from __future__ import annotations

import numpy as np
import pytest

from roomsense.acquisition.base import FrameEvent
from roomsense.acquisition.synthetic import generate_frames, zone_session_scenario
from roomsense.config import AppConfig
from roomsense.inference.zone.dataset import (
    EXCL_LINK_MISSING,
    DatasetError,
    SessionSpec,
    build_dataset,
    feature_names_for,
    feature_row,
    link_feature_names,
    link_feature_values,
)
from roomsense.processing.amplitude import convert_frame
from roomsense.processing.pipeline import ProcessingEngine
from roomsense.schemas import SourceMode
from roomsense.storage.db import Database
from roomsense.storage.recordings import Recorder

from .storage_helpers import make_consent, storage_cfg
from .zone_helpers import (
    LABELS,
    LINK_ORDER,
    RATE_HZ,
    cached_dataset,
    features_for,
    make_fv,
    synthetic_spec,
)

TRIM_S = 5.0
CFG = AppConfig()


def _build(specs, **kw):
    kw.setdefault("trim_session_edges_s", TRIM_S)
    return build_dataset(specs, CFG, **kw)


# --------------------------------------------------------------- feature layout


def test_feature_layout_is_fixed_and_link_ordered():
    block = link_feature_names()
    assert block[:2] == ("log10_amp_cv_median", "log10_amp_tdiff_median")
    assert len(block) == 10  # 2 window scalars + 4 quantiles x 2 per-subcarrier features
    names = feature_names_for(LINK_ORDER)
    assert len(names) == 30
    assert names[0] == f"{LINK_ORDER[0]}|log10_amp_cv_median"
    assert names[10] == f"{LINK_ORDER[1]}|log10_amp_cv_median"


def test_feature_block_does_not_depend_on_the_number_of_subcarriers():
    a, _ = link_feature_values(make_fv("l", 0.1, n_subcarriers=52))
    b, _ = link_feature_values(make_fv("l", 0.1, n_subcarriers=114))
    assert a is not None and b is not None
    np.testing.assert_allclose(a, b)
    np.testing.assert_allclose(a[0], np.log10(0.1))


def test_feature_row_needs_every_required_link():
    row, why = feature_row(features_for("A", links=LINK_ORDER[:2]), LINK_ORDER)
    assert row is None
    assert any(r.startswith(EXCL_LINK_MISSING) and LINK_ORDER[2] in r for r in why)
    row, why = feature_row(features_for("A"), LINK_ORDER)
    assert row is not None and row.shape == (30,) and why == []


def test_feature_row_flags_changed_feature_layout_and_unusable_windows():
    fvs = features_for("A")
    fvs[LINK_ORDER[0]] = make_fv(LINK_ORDER[0], 0.1, names=("something_else", "x"))
    row, why = feature_row(fvs, LINK_ORDER)
    assert row is None and why[0].startswith("FEATURE_LAYOUT_CHANGED")
    fvs = features_for("A")
    fvs[LINK_ORDER[1]] = make_fv(LINK_ORDER[1], 0.1, n_subcarriers=3)  # too few usable subcarriers
    row, why = feature_row(fvs, LINK_ORDER)
    assert row is None and why[0].startswith("FEATURES_UNUSABLE")


# --------------------------------------------------------------- windowing


def test_simulated_dataset_shape_labels_and_links():
    ds = cached_dataset()
    assert ds.X.shape[1] == 30 and ds.X.shape[0] == ds.y.size == ds.session_ids.size == ds.window_end_ns.size
    assert ds.link_order == LINK_ORDER
    assert ds.feature_names == feature_names_for(LINK_ORDER)
    assert set(ds.y.tolist()) == set(LABELS)
    assert np.all(np.isfinite(ds.X))
    assert ds.receivers == ["rx1", "rx2", "rx3"]
    for s in ds.sessions:
        assert s.windows_kept >= 30, s
        assert s.synthetic and any("SYNTHETIC" in r for r in s.synthetic_reasons)
        assert s.source_modes == [SourceMode.SIMULATION.value]
    assert len(ds.hardware_signatures) == 1


def test_windows_follow_a_fixed_hop_grid_inside_the_trimmed_session():
    spec = synthetic_spec("B", 0)
    ds = _build([spec])
    ends = ds.window_end_ns
    hop_ns = int(CFG.processing.hop_s * 1e9)
    assert np.all(np.diff(ends) % hop_ns == 0)
    t = [convert_frame(f).t_ns for f in spec.frames()]  # type: ignore[misc,union-attr]
    t_first, t_last = min(t), max(t)
    window_ns = int(CFG.processing.window_s * 1e9)
    trim_ns = int(TRIM_S * 1e9)
    assert ends[0] == t_first + trim_ns + window_ns
    assert ends[-1] <= t_last - trim_ns
    assert ds.sessions[0].windows_kept == ends.size


def test_dataset_is_deterministic():
    a = _build([synthetic_spec("C", 1)])
    b = _build([synthetic_spec("C", 1)])
    np.testing.assert_array_equal(a.X, b.X)
    np.testing.assert_array_equal(a.window_end_ns, b.window_end_ns)


def test_hardware_signature_matches_the_runtime_engine():
    spec = synthetic_spec("A", 2)
    ds = _build([spec])
    engine = ProcessingEngine(CFG)
    for f in spec.frames():  # type: ignore[misc,union-attr]
        engine.on_event(FrameEvent(f))
    assert ds.sessions[0].hardware_signature == engine.hardware_signature() is not None


def test_simulated_frames_make_a_session_synthetic_even_if_declared_real():
    ds = _build([synthetic_spec("EMPTY", 0, declared_synthetic=False)])
    s = ds.sessions[0]
    assert s.synthetic
    assert s.synthetic_reasons == ["session contains simulated (SYNTHETIC) frames"]


def test_receiver_outage_excludes_windows_instead_of_filling_them():
    full = _build([synthetic_spec("A", 3)])
    cut = _build([synthetic_spec("A", 3, drop=(LINK_ORDER[1], 10.0, 16.0))])
    s = cut.sessions[0]
    assert s.windows_kept < full.sessions[0].windows_kept
    assert s.excluded, "excluded windows must be counted with their reason"
    assert set(s.excluded) <= {"WINDOW_REJECTED", "LOW_QUALITY", "NOT_ALIGNED", EXCL_LINK_MISSING}
    # No kept window may overlap the outage on the missing link.
    start = full.window_end_ns[0] - int((TRIM_S + CFG.processing.window_s) * 1e9)
    rel = (cut.window_end_ns - start) / 1e9
    window_s = CFG.processing.window_s
    assert not np.any((rel > 10.0 + 0.3) & (rel - window_s < 16.0 - 0.3))


def test_given_link_order_marks_sessions_without_a_link():
    spec = synthetic_spec("B", 4, drop=(LINK_ORDER[2], 0.0, 1e9))
    ds = _build([spec], link_order=LINK_ORDER)
    assert ds.sessions[0].windows_kept == 0
    assert ds.sessions[0].excluded.get(EXCL_LINK_MISSING, 0) > 0
    with pytest.raises(DatasetError, match="different link sets"):
        _build([spec, synthetic_spec("C", 4)])


def test_session_validation():
    with pytest.raises(DatasetError, match="duplicate session"):
        _build([synthetic_spec("A", 0), synthetic_spec("A", 0)])
    with pytest.raises(DatasetError, match="unknown label"):
        _build([synthetic_spec("A", 0)], allowed_labels=["B", "EMPTY", "OUTSIDE_TARGET_ROOM"])
    with pytest.raises(DatasetError, match="exactly one"):
        SessionSpec(label="A", recording_id="rec_1", frames=[], session_key="k")
    with pytest.raises(DatasetError, match="split"):
        SessionSpec(label="A", frames=[], session_key="k", split="holdout")
    with pytest.raises(DatasetError, match="session_key"):
        SessionSpec(label="A", frames=[], session_key="../escape")
    with pytest.raises(DatasetError, match="no sessions"):
        _build([])


def test_recorded_session_is_read_from_the_data_dir(tmp_path):
    db = Database(tmp_path / "meta.sqlite3")
    data_dir = tmp_path / "data"
    try:
        rec = Recorder(db, data_dir, storage_cfg())
        scn = zone_session_scenario("C", seed=5, duration_s=20.0, rate_hz=RATE_HZ)
        info = rec.start(consent=make_consent(), label="C", session_id="sess_sim", source_mode=SourceMode.SIMULATION)
        for f in generate_frames(scn, "sess_sim"):
            assert rec.write(f)
        info = rec.stop()
        assert info is not None and info.synthetic
        spec = SessionSpec(label="C", recording_id=info.recording_id, expected_rate_hz=RATE_HZ)
        ds = build_dataset([spec], CFG, trim_session_edges_s=TRIM_S, data_dir=data_dir, db=db)
        s = ds.sessions[0]
        assert s.session_key == info.recording_id and s.windows_kept > 0
        assert s.synthetic
        assert any("header" in r for r in s.synthetic_reasons)
        assert any("metadata" in r for r in s.synthetic_reasons)
        with pytest.raises(DatasetError, match="data_dir"):
            build_dataset([spec], CFG, trim_session_edges_s=TRIM_S)
        with pytest.raises(DatasetError, match="not found"):
            build_dataset([SessionSpec(label="C", recording_id="rec_missing")], CFG, trim_session_edges_s=TRIM_S,
                          data_dir=data_dir)
    finally:
        db.close()
