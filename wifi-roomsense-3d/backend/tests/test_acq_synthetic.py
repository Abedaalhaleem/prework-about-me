"""Tests of the SIMULATOR (roomsense.acquisition.synthetic).

These check that simulated data is deterministic, correctly labelled as
synthetic, shaped like a documented CSI layout, and that the toy model's
episodes have the intended qualitative effect. They test the simulator
itself; nothing here is evidence about real Wi-Fi sensing.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from roomsense.acquisition.base import EndOfStream, FrameEvent, LinkEvent
from roomsense.acquisition.synthetic import (
    DISCONNECT,
    EMPTY,
    MOTION,
    PACKET_LOSS,
    STILL_PERSON,
    SYNTHETIC_EPOCH_MONOTONIC_NS,
    SYNTHETIC_ZONES,
    Episode,
    SyntheticScenario,
    SyntheticSource,
    builtin_scenarios,
    generate_frames,
    with_seed,
    zone_session_scenario,
)
from roomsense.csi_layouts import extract_complex, layout_from_id
from roomsense.schemas import InputFormat, SourceMode
from tests.acq_helpers import EventCollector

LAYOUT_ID = "classic.lltf_only.sec_none.total128.LLTF64"


def small(episodes, *, duration=10.0, seed=5, links=(("tx1", "rx1"),), **params) -> SyntheticScenario:
    return SyntheticScenario(name="t", duration_s=duration, rate_hz=25.0, links=list(links), episodes=episodes,
                             seed=seed, params=params)


def rel_t(frames, f) -> float:
    """Seconds since the fixed synthetic epoch used by generate_frames()."""
    return (f.host_arrival_monotonic_ns - SYNTHETIC_EPOCH_MONOTONIC_NS) / 1e9


def test_deterministic_for_a_seed_and_different_across_seeds():
    scn = small([Episode(0, 5, EMPTY), Episode(5, 10, MOTION)])
    a = [f.to_record() for f in generate_frames(scn, "s")]
    b = [f.to_record() for f in generate_frames(scn, "s")]
    assert a == b and len(a) == 250
    c = [f.raw_csi for f in generate_frames(with_seed(scn, 6), "s")]
    assert c != [r["raw_csi"] for r in a]


def test_every_frame_is_labelled_synthetic():
    frames = list(generate_frames(small([Episode(0, 10, MOTION)]), "sim-session"))
    for f in frames:
        assert f.source_mode == SourceMode.SIMULATION
        assert f.input_format == InputFormat.SYNTHETIC_V1
        assert "SYNTHETIC" in f.quality_flags
        assert f.device.identity_source == "synthetic" and f.device.chip == "synthetic"
        assert f.session_id == "sim-session"
        assert f.transmitter_mac is None  # the simulator does not pretend to be a real device
        assert f.values_are_gain_compensated is False


def test_layout_shape_int8_range_and_zero_non_occupied_positions():
    frames = list(generate_frames(small([Episode(0, 10, MOTION)]), "s"))
    layout = layout_from_id(LAYOUT_ID)
    assert layout is not None
    mask = layout.valid_position_mask()
    raw = np.asarray([f.raw_csi for f in frames])
    assert raw.shape == (len(frames), 128)
    assert raw.min() >= -128 and raw.max() <= 127
    unoccupied = np.flatnonzero(~mask)
    assert np.all(raw[:, 2 * unoccupied] == 0) and np.all(raw[:, 2 * unoccupied + 1] == 0)
    for f in frames[:5]:
        assert f.layout_id == LAYOUT_ID and f.csi_len == 128
        assert f.valid_subcarriers == tuple(int(k) for k in layout.k_indices()[mask])
        assert len(f.valid_subcarriers) == 52
        k, csi, valid = extract_complex(f.raw_csi, layout, f.first_word_invalid)
        assert np.all(np.abs(csi[valid]) > 0)


def test_counters_timestamps_and_host_times():
    frames = list(generate_frames(small([Episode(0, 10, EMPTY)]), "s", start_unix_ns=10**18, start_monotonic_ns=10**9))
    counters = [f.frame_counter_unwrapped for f in frames]
    assert counters == list(range(len(frames)))
    dev = [f.device_timestamp_unwrapped_us for f in frames]
    assert all(b > a for a, b in zip(dev, dev[1:]))
    host = [f.host_arrival_monotonic_ns for f in frames]
    assert all(b > a for a, b in zip(host, host[1:]))
    assert frames[0].host_arrival_unix_ns - frames[0].host_arrival_monotonic_ns == 10**18 - 10**9
    period_ms = np.diff(host) / 1e6
    assert 30 < np.median(period_ms) < 50


def test_device_clock_rollover_is_unwrapped_and_flagged():
    scn = small([Episode(0, 4, EMPTY)], duration=4.0, device_clock_start_us=2**32 - 1_000_000)
    frames = list(generate_frames(scn, "s"))
    flagged = [f for f in frames if "TIMESTAMP_ROLLOVER" in f.quality_flags]
    assert len(flagged) == 1
    dev = [f.device_timestamp_unwrapped_us for f in frames]
    assert all(b > a for a, b in zip(dev, dev[1:]))
    assert max(f.device_timestamp_us for f in frames) < 2**32


def test_packet_loss_creates_counter_gaps():
    frames = list(generate_frames(small([Episode(0, 10, PACKET_LOSS, {"fraction": 0.4})]), "s"))
    assert 100 < len(frames) < 200  # ~60% of 250
    assert sum("COUNTER_GAP" in f.quality_flags for f in frames) > 20


def test_disconnect_removes_frames_for_the_interval():
    scn = small([Episode(0, 3, EMPTY), Episode(3, 6, DISCONNECT), Episode(6, 10, EMPTY)])
    frames = list(generate_frames(scn, "s"))
    ts = [rel_t(frames, f) for f in frames]
    assert not any(3.01 < t < 6.0 for t in ts)
    assert any(t > 6.0 for t in ts)
    after = next(f for f, t in zip(frames, ts) if t > 6.0)
    assert "COUNTER_GAP" in after.quality_flags


def test_disconnect_can_target_one_link():
    scn = small([Episode(2, 4, DISCONNECT, {"links": ["tx1->rx2"]})], links=(("tx1", "rx1"), ("tx1", "rx2")))
    frames = list(generate_frames(scn, "s"))
    in_gap = [f.link_id for f in frames if 2.1 < rel_t(frames, f) < 3.9]
    assert in_gap and set(in_gap) == {"tx1->rx1"}


def _temporal_std(frames, a, b, link="tx1->rx1"):
    layout = layout_from_id(LAYOUT_ID)
    rows = []
    for f in frames:
        t = rel_t(frames, f)
        if a <= t < b and f.link_id == link:
            _, csi, valid = extract_complex(f.raw_csi, layout, False)
            amp = np.abs(csi[valid])
            rows.append(amp / amp.mean())
    arr = np.asarray(rows)
    return float(np.median(arr.std(axis=0)))


def test_toy_model_episodes_have_the_intended_qualitative_effect():
    """Simulator sanity only: MOTION varies more than EMPTY, STILL_PERSON does not."""
    scn = small(
        [Episode(0, 20, EMPTY), Episode(20, 40, MOTION), Episode(40, 60, STILL_PERSON, {"position": (2.0, 2.0)})],
        duration=60.0,
    )
    frames = list(generate_frames(scn, "s"))
    empty = _temporal_std(frames, 1, 19)
    motion = _temporal_std(frames, 21, 39)
    still = _temporal_std(frames, 41, 59)
    assert motion > 2.0 * empty
    assert still < 1.5 * empty


def test_builtin_scenarios_match_the_documented_timelines():
    scns = builtin_scenarios()
    assert set(scns) >= {"demo_walk", "quiet_only", "disconnect", "zones_3rx"}
    demo = scns["demo_walk"]
    assert demo.link_ids() == ["tx1->rx1"] and demo.duration_s == 300
    timeline = [(e.t_start_s, e.t_end_s, e.kind) for e in demo.episodes]
    assert timeline == [
        (0, 120, "EMPTY"), (120, 150, "MOTION"), (150, 180, "EMPTY"), (180, 210, "STILL_PERSON"),
        (215, 220, "DOOR"), (225, 245, "MOTION"), (250, 260, "DISCONNECT"), (260, 300, "EMPTY"),
    ]
    assert scns["quiet_only"].duration_s == 180 and [e.kind for e in scns["quiet_only"].episodes] == ["EMPTY"]
    assert [e.kind for e in scns["disconnect"].episodes] == ["EMPTY", "DISCONNECT"]
    z = scns["zones_3rx"]
    assert z.link_ids() == ["tx1->rx1", "tx1->rx2", "tx1->rx3"]
    assert z.episodes[0].kind == "EMPTY" and z.episodes[0].t_end_s >= 60
    assert [e.params.get("zone") for e in z.episodes if e.kind == "MOTION"] == ["A", "B", "C"]
    assert any(e.kind == "NEAR_SENSOR_OUTSIDE" for e in z.episodes)
    for s in scns.values():
        assert "SIMULATED" in s.description


def test_ground_truth_labels_and_implicit_gaps():
    gt = builtin_scenarios()["demo_walk"].ground_truth()
    assert all(g["synthetic"] for g in gt)
    covered = sum(g["t_end_s"] - g["t_start_s"] for g in gt)
    assert covered == pytest.approx(300.0)
    still = next(g for g in gt if g["kind"] == "STILL_PERSON")
    assert still["motion_expected"] is False and "limitation" in still["note"].lower()
    assert next(g for g in gt if g["kind"] == "DISCONNECT")["motion_expected"] is None
    implicit = [g for g in gt if g["implicit"]]
    assert [(g["t_start_s"], g["t_end_s"]) for g in implicit] == [(210, 215), (220, 225), (245, 250)]
    zgt = builtin_scenarios()["zones_3rx"].ground_truth()
    assert {g["label"] for g in zgt} >= {"A", "B", "C", "EMPTY", "OUTSIDE_TARGET_ROOM"}


def test_zone_session_scenarios_share_one_environment():
    for label in list(SYNTHETIC_ZONES) + ["EMPTY", "OUTSIDE_TARGET_ROOM"]:
        s = zone_session_scenario(label, seed=1, duration_s=5.0)
        assert s.ground_truth()[0]["label"] == label
        assert len(s.links) == 3
    with pytest.raises(ValueError):
        zone_session_scenario("KITCHEN", seed=1)
    # Same simulated room across seeds: the mean amplitude profile of an
    # EMPTY session is (nearly) identical; the noise realisation differs.
    layout = layout_from_id(LAYOUT_ID)

    def profile(seed):
        fr = [f for f in generate_frames(zone_session_scenario("EMPTY", seed=seed, duration_s=8.0), "s")
              if f.link_id == "tx1->rx1"]
        amps = [np.abs(extract_complex(f.raw_csi, layout, False)[1]) for f in fr]
        return np.mean(amps, axis=0), fr[0].raw_csi

    p1, r1 = profile(1)
    p2, r2 = profile(2)
    assert np.corrcoef(p1, p2)[0, 1] > 0.99
    assert r1 != r2


def test_scenario_validation():
    with pytest.raises(ValueError):
        Episode(5, 5, EMPTY)
    with pytest.raises(ValueError):
        Episode(0, 5, "DANCING")
    with pytest.raises(ValueError):
        small([Episode(0, 20, EMPTY)], duration=10.0)
    with pytest.raises(ValueError):
        SyntheticScenario(name="x", duration_s=10, rate_hz=25, links=[], episodes=[], seed=1)


def test_source_non_realtime_emits_frames_link_events_and_end():
    scn = small([Episode(0, 2, EMPTY), Episode(2, 4, DISCONNECT), Episode(4, 6, EMPTY)], duration=6.0)
    src = SyntheticSource(scn, session_id="sim", realtime=False)
    col = EventCollector()
    src.start(col)
    assert col.wait_for(lambda ev: any(isinstance(e, EndOfStream) for e in ev), 10)
    src.stop(1.0)
    ev = col.snapshot()
    kinds = [e.kind for e in ev if isinstance(e, LinkEvent)]
    assert kinds == ["CONNECTED", "DISCONNECTED", "CONNECTED"]
    assert ev[-1].reason == "END_OF_SCENARIO"
    frames = [e.frame for e in ev if isinstance(e, FrameEvent)]
    offline = list(generate_frames(scn, "sim"))
    # Same simulated content as offline generation; only the host epoch differs.
    assert [(f.raw_csi, f.frame_counter, f.device_timestamp_us) for f in frames] == [
        (f.raw_csi, f.frame_counter, f.device_timestamp_us) for f in offline
    ]
    start_mono = next(e for e in ev if isinstance(e, LinkEvent)).host_monotonic_ns
    assert frames[0].host_arrival_monotonic_ns - start_mono == (
        offline[0].host_arrival_monotonic_ns - SYNTHETIC_EPOCH_MONOTONIC_NS
    )
    assert all(f.source_mode == SourceMode.SIMULATION for f in frames)
    d = src.describe()
    assert d["simulated"] is True and "SIMULATED" in d["banner"] and d["mode"] == "SIMULATION"


def test_source_realtime_respects_speed_and_stop():
    scn = small([Episode(0, 2, MOTION)], duration=2.0)
    src = SyntheticSource(scn, session_id="sim", realtime=True, speed=20.0)
    col = EventCollector()
    t0 = time.monotonic()
    src.start(col)
    assert col.wait_for(lambda ev: any(isinstance(e, EndOfStream) for e in ev), 5)
    elapsed = time.monotonic() - t0
    assert 0.05 < elapsed < 1.5
    frames = [e.frame for e in col.snapshot() if isinstance(e, FrameEvent)]
    span_s = (frames[-1].host_arrival_monotonic_ns - frames[0].host_arrival_monotonic_ns) / 1e9
    assert span_s == pytest.approx(2.0 / 20.0, rel=0.1)  # host stamps follow replay-style time
    slow = SyntheticSource(small([Episode(0, 10, EMPTY)]), session_id="sim", realtime=True, speed=1.0)
    col2 = EventCollector()
    slow.start(col2)
    assert col2.wait_for(lambda ev: len(ev) >= 3, 5)
    t1 = time.monotonic()
    slow.stop(2.0)
    assert time.monotonic() - t1 < 0.5
    assert not any(isinstance(e, EndOfStream) for e in col2.snapshot())
    with pytest.raises(ValueError):
        SyntheticSource(scn, session_id="x", speed=0)


def test_unknown_zone_is_rejected():
    with pytest.raises(ValueError):
        small([Episode(0, 5, MOTION, {"zone": "Z"})])
