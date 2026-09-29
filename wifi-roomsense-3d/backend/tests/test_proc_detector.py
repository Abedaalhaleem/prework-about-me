"""Motion detector state machine. Inputs are hand-built feature vectors with
exact scores. These tests check the documented decision rules only; they say
nothing about detection accuracy."""

from __future__ import annotations

import pytest

from .proc_helpers import CHANGED_PROFILE, LINK, fast_cfg, make_baseline, make_fv, make_prov, make_quality
from roomsense.processing.detector import SCORE_NOTE, MotionDetector
from roomsense.schemas import ActivityState, SourceMode

CFG = fast_cfg()  # enter 4, exit 2.5, motion hold 1 s, quiet hold 2 s, drift hold 4 s, clear 5 s
VERSION = CFG.config_version()
S = ActivityState


def _det(**over) -> MotionDetector:
    det_cfg = fast_cfg(**over).detection if over else CFG.detection
    version = fast_cfg(**over).config_version() if over else VERSION
    d = MotionDetector(LINK, det_cfg, version, stale_after_s=2.0)
    d.set_hardware_signature("sig-a")
    d.set_baseline(make_baseline(version))
    return d


def _run(d: MotionDetector, scores: list[float], *, t0: float = 10.0, dt: float = 0.5, quality: str = "GOOD"):
    out = []
    for i, s in enumerate(scores):
        t = t0 + i * dt
        out.append(d.update(make_fv(s, t_s=t), make_quality(quality), make_prov(), now_ns=int(t * 1e9)))
    return out


def test_scores_and_quiet_state() -> None:
    d = _det()
    r = _run(d, [0.0, 1.0, 3.9])
    assert [x.state for x in r] == [S.NO_MOTION_DETECTED] * 3
    assert r[1].activity_score == pytest.approx(1.0)
    assert r[0].calibrated_probability is None
    assert SCORE_NOTE in r[0].reasons
    u = r[1].uncertainty
    assert u is not None and u.method == "subcarrier_iqr" and u.low <= u.high and u.n_subcarriers == 52


def test_hysteresis_no_flapping_between_thresholds() -> None:
    d = _det()
    # Enter at 5; scores between exit (2.5) and enter (4) keep MOTION forever.
    r = _run(d, [5.0] + [3.0] * 20)
    assert all(x.state == S.MOTION_DETECTED for x in r)
    # From NO_MOTION, scores between the thresholds never enter.
    d2 = _det()
    r2 = _run(d2, [3.9, 3.0, 3.99, 2.6] * 5)
    assert all(x.state == S.NO_MOTION_DETECTED for x in r2)


def test_exit_needs_continuous_quiet_hold() -> None:
    d = _det()
    # Quiet hold is 2 s at 0.5 s hop: below-exit windows at t, t+0.5, ..., t+2.0.
    r = _run(d, [6.0, 6.0, 6.0, 1.0, 1.0, 1.0, 3.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    states = [x.state for x in r]
    # The 3.0 at index 6 (>= exit) restarts the quiet timer.
    assert states[:12] == [S.MOTION_DETECTED] * 11 + [S.NO_MOTION_DETECTED]
    assert states[12] == S.NO_MOTION_DETECTED
    assert any(x.startswith("EXIT") for x in r[11].reasons)
    transitions = sum(1 for a, b in zip(states, states[1:]) if a != b)
    assert transitions == 1


def test_min_motion_hold_is_respected() -> None:
    d = _det(min_motion_hold_s=5.0, min_quiet_hold_s=0.0)
    r = _run(d, [6.0] + [0.0] * 12)
    states = [x.state for x in r]
    first_exit = states.index(S.NO_MOTION_DETECTED)
    assert first_exit * 0.5 >= 5.0


def test_no_baseline_is_unknown() -> None:
    d = MotionDetector(LINK, CFG.detection, VERSION)
    r = d.update(make_fv(10.0, t_s=5.0), make_quality(), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.UNKNOWN and r.activity_score is None
    assert r.reasons[0].startswith("NO_BASELINE")


@pytest.mark.parametrize("level", ["BAD", "UNAVAILABLE"])
def test_low_quality_is_unknown_never_no_motion(level: str) -> None:
    d = _det()
    r = d.update(make_fv(0.0, t_s=5.0), make_quality(level), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.UNKNOWN and r.activity_score is None
    assert r.reasons[0].startswith("LOW_QUALITY")


def test_min_quality_good_rejects_degraded() -> None:
    d = _det(min_quality_for_decision="GOOD")
    r = d.update(make_fv(0.0, t_s=5.0), make_quality("DEGRADED"), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.UNKNOWN
    d2 = _det()
    r2 = d2.update(make_fv(0.0, t_s=5.0), make_quality("DEGRADED"), make_prov(), now_ns=5 * 10**9)
    assert r2.state == S.NO_MOTION_DETECTED


def test_rejected_window_without_features_is_unknown() -> None:
    d = _det()
    r = d.update(None, make_quality("BAD", frames=8), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.UNKNOWN


def test_stale_offline_then_motion_cleared() -> None:
    d = _det()
    r = _run(d, [6.0, 6.0])
    assert r[-1].state == S.MOTION_DETECTED
    t = 10.5
    assert d.tick(int((t + 1.0) * 1e9), 1.0, make_prov(age=1.0)) is None  # fresh enough
    r = d.tick(int((t + 3.0) * 1e9), 3.0, make_prov(age=3.0))
    assert r is not None and r.state == S.SENSOR_OFFLINE and r.activity_score is None
    assert d.motion_latched  # not yet cleared (< clear_stale_after_s)
    r = d.tick(int((t + 6.0) * 1e9), 6.0, make_prov(age=6.0))
    assert r is not None and r.state == S.SENSOR_OFFLINE
    assert any(x.startswith("MOTION_CLEARED") for x in r.reasons)
    assert not d.motion_latched
    # Data comes back quiet: no stale MOTION carried over.
    r = _run(d, [0.0], t0=t + 7.0)
    assert r[0].state == S.NO_MOTION_DETECTED


def test_stale_provenance_in_update_is_offline() -> None:
    d = _det()
    r = d.update(make_fv(0.0, t_s=5.0), make_quality(), make_prov(age=2.5), now_ns=5 * 10**9)
    assert r.state == S.SENSOR_OFFLINE
    r = d.update(None, make_quality("UNAVAILABLE", frames=0), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.SENSOR_OFFLINE
    assert d.tick(0, None, make_prov(age=None)).state == S.SENSOR_OFFLINE  # type: ignore[union-attr]
    r = d.tick(0, 0.1, make_prov(), offline_reason="DISCONNECTED: port closed")
    assert r is not None and r.state == S.SENSOR_OFFLINE and r.reasons[0].startswith("DISCONNECTED")


def test_long_undecidable_period_expires_motion_latch() -> None:
    d = _det()
    _run(d, [6.0])
    r = d.update(make_fv(0.0, t_s=16.0), make_quality("BAD"), make_prov(), now_ns=16 * 10**9)
    assert r.state == S.UNKNOWN and any(x.startswith("MOTION_CLEARED") for x in r.reasons)


@pytest.mark.parametrize(
    "setup,expect",
    [
        (lambda d: d.set_baseline(make_baseline("cfg-other")), "CONFIG_CHANGED"),
        (lambda d: d.set_hardware_signature("sig-b"), "HARDWARE_SIGNATURE_CHANGED"),
        (lambda d: d.set_hardware_signature("sig-b", complete=False), "HARDWARE_SIGNATURE_UNVERIFIED"),
        (lambda d: d.set_baseline(make_baseline(VERSION, layout_id="classic.other")), "LAYOUT_CHANGED"),
    ],
)
def test_calibration_mismatch_is_unknown(setup, expect: str) -> None:
    d = _det()
    setup(d)
    r = d.update(make_fv(9.0, t_s=5.0), make_quality(), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.UNKNOWN and r.activity_score is None
    assert r.reasons[0].startswith("CALIBRATION_INVALID")
    assert any(x.startswith(expect) for x in r.reasons)


def test_simulated_baseline_never_used_on_live_data() -> None:
    d = _det()
    r = d.update(make_fv(9.0, t_s=5.0), make_quality(), make_prov(mode=SourceMode.LIVE), now_ns=5 * 10**9)
    assert r.state == S.UNKNOWN and any(x.startswith("SOURCE_MODE_MISMATCH") for x in r.reasons)


def test_invalidated_baseline_reports_reason() -> None:
    d = _det()
    d.invalidate_baseline("LAYOUT_CHANGED: test")
    assert d.baseline is None
    r = d.update(make_fv(9.0, t_s=5.0), make_quality(), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.UNKNOWN
    assert r.reasons[0].startswith("CALIBRATION_INVALID") and "LAYOUT_CHANGED: test" in r.reasons


def test_invalidating_without_baseline_keeps_no_baseline() -> None:
    d = MotionDetector(LINK, CFG.detection, VERSION)
    d.invalidate_baseline("ROOM_CHANGED: test")
    r = d.update(make_fv(0.0, t_s=5.0), make_quality(), make_prov(), now_ns=5 * 10**9)
    assert r.reasons[0].startswith("NO_BASELINE") and d.invalid_reason is None


def test_drift_after_hold_is_unknown_and_recovers() -> None:
    d = _det()
    states = []
    for i in range(14):  # 7 s of a changed static profile, quiet otherwise
        t = 10.0 + 0.5 * i
        r = d.update(make_fv(0.0, t_s=t, profile=CHANGED_PROFILE), make_quality(), make_prov(), now_ns=int(t * 1e9))
        states.append(r.state)
    # Before drift_hold_s (4 s) the detector still decides; afterwards UNKNOWN.
    assert states[:8] == [S.NO_MOTION_DETECTED] * 8
    assert all(s == S.UNKNOWN for s in states[8:])
    assert r.reasons[0].startswith("STATIC_CHANNEL_CHANGED") and "stationary person" in r.reasons[0]
    assert d.drift_status()["suspected"] is True
    r = d.update(make_fv(0.0, t_s=20.0), make_quality(), make_prov(), now_ns=20 * 10**9)
    assert r.state == S.NO_MOTION_DETECTED and d.drift_status()["suspected"] is False


def test_calibrating_has_priority() -> None:
    d = _det()
    d.set_calibrating(True)
    r = d.update(make_fv(9.0, t_s=5.0), make_quality(), make_prov(), now_ns=5 * 10**9)
    assert r.state == S.CALIBRATING and r.activity_score is None


def test_detector_never_modifies_baseline() -> None:
    d = _det()
    before = d.baseline.to_dict()  # type: ignore[union-attr]
    _run(d, [8.0] * 40 + [0.0] * 40)
    after = d.baseline.to_dict()  # type: ignore[union-attr]
    assert before == after


def test_baseline_for_other_link_refused() -> None:
    d = MotionDetector(LINK, CFG.detection, VERSION)
    with pytest.raises(ValueError):
        d.set_baseline(make_baseline(VERSION, link_id="tx9->rx9"))


def test_calibrated_probability_is_always_none() -> None:
    d = _det()
    results = _run(d, [0.0, 8.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0], quality="GOOD")
    results += [d.tick(10**12, 9.0, make_prov(age=9.0))]  # type: ignore[list-item]
    assert all(r is not None and r.calibrated_probability is None for r in results)
