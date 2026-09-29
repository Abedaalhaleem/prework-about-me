"""AcquisitionManager tests with fake and synthetic sources (software only)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from roomsense.acquisition.base import EndOfStream, FrameEvent, LinkEvent
from roomsense.acquisition.manager import AcquisitionManager
from roomsense.acquisition.synthetic import EMPTY, Episode, SyntheticScenario, SyntheticSource, generate_frames
from roomsense.config import AcquisitionConfig, AppConfig
from roomsense.schemas import SourceMode, SourceState
from tests.acq_helpers import FakeSource, make_frame

L1, L2 = "tx1->rx1", "tx1->rx2"
S = 1_000_000_000  # ns per second


def cfg(**acq) -> AppConfig:
    return AppConfig(acquisition=AcquisitionConfig(**acq))


def link(ev_kind: str, link_id: str = L1, **data) -> LinkEvent:
    return LinkEvent(link_id=link_id, receiver_id=link_id.split("->")[1], kind=ev_kind, data=data)


def status(mgr: AcquisitionManager, link_id: str, now_ns: int):
    return next(s for s in mgr.link_statuses(now_ns) if s.link_id == link_id)


def test_initial_state_and_explicit_switching():
    mgr = AcquisitionManager(cfg())
    assert mgr.source_state() == SourceState.NO_SOURCE and mgr.active is None
    assert mgr.link_statuses() == []
    live = FakeSource(SourceMode.LIVE, [L1])
    mgr.start(live)
    assert mgr.active is live and mgr.source_state() == SourceState.CONNECTING
    old_sink = live.sink
    sim = FakeSource(SourceMode.SIMULATION, [L1])
    mgr.start(sim)
    assert live.stopped and mgr.active is sim and mgr.mode == SourceMode.SIMULATION
    assert mgr.source_state() == SourceState.RUNNING
    # Late events from the previous source are dropped, never mixed in.
    received = []
    mgr.add_consumer(received.append)
    old_sink(FrameEvent(make_frame()))
    assert received == [] and status(mgr, L1, 2 * S).frames_total == 0
    mgr.stop()
    assert sim.stopped and mgr.active is None and mgr.source_state() == SourceState.NO_SOURCE


def test_live_disconnect_never_falls_back():
    mgr = AcquisitionManager(cfg())
    live = FakeSource(SourceMode.LIVE, [L1, L2])
    mgr.start(live)
    live.push(link("CONNECTED", L1))
    assert mgr.source_state() == SourceState.RUNNING
    live.push(link("DISCONNECTED", L1))
    assert mgr.source_state() == SourceState.CONNECTING  # L2 has not reported yet
    live.push(link("DISCONNECTED", L2))
    assert mgr.source_state() == SourceState.DISCONNECTED
    live.push(link("RECONNECTING", L1))
    assert mgr.source_state() == SourceState.DISCONNECTED
    assert mgr.active is live and mgr.mode == SourceMode.LIVE  # still the user's choice
    assert "not falling back" in (mgr.detail or "")
    live.push(link("CONNECTED", L2))
    assert mgr.source_state() == SourceState.RUNNING


def test_link_status_staleness_and_disconnect():
    mgr = AcquisitionManager(cfg(stale_after_s=2.0))
    live = FakeSource(SourceMode.LIVE, [L1])
    mgr.start(live)
    t = 10 * S
    live.push(FrameEvent(make_frame(host_mono_ns=t)))
    s = status(mgr, L1, t + 1 * S)
    assert s.connected and s.last_frame_age_s == pytest.approx(1.0)
    s = status(mgr, L1, t + 3 * S)
    assert not s.connected and s.last_frame_age_s == pytest.approx(3.0)
    live.push(FrameEvent(make_frame(host_mono_ns=t + 4 * S)))
    assert status(mgr, L1, t + 4 * S + S // 10).connected
    live.push(link("DISCONNECTED"))
    assert not status(mgr, L1, t + 4 * S + S // 10).connected
    live.push(FrameEvent(make_frame(host_mono_ns=t + 5 * S)))  # data again => link is back
    assert status(mgr, L1, t + 5 * S).connected


def test_rate_counts_and_metadata():
    mgr = AcquisitionManager(cfg())
    live = FakeSource(SourceMode.LIVE, [L1])
    mgr.start(live)
    t0 = 100 * S
    for i in range(150):  # 25 Hz for 6 s
        live.push(FrameEvent(make_frame(host_mono_ns=t0 + i * S // 25, counter=i, drops=i // 50)))
    live.push(FrameEvent(make_frame(host_mono_ns=t0 + 6 * S, layout_id=None, counter=150, drops=3)))
    live.push(link("PARSE_ERROR", code="CRC_MISMATCH", count=1))
    live.push(link("PARSE_ERROR", code="MAC_NOT_CONFIGURED", count=30, summary=True))
    s = status(mgr, L1, t0 + 6 * S)
    assert s.acquisition_rate_hz == pytest.approx(25.2, abs=0.5)
    assert s.frames_total == 151 and s.frames_rejected == 1
    assert s.parse_errors == 31
    assert s.firmware_drops == 3
    assert s.layout_id is None and s.channel == 6
    assert s.device["chip"] == "esp32s3" and s.device["identity_source"] == "user_config"
    assert s.receiver_id == "rx1" and s.transmitter_id == "tx1"
    # Stream stops: the measured rate decays instead of freezing.
    assert status(mgr, L1, t0 + 9 * S).acquisition_rate_hz < 15
    assert status(mgr, L1, t0 + 20 * S).acquisition_rate_hz == 0.0


def test_clock_model_only_for_live_frames():
    rng = np.random.default_rng(0)
    for mode, expect in [(SourceMode.LIVE, True), (SourceMode.SIMULATION, False)]:
        mgr = AcquisitionManager(cfg())
        src = FakeSource(mode, [L1])
        mgr.start(src)
        for i in range(25 * 60):
            dev_us = 3_000_000 + i * 40_000
            host = 50 * S + int(i * 40_000_000 * (1 + 30e-6)) + int(rng.exponential(1e6))
            src.push(FrameEvent(make_frame(mode=mode, host_mono_ns=host, device_us=dev_us, counter=i)))
        s = status(mgr, L1, host)
        if expect:
            assert s.clock_drift_ppm == pytest.approx(30.0, abs=5.0)
            assert s.clock_offset_ms is not None
        else:
            assert s.clock_drift_ppm is None and s.clock_offset_ms is None


def test_end_of_stream_states():
    mgr = AcquisitionManager(cfg())
    rep = FakeSource(SourceMode.REPLAY, [L1])
    mgr.start(rep)
    rep.push(EndOfStream("END_OF_RECORDING"))
    assert mgr.source_state() == SourceState.FINISHED
    rep2 = FakeSource(SourceMode.REPLAY, [L1])
    mgr.start(rep2)
    rep2.push(EndOfStream("ERROR: RecordingFormatError: bad"))
    assert mgr.source_state() == SourceState.ERROR and "bad" in mgr.detail


def test_start_failure_sets_error_and_propagates():
    mgr = AcquisitionManager(cfg())
    with pytest.raises(RuntimeError):
        mgr.start(FakeSource(SourceMode.LIVE, [L1], fail_on_start=True))
    assert mgr.source_state() == SourceState.ERROR
    assert "cannot open hardware" in (mgr.detail or "")


def test_consumer_exceptions_are_isolated():
    mgr = AcquisitionManager(cfg())
    got = []

    def broken(ev):
        raise ValueError("consumer bug")

    mgr.add_consumer(broken)
    mgr.add_consumer(got.append)
    mgr.add_consumer(got.append)  # duplicate registration is ignored
    src = FakeSource(SourceMode.SIMULATION, [L1])
    mgr.start(src)
    for i in range(3):
        src.push(FrameEvent(make_frame(mode=SourceMode.SIMULATION, counter=i)))
    assert len(got) == 3
    mgr.remove_consumer(got.append)
    src.push(FrameEvent(make_frame(mode=SourceMode.SIMULATION)))
    assert len(got) == 3


def test_unknown_links_are_tracked_but_bounded():
    mgr = AcquisitionManager(cfg())
    src = FakeSource(SourceMode.REPLAY, [])
    mgr.start(src)
    for i in range(100):
        src.push(FrameEvent(make_frame(link_id=f"tx1->rx{i}", mode=SourceMode.REPLAY)))
    assert len(mgr.link_statuses()) == 64


def test_concurrent_sources_threads_are_thread_safe():
    mgr = AcquisitionManager(cfg())
    src = FakeSource(SourceMode.LIVE, [L1, L2])
    mgr.start(src)

    def pump(link_id):
        for i in range(2000):
            src.push(FrameEvent(make_frame(link_id=link_id, host_mono_ns=S + i * 1_000_000, counter=i)))

    threads = [threading.Thread(target=pump, args=(lid,)) for lid in (L1, L2)]
    for th in threads:
        th.start()
    for _ in range(200):
        mgr.link_statuses()
    for th in threads:
        th.join()
    assert [s.frames_total for s in mgr.link_statuses(3 * S)] == [2000, 2000]


def test_with_synthetic_source_runs_to_finished():
    scn = SyntheticScenario(name="m", duration_s=4.0, rate_hz=25.0, links=[("tx1", "rx1"), ("tx1", "rx2")],
                            episodes=[Episode(0, 4, EMPTY)], seed=9)
    expected = sum(1 for _ in generate_frames(scn, "x"))
    mgr = AcquisitionManager(cfg())
    frames = []
    mgr.add_consumer(lambda ev: frames.append(ev) if isinstance(ev, FrameEvent) else None)
    mgr.start(SyntheticSource(scn, session_id="sim", realtime=False))
    deadline = time.monotonic() + 10
    while mgr.source_state() != SourceState.FINISHED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mgr.source_state() == SourceState.FINISHED
    assert len(frames) == expected
    assert sum(s.frames_total for s in mgr.link_statuses()) == expected
    assert all(ev.frame.source_mode == SourceMode.SIMULATION for ev in frames)
    mgr.stop()


def test_consumers_are_never_called_concurrently():
    mgr = AcquisitionManager(cfg())
    src = FakeSource(SourceMode.LIVE, [L1, L2])
    inside = [0]
    overlaps = [0]
    seen = []

    def slowish(ev):
        inside[0] += 1
        if inside[0] > 1:
            overlaps[0] += 1
        time.sleep(0.0005)
        seen.append(ev)
        inside[0] -= 1

    mgr.add_consumer(slowish)
    mgr.start(src)

    def pump(link_id):
        for i in range(200):
            src.push(FrameEvent(make_frame(link_id=link_id, host_mono_ns=S + i, counter=i)))

    threads = [threading.Thread(target=pump, args=(lid,)) for lid in (L1, L2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert overlaps[0] == 0 and len(seen) == 400
