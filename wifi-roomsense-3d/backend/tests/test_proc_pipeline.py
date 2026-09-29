"""ProcessingEngine end-to-end on synthetic frames (SIMULATION only).

These tests exercise the software paths: buffering, windows, calibration
control, invalidation, staleness, provenance and plot snapshots. The
synthetic "motion" is a made-up amplitude modulation. Passing tests show
nothing about through-wall sensing accuracy.
"""

from __future__ import annotations

import dataclasses
import json
import threading

import numpy as np
import pytest

from .proc_helpers import (
    BASE_UNIX_NS,
    LINK,
    RX,
    SESSION,
    Scenario,
    fast_cfg,
    feed,
    generate,
    t_ns,
)
from roomsense.acquisition.base import CONNECTED, DISCONNECTED, HELLO, EndOfStream, FrameEvent, LinkEvent
from roomsense.csi_layouts import resolve_layout
from roomsense.processing.pipeline import (
    MAX_SNAPSHOT_POINTS,
    MAX_SNAPSHOT_SUBCARRIERS,
    ProcessingEngine,
    _downsample_segments,
)
from roomsense.schemas import ActivityState, SourceMode

S = ActivityState
DECIDED = (S.NO_MOTION_DETECTED, S.MOTION_DETECTED)


def _engine(cfg=None) -> ProcessingEngine:
    eng = ProcessingEngine(cfg or fast_cfg(), clock_ns=lambda: 0, clock_unix_ns=lambda: BASE_UNIX_NS)
    eng.reset(SESSION, SourceMode.SIMULATION)
    return eng


def _sc(start: float, dur: float, **kw) -> Scenario:
    return Scenario(duration_s=dur, start_s=start, first_counter=int(start * 25), **kw)


def _calibrate(eng: ProcessingEngine, dur: float = 20.0, seed: int = 1, t0: float = 0.0):
    frames = generate(_sc(t0, dur, seed=seed))
    feed(eng, frames[:5])
    eng.start_baseline()
    feed(eng, frames[5:])
    b, reasons = eng.stop_baseline()[LINK]
    assert b is not None, reasons
    eng.set_baselines({LINK: b}, b.calibration_id)
    return b


def _t(res) -> float:
    return (res.provenance.window_end_unix_ns - BASE_UNIX_NS) / 1e9


def test_end_to_end_quiet_motion_quiet() -> None:
    eng = _engine()
    b = _calibrate(eng)
    res = feed(eng, generate(_sc(20.0, 30.0, motion=[(28.0, 36.0)], seed=2)))
    decided = [r for r in res if r.state in DECIDED]
    assert len(decided) >= 40
    before = [r for r in decided if _t(r) < 28.0]
    assert before and all(r.state == S.NO_MOTION_DETECTED for r in before)
    during = [r for r in decided if 29.0 <= _t(r) <= 36.0]
    assert during and all(r.state == S.MOTION_DETECTED for r in during)
    first_quiet_after = next(r for r in decided if _t(r) > 36.0 and r.state == S.NO_MOTION_DETECTED)
    # Window (2 s) must clear the motion, then the 2 s quiet hold must pass.
    assert _t(first_quiet_after) >= 36.0 + fast_cfg().detection.min_quiet_hold_s
    assert res[-1].state == S.NO_MOTION_DETECTED
    assert all(r.calibrated_probability is None for r in res)
    for r in decided:
        assert r.provenance.calibration_id == b.calibration_id
        assert "SYNTHETIC" in r.quality.flags


def test_every_result_has_provenance() -> None:
    cfg = fast_cfg()
    eng = _engine(cfg)
    res = feed(eng, generate(_sc(0.0, 6.0)))
    b = _calibrate(eng, t0=6.0)
    res += feed(eng, generate(_sc(26.0, 4.0, seed=3)))
    res += eng.step(t_ns(36.0))  # stale -> SENSOR_OFFLINE result
    assert res and res[-1].state == S.SENSOR_OFFLINE
    for r in res:
        p = r.provenance
        assert p.source_mode == SourceMode.SIMULATION
        assert p.session_id == SESSION and p.link_ids == [LINK]
        assert p.config_version == cfg.config_version()
        assert p.computed_at_unix_ns == BASE_UNIX_NS
        assert p.measurement_age_s is not None and p.measurement_age_s >= 0
        assert r.calibrated_probability is None
        if r.state in DECIDED:
            assert p.window_frame_count > 0
            assert p.window_start_unix_ns is not None and p.window_end_unix_ns is not None
            assert p.calibration_id == b.calibration_id
    assert all(r.provenance.calibration_id is None for r in res[:5])


def test_without_baseline_everything_is_unknown() -> None:
    eng = _engine()
    res = feed(eng, generate(_sc(0.0, 10.0)))
    assert res and all(r.state == S.UNKNOWN for r in res)
    assert all(r.activity_score is None for r in res)
    assert any(x.startswith("NO_BASELINE") for x in res[-1].reasons)


def test_stale_goes_offline_and_clears_motion() -> None:
    eng = _engine()
    _calibrate(eng)
    res = feed(eng, generate(_sc(20.0, 6.0, motion=[(20.0, 26.0)], seed=4)))
    assert res[-1].state == S.MOTION_DETECTED
    last = 26.0
    assert eng.step(t_ns(last + 1.0)) == []  # not stale yet, no new window
    r = eng.step(t_ns(last + 3.0))
    assert r and r[0].state == S.SENSOR_OFFLINE
    assert eng.latest()[LINK].state == S.SENSOR_OFFLINE
    r = eng.step(t_ns(last + 6.0))
    assert r and r[0].state == S.SENSOR_OFFLINE
    assert any(x.startswith("MOTION_CLEARED") for x in r[0].reasons)
    res = feed(eng, generate(_sc(last + 8.0, 8.0, seed=5)))
    assert all(x.state != S.MOTION_DETECTED for x in res)
    assert [x.state for x in res if x.state in DECIDED][0] == S.NO_MOTION_DETECTED


def test_heavy_loss_is_unknown_never_no_motion() -> None:
    eng = _engine()
    _calibrate(eng)
    res = feed(eng, generate(_sc(20.0, 10.0, loss_fraction=0.8, seed=6)))
    # The first windows still hold full-rate calibration data; skip 3 s of hops.
    late = res[6:]
    assert late and all(r.state == S.UNKNOWN for r in late)
    assert all(r.state != S.NO_MOTION_DETECTED for r in late)
    assert any(x.startswith("LOW_QUALITY") for x in late[-1].reasons)


def _relayout(frames):
    other = resolve_layout(family="classic", total_values=256, ltf_config="lltf_htltf_stbc",
                           secondary_channel=0, sig_mode=1, cwb=0, stbc=0)
    assert other is not None
    return [dataclasses.replace(f, raw_csi=f.raw_csi * 2, csi_len=256, layout_id=other.layout_id) for f in frames]


def test_layout_change_invalidates_calibration() -> None:
    eng = _engine()
    _calibrate(eng)
    res = feed(eng, _relayout(generate(_sc(20.0, 8.0, seed=7))))
    st = eng.baseline_status()[LINK]
    assert st["has_baseline"] is False and st["invalid_reason"].startswith("LAYOUT_CHANGED")
    assert res and all(r.state == S.UNKNOWN for r in res)
    assert res[-1].reasons[0].startswith("CALIBRATION_INVALID")
    assert any(x.startswith("LAYOUT_CHANGED") for x in res[-1].reasons)
    assert res[-1].provenance.calibration_id is None


def test_channel_change_invalidates_calibration_and_signature() -> None:
    eng = _engine()
    _calibrate(eng)
    sig_before = eng.hardware_signature()
    assert sig_before is not None
    res = feed(eng, [dataclasses.replace(f, channel=11) for f in generate(_sc(20.0, 6.0, seed=8))])
    assert eng.hardware_signature() != sig_before
    st = eng.baseline_status()[LINK]
    assert st["has_baseline"] is False and st["invalid_reason"].startswith("HARDWARE_SIGNATURE_CHANGED")
    assert all(r.state == S.UNKNOWN for r in res)


def test_unreported_identity_field_is_not_a_change() -> None:
    eng = _engine()
    _calibrate(eng)
    sig = eng.hardware_signature()
    res = feed(eng, [dataclasses.replace(f, channel=None) for f in generate(_sc(20.0, 6.0, seed=9))])
    assert eng.hardware_signature() == sig
    assert eng.baseline_status()[LINK]["has_baseline"] is True
    assert res[-1].state == S.NO_MOTION_DETECTED


def test_motion_during_calibration_rejected_and_nothing_auto_applied() -> None:
    eng = _engine()
    frames = generate(_sc(0.0, 20.0, motion=[(8.0, 13.0)], seed=10))
    feed(eng, frames[:5])
    eng.start_baseline([LINK])
    res = feed(eng, frames[5:])
    assert all(r.state == S.CALIBRATING for r in res)
    b, reasons = eng.stop_baseline()[LINK]
    assert b is None and any(r.startswith("BASELINE_UNSTABLE") for r in reasons)
    res = feed(eng, generate(_sc(20.0, 4.0, seed=11)))
    assert all(r.state == S.UNKNOWN for r in res)


def test_stop_baseline_does_not_apply_and_cancel_works() -> None:
    eng = _engine()
    frames = generate(_sc(0.0, 20.0, seed=12))
    feed(eng, frames[:5])
    eng.start_baseline()
    assert eng.is_calibrating()
    feed(eng, frames[5:])
    prog = eng.calibration_progress()[LINK]
    assert prog["windows_used"] >= 10 and prog["frames_accepted"] == len(frames) - 5
    b, reasons = eng.stop_baseline()[LINK]
    assert b is not None and reasons == []
    assert b.summary["frames_accepted"] == len(frames) - 5
    assert eng.baseline_status()[LINK]["has_baseline"] is False
    eng.start_baseline()
    eng.cancel_baseline()
    assert not eng.is_calibrating() and eng.stop_baseline() == {}


def test_start_baseline_without_links_raises() -> None:
    with pytest.raises(ValueError):
        _engine().start_baseline()


def test_walk_test_report() -> None:
    eng = _engine()
    _calibrate(eng)
    eng.start_walk_test()
    feed(eng, generate(_sc(20.0, 10.0, motion=[(21.0, 29.0)], seed=13)))
    rep = eng.stop_walk_test()[LINK]
    assert rep["detected"] is True and rep["windows"] > 0
    assert rep["max_score"] > fast_cfg().detection.enter_threshold
    assert 0 < rep["motion_window_fraction"] <= 1
    eng.start_walk_test()
    feed(eng, generate(_sc(40.0, 8.0, seed=14)))
    rep = eng.stop_walk_test()[LINK]
    assert rep["detected"] is False and rep["motion_window_fraction"] == 0.0
    assert eng.stop_walk_test() == {}


def test_walk_test_without_baseline_reports_no_decisions() -> None:
    eng = _engine()
    feed(eng, generate(_sc(0.0, 2.0)))
    eng.start_walk_test()
    feed(eng, generate(_sc(2.0, 6.0, motion=[(2.0, 8.0)])))
    rep = eng.stop_walk_test()[LINK]
    assert rep["detected"] is False and rep["windows"] == 0 and rep["max_score"] is None
    assert rep["reasons"] and rep["reasons"][0].startswith("NO_DECISION_WINDOWS")


def test_signal_snapshot_marks_gaps_and_is_bounded() -> None:
    eng = _engine()
    _calibrate(eng)
    feed(eng, generate(_sc(20.0, 60.0, gaps=[(40.0, 43.0)], motion=[(50.0, 55.0)], seed=15)))
    snap = eng.signal_snapshot(LINK, 60)
    json.dumps(snap, allow_nan=False)
    assert snap["source_mode"] == "SIMULATION" and snap["link_id"] == LINK
    assert snap["enter_threshold"] == 4.0 and snap["exit_threshold"] == 2.5

    base_ms = BASE_UNIX_NS / 1e6
    gaps = snap["gaps"]
    assert any(abs(g["start"] - (base_ms + 40_000)) < 100 and abs(g["end"] - (base_ms + 43_000)) < 100 for g in gaps)

    score = snap["score"]
    assert len(score["t"]) == len(score["v"]) == len(score["state"])
    assert score["t"] == sorted(score["t"])
    i_null = [i for i, v in enumerate(score["v"]) if v is None]
    assert any(base_ms + 40_000 < score["t"][i] < base_ms + 43_000 for i in i_null)
    assert len(snap["rate_hz"]["v"]) == len(score["t"]) and len(snap["rssi_dbm"]["v"]) == len(score["t"])

    amp = snap["amplitude"]
    assert 0 < len(amp["t"]) <= MAX_SNAPSHOT_POINTS
    assert 0 < len(amp["k"]) <= MAX_SNAPSHOT_SUBCARRIERS and amp["k"] == sorted(amp["k"])
    assert len(amp["v"]) == len(amp["t"]) and all(len(row) == len(amp["k"]) for row in amp["v"])
    null_rows = [i for i, row in enumerate(amp["v"]) if all(v is None for v in row)]
    assert null_rows and any(base_ms + 40_000 < amp["t"][i] < base_ms + 43_000 for i in null_rows)
    assert amp["t"] == sorted(amp["t"])

    prof = snap["latest_profile"]
    assert prof["t"] is not None and prof["k"] == sorted(prof["k"]) and len(prof["amp"]) == 64
    assert prof["amp"][prof["k"].index(0)] is None  # DC


def test_signal_snapshot_ongoing_gap_and_unknown_link() -> None:
    eng = _engine()
    feed(eng, generate(_sc(0.0, 5.0)))
    eng.step(t_ns(9.0))
    snap = eng.signal_snapshot(LINK, 30)
    assert snap["gaps"] and snap["gaps"][-1]["end"] == pytest.approx(BASE_UNIX_NS / 1e6 + 9000, abs=1)
    empty = eng.signal_snapshot("nope", 30)
    assert empty["score"]["t"] == [] and empty["latest_profile"]["t"] is None


def test_rejected_frames_are_counted_and_reported() -> None:
    eng = _engine()
    frames = [dataclasses.replace(f, layout_id=None) for f in generate(_sc(0.0, 4.0))]
    res = feed(eng, frames)
    st = eng.link_stats()[LINK]
    assert st["frames_rejected"] == len(frames) and st["rejections"] == {"UNKNOWN_LAYOUT": len(frames)}
    assert res and all(r.state == S.SENSOR_OFFLINE for r in res)
    assert any(x.startswith("FRAMES_REJECTED") for x in res[-1].reasons)


def test_history_and_feature_history() -> None:
    eng = _engine()
    feed(eng, generate(_sc(0.0, 20.0)))
    h_all = eng.history(LINK, 600)
    h5 = eng.history(LINK, 5)
    assert len(h5) < len(h_all) and 9 <= len(h5) <= 12
    f5 = eng.feature_history(LINK, 5)
    assert f5 and all(fv.t_end_ns >= f5[-1].t_end_ns - int(5e9) for fv in f5)
    assert eng.history("nope", 5) == [] and eng.feature_history("nope", 5) == []


def test_reset_clears_everything_and_drops_old_session_frames() -> None:
    eng = _engine()
    _calibrate(eng)
    assert eng.latest()
    eng.reset("sess-2", SourceMode.REPLAY)
    assert eng.latest() == {} and eng.baseline_status() == {} and eng.hardware_signature() is None
    feed(eng, generate(_sc(30.0, 2.0)))  # still tagged SESSION / SIMULATION
    assert eng.engine_stats()["foreign_session_frames"] > 0
    assert eng.link_ids() == []


def test_disconnect_and_end_of_stream_go_offline() -> None:
    eng = _engine()
    _calibrate(eng)
    eng.on_event(LinkEvent(LINK, RX, DISCONNECTED, "port closed"))
    r = eng.step(t_ns(20.2))
    assert r and r[0].state == S.SENSOR_OFFLINE and r[0].reasons[0].startswith("DISCONNECTED")
    feed(eng, generate(_sc(21.0, 4.0, seed=16)))
    assert eng.latest()[LINK].state != S.SENSOR_OFFLINE
    eng.on_event(EndOfStream("replay finished"))
    r = eng.step(t_ns(26.0))
    assert r and r[0].state == S.SENSOR_OFFLINE and r[0].reasons[0].startswith("END_OF_STREAM")


def test_drift_in_pipeline_is_unknown() -> None:
    eng = _engine()
    _calibrate(eng)
    res = feed(eng, generate(_sc(20.0, 12.0, profile_change_at=20.0, profile_phase2=np.pi, seed=17)))
    assert res[-1].state == S.UNKNOWN
    assert res[-1].reasons[0].startswith("STATIC_CHANNEL_CHANGED")
    assert eng.drift_status(LINK)[LINK]["suspected"] is True
    assert eng.baseline_status()[LINK]["has_baseline"] is True  # never silently replaced


def test_hello_rate_and_bounded_buffers() -> None:
    eng = _engine()
    eng.on_event(LinkEvent(LINK, RX, HELLO, data={"rate_hz": 100}))
    assert eng.link_stats()[LINK]["expected_rate_hz"] == 100.0
    feed(eng, generate(_sc(0.0, 30.0)))
    link = eng._links[LINK]  # internal invariant check
    assert len(link.samples) <= link.samples.maxlen and link.samples.maxlen >= 800
    assert link.results.maxlen is not None and link.display.maxlen is not None


def test_concurrent_readers_and_writer() -> None:
    eng = _engine()
    frames = generate(_sc(0.0, 20.0))
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                eng.signal_snapshot(LINK, 30)
                eng.latest()
                eng.history(LINK, 10)
                eng.hardware_signature()
        except BaseException as exc:  # noqa: BLE001 - surface any failure to the test
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for th in threads:
        th.start()
    try:
        for f in frames:
            eng.on_event(FrameEvent(f))
            eng.step(f.host_arrival_monotonic_ns)
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=5)
    assert not errors
    assert eng.latest()[LINK].state == S.UNKNOWN  # no baseline


def test_connected_event_does_not_mean_data() -> None:
    eng = _engine()
    feed(eng, generate(_sc(0.0, 3.0)))
    eng.on_event(LinkEvent(LINK, RX, DISCONNECTED, "unplugged"))
    eng.on_event(LinkEvent(LINK, RX, CONNECTED))
    r = eng.step(t_ns(4.0))  # 1 s old data, connected again: not offline yet
    assert all(x.state != S.SENSOR_OFFLINE for x in r)
    r = eng.step(t_ns(6.0))
    assert r and r[0].state == S.SENSOR_OFFLINE and r[0].reasons[0].startswith("STALE_DATA")


def test_link_event_before_frames_is_offline_with_no_frames() -> None:
    eng = _engine()
    eng.on_event(LinkEvent("tx2->rx2", "rx2", CONNECTED))
    r = eng.step(t_ns(1.0))
    assert r and r[0].link_id == "tx2->rx2" and r[0].state == S.SENSOR_OFFLINE
    assert any(x.startswith("NO_FRAMES") for x in r[0].reasons)
    assert r[0].provenance.source_mode == SourceMode.SIMULATION


def test_device_clock_fallback_is_processed_consistently() -> None:
    # Frames without host time: the device timestamp is the time axis, and
    # staleness uses the engine's own receipt clock.
    clock = {"now": 0}
    eng = ProcessingEngine(fast_cfg(), clock_ns=lambda: clock["now"], clock_unix_ns=lambda: BASE_UNIX_NS + clock["now"])
    eng.reset(SESSION, SourceMode.SIMULATION)
    frames = generate(_sc(0.0, 6.0))
    res = []
    for f in frames:
        t_rel = f.host_arrival_monotonic_ns - t_ns(0.0)
        dev = dataclasses.replace(f, host_arrival_monotonic_ns=None, host_arrival_unix_ns=None,
                                  device_timestamp_us=5_000_000 + t_rel // 1000,
                                  device_timestamp_unwrapped_us=5_000_000 + t_rel // 1000)
        clock["now"] = 10**12 + t_rel
        eng.on_event(FrameEvent(dev))
        res += eng.step(clock["now"])
    windows = [r for r in res if r.provenance.window_frame_count >= 20]
    assert windows and "DEVICE_TIME_BASE" in windows[-1].quality.flags
    assert all(r.state == S.UNKNOWN for r in res)  # no baseline
    assert windows[-1].provenance.window_end_unix_ns is None  # never invented
    clock["now"] += int(3e9)
    r = eng.step(clock["now"])
    assert r and r[0].state == S.SENSOR_OFFLINE
    snap = eng.signal_snapshot(LINK, 30)
    json.dumps(snap, allow_nan=False)
    assert snap["score"]["t"] == sorted(snap["score"]["t"])
    # The offline point is plotted after the last window, on one consistent axis.
    assert snap["score"]["t"][-1] > snap["score"]["t"][-2]


def test_interleaved_rejected_frames_degrade_quality() -> None:
    eng = _engine()
    frames = generate(_sc(0.0, 6.0, rate_hz=50.0))
    mixed = [f if i % 2 == 0 else dataclasses.replace(f, layout_id=None) for i, f in enumerate(frames)]
    res = feed(eng, mixed)
    late = [r for r in res if r.quality.frames_in_window >= 40]
    assert late
    q = late[-1].quality
    assert q.rejected_frames >= 40 and "MANY_REJECTED_FRAMES" in q.flags
    assert eng.link_stats()[LINK]["rejections"]["UNKNOWN_LAYOUT"] == len(frames) // 2


def test_downsampler_never_bridges_segments_and_respects_budget() -> None:
    t = np.arange(1000, dtype=float)
    v = np.vstack([t, -t]).T
    seg = t.astype(int) // 100  # 10 segments of 100 points
    out_t, out_v = _downsample_segments(t, v, seg, 50)
    assert len(out_t) == len(out_v) <= 50
    assert sum(row is None for row in out_v) == 9  # one marker between each pair of segments
    assert out_t == sorted(out_t)
    # Every non-marker row averages points of a single segment only.
    for tt, row in zip(out_t, out_v):
        if row is not None:
            assert int(row[0]) // 100 == int(tt) // 100
    # Too many segments for the budget: only the most recent ones are kept.
    seg_many = t.astype(int) // 5  # 200 segments
    out_t2, out_v2 = _downsample_segments(t, v, seg_many, 21)
    assert len(out_t2) <= 21 and out_t2[-1] > 990
    # Few points: returned as is.
    out_t3, out_v3 = _downsample_segments(t[:10], v[:10], np.zeros(10, dtype=int), 50)
    assert out_t3 == list(t[:10]) and all(r is not None for r in out_v3)
