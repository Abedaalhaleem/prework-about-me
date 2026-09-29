"""Safety invariants of the integrated runtime (required by the product owner).

All inputs are synthetic: fake serial ports emitting hand-made firmware lines,
or the SIMULATION generator. The tests check what the software claims, never
how well Wi-Fi sensing works.

(a) disconnected hardware never triggers synthetic live detections
(b) unsupported location/pose outputs remain disabled
(c) low-quality or stale data never becomes NO_MOTION_DETECTED
(d) a simulation cannot start without an explicit acknowledgement
(e) a replay of a synthetic recording reports simulated=True
plus: dropped events are counted and reported, never silent; frames that do
not match the live session never reach the engine; baselines made on
simulated data are never applied to live data; stale zone estimates are not
presented as current.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import roomsense.runtime as runtime_mod
from roomsense.acquisition.base import FrameEvent, SourceEvent
from roomsense.acquisition.serial_source import LiveSerialSource
from roomsense.acquisition.synthetic import (
    ZONES_3RX_LINKS,
    ZONES_3RX_POSITIONS,
    ZONES_ENVIRONMENT_SEED,
    Episode,
    SyntheticScenario,
)
from roomsense.cli import main as cli_main
from roomsense.runtime import AppRuntime, OperationRefused
from roomsense.schemas import (
    ActivityResult,
    ActivityState,
    CalibrationKind,
    CalibrationRecord,
    CapabilityId,
    CapabilityState,
    InputFormat,
    QualityFlag,
    QualityLevel,
    SourceMode,
    SourceState,
    SystemStatus,
    ZonePrediction,
    ZoneState,
)
from tests.api_helpers import (  # noqa: F401  (_no_api_token_in_env is an autouse fixture)
    CONSENT,
    FAST_CALIBRATION,
    LINK,
    MissingPortFactory,
    PacedSerialFactory,
    _no_api_token_in_env,
    make_cfg,
    make_offline_cfg,
    pump_until,
    receiver_cfg,
    rel_s,
    wait_until,
)
from tests.proc_helpers import make_frame as make_synthetic_frame
from tests.zone_helpers import fake_provenance, save_fake_model

DECISION_STATES = (ActivityState.NO_MOTION_DETECTED, ActivityState.MOTION_DETECTED)


def _cap(status: SystemStatus, cap: CapabilityId) -> CapabilityState:
    return next(c.state for c in status.capabilities if c.capability == cap)


def _spy_engine(rt: AppRuntime) -> list[SourceEvent]:
    """Record every event that actually reaches the processing engine."""
    seen: list[SourceEvent] = []
    original = rt.engine.on_event

    def spy(ev: SourceEvent) -> None:
        seen.append(ev)
        original(ev)

    rt.engine.on_event = spy  # type: ignore[method-assign]
    return seen


def _forbid_non_live_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("a replay or simulation source was constructed during a LIVE test")

    monkeypatch.setattr(runtime_mod, "SyntheticSource", forbidden)
    monkeypatch.setattr(runtime_mod, "ReplaySource", forbidden)


def _assert_live_only(rt: AppRuntime, st: SystemStatus, seen: list[SourceEvent]) -> list[ActivityResult]:
    assert st.source_mode == SourceMode.LIVE
    assert st.source_banner == "LIVE MEASUREMENTS"
    assert st.simulated is False
    assert isinstance(rt.manager.active, LiveSerialSource)  # never swapped for anything else
    for ev in seen:
        if isinstance(ev, FrameEvent):
            f = ev.frame
            assert f.source_mode == SourceMode.LIVE
            assert f.session_id == st.session_id
            assert QualityFlag.SYNTHETIC.value not in f.quality_flags
            assert QualityFlag.REPLAYED.value not in f.quality_flags
            assert f.input_format != InputFormat.SYNTHETIC_V1
    hist = rt.activity_history(LINK)
    assert hist, "the offline state must be reported, not left silent"
    assert all(r.provenance.source_mode == SourceMode.LIVE for r in hist)
    assert all(r.state != ActivityState.MOTION_DETECTED for r in hist)
    assert all(r.calibrated_probability is None for r in hist)
    assert rt.link_states()[LINK] in (ActivityState.SENSOR_OFFLINE, ActivityState.UNKNOWN)
    assert st.activity and all(a.state in (ActivityState.SENSOR_OFFLINE, ActivityState.UNKNOWN) for a in st.activity)
    assert _cap(st, CapabilityId.A_ACQUISITION) != CapabilityState.ENABLED
    return hist


# ---------------------------------------------------------------------------
# (a) disconnected hardware never triggers synthetic live detections
# ---------------------------------------------------------------------------


def test_a_missing_board_stays_live_disconnected_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_non_live_sources(monkeypatch)
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    factory = MissingPortFactory()
    with AppRuntime(cfg, serial_factory=factory) as rt:
        seen = _spy_engine(rt)
        rt.start_live()
        assert wait_until(lambda: rt.build_status().source_state == SourceState.DISCONNECTED, 5)
        time.sleep(1.0)  # twice stale_after_s, processed on the wall clock
        st = rt.build_status()
        hist = _assert_live_only(rt, st, seen)
        assert st.source_state == SourceState.DISCONNECTED
        assert "not falling back" in (st.source_detail or "")
        assert not [e for e in seen if isinstance(e, FrameEvent)]
        assert all(r.state == ActivityState.SENSOR_OFFLINE for r in hist)
        assert st.hardware_required is True
        assert factory.opens >= 2  # keeps retrying the configured port, nothing else
        rt.stop_source()
        rows = rt.db.list_activity(session_id=st.session_id)
        assert rows and all(r.source_mode == SourceMode.LIVE and r.state == "SENSOR_OFFLINE" for r in rows)


def test_a_board_unplugged_mid_stream_goes_offline_never_other_modes(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_non_live_sources(monkeypatch)
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    factory = PacedSerialFactory(rate_hz=50, fail_after=60)  # ~1.2 s of lines, then the read raises
    with AppRuntime(cfg, serial_factory=factory) as rt:
        seen = _spy_engine(rt)
        rt.start_live()
        assert wait_until(lambda: rt.build_status().source_state == SourceState.RUNNING, 5)
        assert wait_until(lambda: rt.build_status().source_state == SourceState.DISCONNECTED, 8)
        time.sleep(1.0)
        st = rt.build_status()
        hist = _assert_live_only(rt, st, seen)
        frames = [e.frame for e in seen if isinstance(e, FrameEvent)]
        assert 30 <= len(frames) <= 60
        # No baseline was recorded, so nothing may claim "no motion" either.
        assert all(r.state not in DECISION_STATES for r in hist)
        assert rt.link_states()[LINK] == ActivityState.SENSOR_OFFLINE
        assert st.hardware_required is False  # real (fake-port) LIVE frames with a layout arrived


# ---------------------------------------------------------------------------
# (b) unsupported location/pose outputs remain disabled
# ---------------------------------------------------------------------------


def _three_link_motion_scenario(seed: int = 5, duration_s: float = 36.0) -> SyntheticScenario:
    quiet_s = min(20.0, duration_s)
    episodes = [Episode(0.0, quiet_s, "EMPTY")]
    if duration_s > quiet_s:
        episodes.append(Episode(quiet_s, duration_s, "MOTION", {"zone": "A"}))
    return SyntheticScenario(
        name="safety_motion_3rx",
        duration_s=duration_s,
        rate_hz=25.0,
        links=list(ZONES_3RX_LINKS),
        seed=seed,
        node_positions=dict(ZONES_3RX_POSITIONS),
        params={"environment_seed": ZONES_ENVIRONMENT_SEED},
        description="SIMULATED: 20 s empty, then walking in zone A (software test only).",
        episodes=episodes,
    )


def _run_offline(rt: AppRuntime, scn: SyntheticScenario, *, calibrate: tuple[float, float] | None) -> str:
    st = rt.start_simulation(scn, acknowledge_simulated=True, realtime=False)
    if calibrate is not None:
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= calibrate[0])
        rt.start_baseline(confirm_room_empty=True)
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= calibrate[1])
        assert rt.stop_baseline().valid
    pump_until(rt, lambda: rt.build_status().source_state == SourceState.FINISHED)
    rt.pump()
    assert st.session_id is not None
    return st.session_id


SHORT_BASELINE = {"detection": {"baseline_min_duration_s": 10.0, "baseline_min_windows": 10}}


def test_b_zone_and_pose_stay_disabled_after_simulated_motion(tmp_path: Path) -> None:
    cfg = make_offline_cfg(tmp_path, **SHORT_BASELINE)
    rt = AppRuntime(cfg, background_processing=False)
    rt.start()
    try:
        predictions: list[ZonePrediction] = []
        original = rt.predictor.predict

        def spy(*a: Any, **k: Any) -> ZonePrediction:
            out = original(*a, **k)
            predictions.append(out)
            return out

        rt.predictor.predict = spy  # type: ignore[method-assign]
        _run_offline(rt, _three_link_motion_scenario(), calibrate=(2.0, 18.0))
        st = rt.build_status()
        # The run exercised the motion path (otherwise the check below would be vacuous).
        assert any(r.state == ActivityState.MOTION_DETECTED
                   for lid in rt.engine.link_ids() for r in rt.activity_history(lid))
        assert predictions and all(p.state == ZoneState.DISABLED for p in predictions)
        assert st.zone.state == ZoneState.DISABLED
        assert st.zone.zone_id is None and st.zone.display_anchor is None
        assert st.localization_status.startswith("DISABLED")
        assert st.pose.enabled is False and st.pose.missing_requirements
        assert _cap(st, CapabilityId.C_ZONE) != CapabilityState.ENABLED
        assert _cap(st, CapabilityId.D_POSE) != CapabilityState.ENABLED
    finally:
        rt.shutdown()


def test_b_even_a_matching_enabled_model_never_estimates_on_simulated_data(tmp_path: Path) -> None:
    """A HAND-BUILT fake model (fake passing report) is bound to exactly the
    simulated hardware, room and config, so the zone gate itself is open. Zone
    output must still stay DISABLED for SIMULATION and for a replay of it."""
    cfg = make_offline_cfg(tmp_path, **SHORT_BASELINE)
    rt = AppRuntime(cfg, background_processing=False)
    rt.start()
    try:
        # The hardware signature depends only on the (simulated) link identities.
        _run_offline(rt, _three_link_motion_scenario(seed=6, duration_s=4.0), calibrate=None)
        signature = rt.engine.hardware_signature()
        assert signature is not None
        save_fake_model(rt.registry, room=rt.room(), hardware_signature=signature, config_version=rt.config_version)
        rt.predictor.refresh()
        rt._zone_status_cache.clear()
        assert rt.zone_status()["state"] == "ENABLED"  # the gate is genuinely open

        # SIMULATION: the predictor itself refuses.
        st = rt.start_simulation(_three_link_motion_scenario(seed=6), acknowledge_simulated=True, realtime=False)
        rec = rt.start_recording(consent=CONSENT, label="synthetic capture for the replay check")
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 12.0)
        st = rt.build_status()
        assert st.zone.state == ZoneState.DISABLED
        assert any(r.startswith("SIMULATED_SOURCE") for r in st.zone.reasons)
        assert _cap(st, CapabilityId.C_ZONE) == CapabilityState.DISABLED
        info = rt.stop_recording()
        assert info.recording_id == rec.recording_id and info.synthetic

        # REPLAY of that simulated recording: the runtime keeps it DISABLED as well.
        rt.start_replay(info.recording_id, speed=200.0)
        pump_until(rt, lambda: rt.build_status().source_state == SourceState.FINISHED, timeout=30)
        rt.pump()
        st = rt.build_status()
        assert st.source_mode == SourceMode.REPLAY and st.simulated is True
        assert st.zone.state == ZoneState.DISABLED and st.zone.zone_id is None
        assert _cap(st, CapabilityId.C_ZONE) != CapabilityState.ENABLED
        assert st.pose.enabled is False
    finally:
        rt.shutdown()


# ---------------------------------------------------------------------------
# (c) low quality / stale data never becomes NO_MOTION_DETECTED
# ---------------------------------------------------------------------------


def _assert_no_motion_only_on_fresh_good_data(results: list[ActivityResult], cfg: Any) -> None:
    for r in results:
        if r.state != ActivityState.NO_MOTION_DETECTED:
            continue
        assert r.quality.level in (QualityLevel.GOOD, QualityLevel.DEGRADED), r
        assert r.provenance.window_end_unix_ns is not None, r
        assert r.provenance.measurement_age_s is not None
        assert r.provenance.measurement_age_s <= cfg.acquisition.stale_after_s
        assert r.quality.frames_in_window >= cfg.processing.min_frames_per_window


def _spy_steps(rt: AppRuntime) -> list[tuple[int, ActivityResult]]:
    """Record (processing time, result) for every result the engine emits.
    Rejected windows carry no window times, so results are timed by the step."""
    out: list[tuple[int, ActivityResult]] = []
    original = rt.engine.step

    def spy(now_ns: int | None = None) -> list[ActivityResult]:
        res = original(now_ns)
        out.extend((int(now_ns if now_ns is not None else time.monotonic_ns()), r) for r in res)
        return res

    rt.engine.step = spy  # type: ignore[method-assign]
    return out


def test_c_packet_loss_and_disconnect_never_read_as_no_motion(tmp_path: Path) -> None:
    cfg = make_offline_cfg(tmp_path)  # default detection: 60 s quiet baseline
    scn = SyntheticScenario(
        name="safety_degraded",
        duration_s=120.0,
        rate_hz=25.0,
        links=[("tx1", "rx1")],
        seed=21,
        description="SIMULATED: empty room, heavy packet loss, a disconnect (software test only).",
        episodes=[Episode(70.0, 85.0, "PACKET_LOSS", {"fraction": 0.9}), Episode(95.0, 110.0, "DISCONNECT")],
    )
    rt = AppRuntime(cfg, background_processing=False)
    rt.start()
    try:
        seen = _spy_engine(rt)
        steps = _spy_steps(rt)
        _run_offline(rt, scn, calibrate=(3.0, 66.0))
        hist = rt.activity_history(LINK)
        assert len(hist) == len(steps)
        _assert_no_motion_only_on_fresh_good_data(hist, cfg)
        t0 = min(e.frame.host_arrival_monotonic_ns for e in seen if isinstance(e, FrameEvent))
        timed = [((now - t0) / 1e9, r) for now, r in steps]
        # Good data after calibration really was decided (the checks below are not vacuous).
        assert any(r.state == ActivityState.NO_MOTION_DETECTED for t, r in timed if 67 < t < 70)
        lossy = [r for t, r in timed if 73.0 <= t <= 85.0]
        assert len(lossy) > 10 and all(r.state not in DECISION_STATES for r in lossy)
        assert any(any(x.startswith(("LOW_QUALITY", "WINDOW_REJECTED")) for x in r.reasons) for r in lossy)
        gap = [r for t, r in timed if 95.5 < t < 110.0]
        assert all(r.state not in DECISION_STATES for r in gap)
        offline = [r for r in hist if r.state == ActivityState.SENSOR_OFFLINE]
        assert any("DISCONNECTED" in " ".join(r.reasons) for r in offline)
        assert rt.link_states()[LINK] == ActivityState.SENSOR_OFFLINE  # end of stream
    finally:
        rt.shutdown()


def test_c_live_stale_after_calibration_is_offline_not_empty(tmp_path: Path) -> None:
    """A calibrated LIVE link whose board vanishes must not keep saying 'no motion'."""
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]}, **FAST_CALIBRATION)
    # Identical CSI values in every line: a perfectly still (made-up) channel,
    # so every decision while data flows is deterministic NO_MOTION_DETECTED.
    factory = PacedSerialFactory(rate_hz=50, fail_after=180, identical=True)
    with AppRuntime(cfg, serial_factory=factory) as rt:
        seen = _spy_engine(rt)
        rt.start_live()
        assert wait_until(lambda: rt.build_status().source_state == SourceState.RUNNING, 5)
        time.sleep(0.3)
        rt.start_baseline(confirm_room_empty=True)
        time.sleep(2.0)
        assert rt.stop_baseline().valid
        assert wait_until(lambda: rt.build_status().source_state == SourceState.DISCONNECTED, 8)
        time.sleep(1.0)
        st = rt.build_status()
        hist = _assert_live_only(rt, st, seen)
        _assert_no_motion_only_on_fresh_good_data(hist, cfg)
        last_frame_unix = max(e.frame.host_arrival_unix_ns for e in seen if isinstance(e, FrameEvent))
        stale_after_ns = int(cfg.acquisition.stale_after_s * 1e9)
        assert any(r.state == ActivityState.NO_MOTION_DETECTED for r in hist)  # decisions were made
        late = [r for r in hist if r.provenance.computed_at_unix_ns > last_frame_unix + stale_after_ns + 50_000_000]
        assert late and all(r.state in (ActivityState.SENSOR_OFFLINE, ActivityState.UNKNOWN) for r in late)
        assert rt.link_states()[LINK] == ActivityState.SENSOR_OFFLINE
        assert _cap(st, CapabilityId.B_MOTION) != CapabilityState.ENABLED


# ---------------------------------------------------------------------------
# (d) simulation requires an explicit acknowledgement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ack", [False, None, "true", 1, "yes"])
def test_d_simulation_refused_without_acknowledgement(tmp_path: Path, ack: Any) -> None:
    with AppRuntime(make_cfg(tmp_path), background_processing=False) as rt:
        with pytest.raises(OperationRefused) as ei:
            rt.start_simulation("demo_walk", acknowledge_simulated=ack)
        assert ei.value.code == "SIMULATION_NOT_ACKNOWLEDGED" and ei.value.http_status == 422
        assert rt.mode is None and rt.manager.active is None
        st = rt.build_status()
        assert st.source_banner == "NO SOURCE" and st.simulated is False


# ---------------------------------------------------------------------------
# (e) replay of synthetic data reports simulated=True
# ---------------------------------------------------------------------------


def test_e_replay_of_recorded_simulation_is_simulated(tmp_path: Path) -> None:
    rt = AppRuntime(make_offline_cfg(tmp_path), background_processing=False)
    rt.start()
    try:
        seen = _spy_engine(rt)
        rt.start_simulation("quiet_only", acknowledge_simulated=True, realtime=False)
        rt.start_recording(consent=CONSENT, label="simulated capture")
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 4.0)
        info = rt.stop_recording()
        assert info.synthetic and info.original_source_mode == SourceMode.SIMULATION and info.frames > 50

        st = rt.start_replay(info.recording_id, speed=10.0)
        assert st.source_mode == SourceMode.REPLAY and st.source_banner == "RECORDED REPLAY"
        assert st.simulated is True  # known before the first replayed frame
        pump_until(rt, lambda: rt.build_status().source_state == SourceState.FINISHED, timeout=30)
        st = rt.build_status()
        assert st.simulated is True
        assert any(n.startswith("SIMULATED_DATA") for n in st.notes)
        replayed = [e.frame for e in seen if isinstance(e, FrameEvent) and e.frame.session_id == st.session_id]
        assert replayed and all(QualityFlag.SYNTHETIC.value in f.quality_flags
                                and QualityFlag.REPLAYED.value in f.quality_flags for f in replayed)
        assert _cap(st, CapabilityId.A_ACQUISITION) == CapabilityState.HARDWARE_REQUIRED
        assert _cap(st, CapabilityId.C_ZONE) != CapabilityState.ENABLED
        # The capability texts must not call simulated data "recorded measurements".
        reasons = [r for c in st.capabilities for r in c.reasons]
        assert not any("recorded measurements" in r for r in reasons)
        assert any("never enabled on simulated data" in r
                   for c in st.capabilities if c.capability == CapabilityId.C_ZONE for r in c.reasons)
    finally:
        rt.shutdown()


def test_e_replay_of_cli_simulated_file_is_simulated(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    rec_dir = cfg.storage.resolved_data_dir() / "recordings"
    rec_dir.mkdir(parents=True)
    assert cli_main(["simulate", "--scenario", "disconnect", "--seed", "4",
                     "--out", str(rec_dir / "sim_cli_file.jsonl.gz")]) == 0
    with AppRuntime(cfg, background_processing=False) as rt:
        st = rt.start_replay("sim_cli_file", speed=100.0)
        assert st.source_mode == SourceMode.REPLAY and st.simulated is True


# ---------------------------------------------------------------------------
# Further invariants
# ---------------------------------------------------------------------------


def test_host_queue_drops_are_counted_and_reported(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()], "frame_queue_size": 64})
    # No processing thread: the queue fills up, and the live reader must not wait.
    rt = AppRuntime(cfg, serial_factory=PacedSerialFactory(rate_hz=400), background_processing=False)
    rt.start()
    try:
        rt.start_live()
        assert wait_until(lambda: rt.queue_drops().get(LINK, 0) > 20, 5)
        st = rt.build_status()
        assert any(n.startswith("HOST_QUEUE_DROPS") and LINK in n for n in st.notes)
        assert rt.pump(1000) >= 64  # everything that was queued is still processed
    finally:
        rt.shutdown()


def test_frames_not_matching_the_live_session_never_reach_the_engine(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    rt = AppRuntime(cfg, serial_factory=MissingPortFactory(), background_processing=False)
    rt.start()
    try:
        seen = _spy_engine(rt)
        st = rt.start_live()
        # White-box: feed the runtime's acquisition consumer directly, as a buggy
        # source would. A SYNTHETIC frame claiming LIVE, and a frame of another session.
        rt._consume(FrameEvent(make_synthetic_frame((1, 2) * 64, t_s=1.0, session_id=st.session_id,
                                                    source_mode=SourceMode.LIVE)))
        rt._consume(FrameEvent(make_synthetic_frame((1, 2) * 64, t_s=1.1, session_id="other_session",
                                                    source_mode=SourceMode.REPLAY)))
        rt.pump()
        assert not [e for e in seen if isinstance(e, FrameEvent)]
        notes = rt.build_status().notes
        assert any(n.startswith("FRAMES_REJECTED_SOURCE_MISMATCH: 2") for n in notes)
    finally:
        rt.shutdown()


def test_simulated_baselines_are_never_applied_to_live(tmp_path: Path) -> None:
    def record(mode: SourceMode, simulated: bool) -> CalibrationRecord:
        return CalibrationRecord(
            calibration_id="cal-x", kind=CalibrationKind.QUIET_BASELINE, created_at_unix_ns=1, session_id="s",
            source_mode=mode, link_ids=[LINK], hardware_signature="hw", room_config_hash="room",
            processing_config_version="cfg", duration_s=60, frame_count=1, window_count=1, valid=True,
            summary={"simulated_data": simulated})

    applicable = AppRuntime._calibration_applicable
    assert applicable(record(SourceMode.SIMULATION, True), SourceMode.LIVE) is False
    assert applicable(record(SourceMode.REPLAY, True), SourceMode.LIVE) is False
    assert applicable(record(SourceMode.LIVE, False), SourceMode.LIVE) is True

    cfg = make_offline_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]}, **SHORT_BASELINE)
    rt = AppRuntime(cfg, serial_factory=MissingPortFactory(), background_processing=False)
    rt.start()
    try:
        rt.start_simulation("quiet_only", acknowledge_simulated=True, realtime=False)
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 1.0)
        rt.start_baseline(confirm_room_empty=True)
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 14.0)
        rec = rt.stop_baseline()
        assert rec.valid and rec.source_mode == SourceMode.SIMULATION and rec.summary["simulated_data"] is True
        assert rt.build_status().calibration_valid is True
        st = rt.start_live()
        rt.pump()
        st = rt.build_status()
        assert st.calibration is None and st.calibration_valid is False
        assert not any(v["has_baseline"] for v in rt.engine.baseline_status().values())
        assert _cap(st, CapabilityId.B_MOTION) != CapabilityState.ENABLED
    finally:
        rt.shutdown()


def test_old_zone_estimate_is_not_presented_as_current(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, acquisition={"receivers": [receiver_cfg()]})
    rt = AppRuntime(cfg, serial_factory=MissingPortFactory(), background_processing=False)
    rt.start()
    try:
        st = rt.start_live()
        old = time.monotonic_ns() - int(10 * 1e9)
        pred = ZonePrediction(state=ZoneState.ESTIMATE, zone_id="A", zone_label="Zone A", model_id="fake",
                              provenance=fake_provenance())
        with rt._state_lock:
            rt._zone_pred = (st.session_id, old, pred)
        zone = rt.build_status().zone
        assert zone.state == ZoneState.ABSTAIN and zone.zone_id is None
        assert zone.reasons[0].startswith("ZONE_STALE")
    finally:
        rt.shutdown()


def test_every_result_is_logged_once(tmp_path: Path) -> None:
    """Validation derives observation time from activity_log: one row per result."""
    rt = AppRuntime(make_offline_cfg(tmp_path), background_processing=False)
    rt.start()
    try:
        st = rt.start_simulation("disconnect", acknowledge_simulated=True, realtime=False)
        pump_until(rt, lambda: rt.build_status().source_state == SourceState.FINISHED)
        rt.pump()
        n_results = len(rt.activity_history(LINK))
        rt.stop_source()
        rows = rt.db.list_activity(session_id=st.session_id)
        assert n_results > 100 and len(rows) == n_results
        assert Counter(r.source_mode for r in rows) == Counter({SourceMode.SIMULATION: n_results})
    finally:
        rt.shutdown()


def test_state_left_by_a_crash_is_closed_and_never_counts(tmp_path: Path) -> None:
    """A validation run that was never stopped (the app died) becomes ABORTED."""
    from roomsense.runtime import DB_FILENAME
    from roomsense.storage.db import Database
    from roomsense.storage.models import SessionRecord, ValidationRun, ValidationRunStatus

    cfg = make_cfg(tmp_path)
    data_dir = cfg.storage.resolved_data_dir()
    data_dir.mkdir(parents=True)
    with Database(data_dir / DB_FILENAME) as db:
        db.add_session(SessionRecord(session_id="live_crashed", created_at_unix_ns=1_000, source_mode=SourceMode.LIVE))
        db.add_validation_run(ValidationRun(scenario_id="S1", session_id="live_crashed", source_mode=SourceMode.LIVE,
                                            started_at_unix_ns=2_000, placement="p", wall_description="w",
                                            channel=6, conditions="c"))
    with AppRuntime(cfg, background_processing=False) as rt:
        run = rt.db.list_validation_runs()[0]
        assert run.status == ValidationRunStatus.ABORTED and run.ended_at_unix_ns == run.started_at_unix_ns
        assert rt.db.get_session("live_crashed").ended_at_unix_ns is not None  # type: ignore[union-attr]
        report = rt.validation_report(force=True)
        assert report["through_wall_status"] == "UNVERIFIED"
        assert run.run_id in [x["run_id"] for x in report["excluded_runs"]]
