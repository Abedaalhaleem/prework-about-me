"""End-to-end SIMULATED run of the ``demo_walk`` scenario through the runtime.

The simulator's toy model generates 300 s of data as fast as the processing
keeps up (non-realtime; the timeline is the simulated one). The operator
workflow is replayed on that timeline: quiet baseline during the empty
period, a walk test around the first walk, then the rest of the scenario.
This checks that the pieces are wired together; it says nothing about how
well motion detection works in a real room.
"""

from __future__ import annotations

from pathlib import Path

from roomsense.acquisition.base import FrameEvent
from roomsense.runtime import AppRuntime
from roomsense.schemas import (
    ActivityState,
    CapabilityId,
    CapabilityState,
    SourceMode,
    SourceState,
    ZoneState,
)
from tests.api_helpers import (  # noqa: F401  (autouse fixture)
    LINK,
    _no_api_token_in_env,
    make_offline_cfg,
    pump_until,
    rel_s,
)


def test_demo_walk_calibrate_detect_and_clear(tmp_path: Path) -> None:
    cfg = make_offline_cfg(tmp_path)
    rt = AppRuntime(cfg, background_processing=False)
    rt.start()
    try:
        frames_unix: list[int] = []
        original = rt.engine.on_event

        def spy(ev: object) -> None:
            if isinstance(ev, FrameEvent) and ev.frame.host_arrival_unix_ns is not None:
                frames_unix.append(ev.frame.host_arrival_unix_ns)
            original(ev)  # type: ignore[arg-type]

        rt.engine.on_event = spy  # type: ignore[method-assign]

        st = rt.start_simulation("demo_walk", acknowledge_simulated=True, realtime=False)
        sid = st.session_id
        assert st.source_banner == "SIMULATION" and st.simulated is True

        # Quiet baseline during the empty first 120 s (5 s .. 110 s of data).
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 5.0)
        rt.start_baseline(confirm_room_empty=True)
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 20.0)
        during = rt.build_status()
        assert during.calibration_detail and during.calibration_detail.startswith("CALIBRATING")
        assert all(a.state == ActivityState.CALIBRATING for a in during.activity)
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 110.0)
        rec = rt.stop_baseline()
        assert rec.valid and rec.invalidated_reason is None
        assert rec.source_mode == SourceMode.SIMULATION and rec.summary["simulated_data"] is True
        assert rec.session_id == sid and rec.link_ids == [LINK]
        assert rec.room_config_hash == rt.room().config_hash()
        assert rec.processing_config_version == cfg.config_version()
        assert rec.hardware_signature == rt.engine.hardware_signature()
        stored = rt.db.get_calibration(rec.calibration_id)
        assert stored is not None and stored.record.valid and LINK in stored.baselines

        st = rt.build_status()
        assert st.calibration_valid and st.calibration is not None
        motion_cap = next(c for c in st.capabilities if c.capability == CapabilityId.B_MOTION)
        assert motion_cap.state == CapabilityState.ENABLED
        assert "simulation only" in motion_cap.reasons[0]

        # Same-room walk test around the first walk (120-150 s).
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 117.0)
        rt.start_walk_test()
        pump_until(rt, lambda: (rt.data_elapsed_s() or 0.0) >= 152.0)
        walk = rt.stop_walk_test()
        assert walk.source_mode == SourceMode.SIMULATION and walk.session_id == sid
        assert len(walk.links) == 1 and walk.links[0].detected and walk.links[0].motion_windows > 0
        assert "does not establish behind-wall performance" in walk.note

        pump_until(rt, lambda: rt.build_status().source_state == SourceState.FINISHED)
        rt.pump()

        hist = rt.activity_history(LINK)
        assert len(hist) > 480  # ~ (300 s - 10 s disconnect) / hop_s 0.5 s
        t0 = min(frames_unix)
        for r in hist:
            assert r.provenance.source_mode == SourceMode.SIMULATION
            assert r.provenance.session_id == sid
            assert r.provenance.config_version == cfg.config_version()
            assert r.calibrated_probability is None
        timed = [(rel_s(r, t0), r) for r in hist]
        before_walk = [r for t, r in timed if t is not None and 111.0 < t < 119.8]
        walking = [r for t, r in timed if t is not None and 120.0 < t <= 152.0]
        after_walk = [r for t, r in timed if t is not None and 158.0 <= t < 180.0]
        assert before_walk and all(r.state == ActivityState.NO_MOTION_DETECTED for r in before_walk)
        assert sum(r.state == ActivityState.MOTION_DETECTED for r in walking) >= 20
        assert all(r.provenance.calibration_id == rec.calibration_id
                   for r in walking if r.state == ActivityState.MOTION_DETECTED)
        assert any(r.state == ActivityState.NO_MOTION_DETECTED for r in after_walk)
        assert not any(r.state == ActivityState.MOTION_DETECTED for t, r in timed if t is not None and 165 <= t < 180)
        # The 10 s disconnect and the end of the scenario read as offline, never as "no motion".
        assert any(r.state == ActivityState.SENSOR_OFFLINE for r in hist)
        assert rt.link_states()[LINK] == ActivityState.SENSOR_OFFLINE

        st = rt.build_status()
        assert st.zone.state == ZoneState.DISABLED and st.pose.enabled is False
        assert st.through_wall_status == "UNVERIFIED"

        rt.stop_source()
        rows = rt.db.list_activity(session_id=sid)
        assert len(rows) == len(hist)  # one row per result
        assert {r.source_mode for r in rows} == {SourceMode.SIMULATION}
    finally:
        rt.shutdown()
