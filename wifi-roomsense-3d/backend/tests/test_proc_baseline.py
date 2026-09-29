"""Quiet-baseline recording and acceptance rules. Synthetic data only; these
tests check the documented rules, not how well a real baseline works."""

from __future__ import annotations

import json

import numpy as np
import pytest

from .proc_helpers import Scenario, fast_cfg, feature_stream, generate, make_fv, make_quality
from roomsense.processing.baseline import SCALE_REL_FLOOR, Baseline, BaselineRecorder, robust_center_scale

CFG = fast_cfg()
DET = CFG.detection
VERSION = CFG.config_version()


def _record(sc: Scenario, det=DET) -> tuple[Baseline | None, list[str]]:
    rec = BaselineRecorder("tx1->rx1", det)
    for _w, q, fv in feature_stream(generate(sc), CFG.processing):
        if fv is not None:
            rec.add(fv, q)
    return rec.finalize(calibration_id="cal-1", hardware_signature="hw-1", config_version=VERSION,
                        link_signature="sig-1")


def test_quiet_baseline_accepted() -> None:
    b, reasons = _record(Scenario(duration_s=20.0, seed=1))
    assert reasons == []
    assert b is not None
    assert b.n_windows >= DET.baseline_min_windows
    assert b.duration_s >= DET.baseline_min_duration_s
    assert set(b.feature_median) == {"amp_cv_median", "amp_tdiff_median"}
    assert all(v > 0 for v in b.feature_scale.values())
    assert b.profile.shape == b.k.shape
    assert b.calibration_id == "cal-1" and b.hardware_signature == "hw-1" and b.config_version == VERSION
    assert b.link_signature == "sig-1"
    assert b.summary["unstable_window_fraction"] <= 0.10
    # Frozen: nothing downstream can adapt the baseline in place.
    with pytest.raises(ValueError):
        b.profile[0] = 1.0


def test_motion_during_calibration_rejected() -> None:
    b, reasons = _record(Scenario(duration_s=20.0, motion=[(8.0, 13.0)], seed=1))
    assert b is None
    assert any(r.startswith("BASELINE_UNSTABLE") for r in reasons)


def test_mostly_moving_recording_rejected() -> None:
    # Motion for 60 % of the recording: the robust centre sits on motion, and the
    # quiet part then deviates negatively. The two-sided rule still rejects it.
    b, reasons = _record(Scenario(duration_s=20.0, motion=[(8.0, 20.0)], seed=2))
    assert b is None and any(r.startswith("BASELINE_UNSTABLE") for r in reasons)


def test_static_change_during_calibration_rejected() -> None:
    b, reasons = _record(Scenario(duration_s=20.0, profile_change_at=10.0, seed=3))
    assert b is None
    assert any("BASELINE_UNSTABLE" in r and "profile" in r for r in reasons)


def test_too_short_rejected() -> None:
    b, reasons = _record(Scenario(duration_s=6.0, seed=1))
    assert b is None
    assert any(r.startswith("INSUFFICIENT_WINDOWS") for r in reasons)
    assert any(r.startswith("INSUFFICIENT_DURATION") for r in reasons)


def test_empty_recorder_rejected() -> None:
    b, reasons = BaselineRecorder("L", DET).finalize(calibration_id="c", hardware_signature="h",
                                                      config_version=VERSION)
    assert b is None and reasons[0].startswith("NO_USABLE_WINDOWS")


def test_low_quality_windows_do_not_count() -> None:
    rec = BaselineRecorder("tx1->rx1", DET)
    for i in range(40):
        rec.add(make_fv(0.0, t_s=2.0 + 0.5 * i), make_quality("BAD"))
    assert rec.n_windows == 0 and rec.skipped_windows == 40
    b, reasons = rec.finalize(calibration_id="c", hardware_signature="h", config_version=VERSION)
    assert b is None


def test_mostly_low_quality_recording_rejected() -> None:
    rec = BaselineRecorder("tx1->rx1", DET)
    for i in range(40):
        rec.add(make_fv(0.0, t_s=2.0 + 0.5 * i), make_quality("GOOD" if i % 3 == 0 else "BAD"))
    b, reasons = rec.finalize(calibration_id="c", hardware_signature="h", config_version=VERSION)
    assert b is None and any(r.startswith("LOW_QUALITY_DURING_CALIBRATION") for r in reasons)


def test_perfectly_flat_baseline_has_finite_positive_scale() -> None:
    rec = BaselineRecorder("tx1->rx1", DET)
    for i in range(40):
        rec.add(make_fv(0.0, t_s=2.0 + 0.5 * i), make_quality("GOOD"))
    b, reasons = rec.finalize(calibration_id="c", hardware_signature="h", config_version=VERSION)
    assert reasons == [] and b is not None
    for name, med in b.feature_median.items():
        assert b.feature_scale[name] == pytest.approx(SCALE_REL_FLOOR * med)
    for arr in b.per_subcarrier_scale.values():
        assert np.all(arr[np.isfinite(arr)] > 0)


def test_robust_center_scale_floors() -> None:
    med, scale = robust_center_scale(np.array([[2.0], [2.0], [2.0]]))
    assert med[0] == 2.0 and scale[0] == pytest.approx(0.1)
    med, scale = robust_center_scale(np.array([[0.0], [0.0]]))
    assert scale[0] > 0


def test_identity_change_during_recording_rejected() -> None:
    rec = BaselineRecorder("tx1->rx1", DET)
    for i in range(40):
        rec.add(make_fv(0.0, t_s=2.0 + 0.5 * i), make_quality("GOOD"))
    rec.note_identity_change("channel changed")
    b, reasons = rec.finalize(calibration_id="c", hardware_signature="h", config_version=VERSION)
    assert b is None and any(r.startswith("HARDWARE_SIGNATURE_CHANGED") for r in reasons)


def test_round_trip_json() -> None:
    b, _ = _record(Scenario(duration_s=20.0, seed=5))
    assert b is not None
    d = b.to_dict()
    text = json.dumps(d, allow_nan=False)
    b2 = Baseline.from_dict(json.loads(text))
    for field in ("link_id", "calibration_id", "layout_id", "hardware_signature", "config_version",
                  "link_signature", "n_windows", "feature_median", "feature_scale", "source_mode"):
        assert getattr(b2, field) == getattr(b, field)
    assert np.array_equal(b2.k, b.k)
    assert np.allclose(b2.profile, b.profile, equal_nan=True)
    for n in b.per_subcarrier_median:
        assert np.allclose(b2.per_subcarrier_median[n], b.per_subcarrier_median[n], equal_nan=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="something-else"),
        lambda d: d.update(profile=d["profile"][:-1]),
        lambda d: d["feature_scale"].update(amp_cv_median=0.0),
        lambda d: d.pop("layout_id"),
        lambda d: d["feature_median"].update(amp_cv_median=float("inf")),
    ],
)
def test_from_dict_rejects_malformed(mutate) -> None:
    b, _ = _record(Scenario(duration_s=20.0, seed=5))
    assert b is not None
    d = json.loads(json.dumps(b.to_dict()))
    mutate(d)
    with pytest.raises(ValueError):
        Baseline.from_dict(d)
