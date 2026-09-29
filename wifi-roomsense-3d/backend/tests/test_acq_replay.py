"""ReplaySource tests on recordings built from SYNTHETIC frames.

They check timing preservation, re-stamping and labelling of replayed data;
they say nothing about sensing accuracy.
"""

from __future__ import annotations

import dataclasses
import gzip
import time
from pathlib import Path

import pytest

from roomsense.acquisition.base import EndOfStream, FrameEvent, LinkEvent
from roomsense.acquisition.replay_source import ReplaySource, original_is_synthetic
from roomsense.acquisition.synthetic import DISCONNECT, EMPTY, Episode, SyntheticScenario, generate_frames
from roomsense.recording_format import RecordingFormatError, RecordingWriter
from roomsense.schemas import CsiFrame, SourceMode
from tests.acq_helpers import EventCollector, make_frame

SPEED = 50.0


def _scenario() -> SyntheticScenario:
    return SyntheticScenario(
        name="replay_test",
        duration_s=6.0,
        rate_hz=25.0,
        links=[("tx1", "rx1")],
        seed=3,
        episodes=[Episode(0, 2, EMPTY), Episode(2, 4, DISCONNECT), Episode(4, 6, EMPTY)],
    )


def _header(source_mode: str, link_ids: list[str], **kw) -> dict:
    h = {
        "recording_id": "rec-001",
        "session_id": "orig-session",
        "source_mode": source_mode,
        "created_at_unix_ns": 1_700_000_000_000_000_000,
        "label": "test",
        "scenario": None,
        "link_ids": link_ids,
        "consent_id": None,
        "config_version": "cfg-test",
        "notes": None,
    }
    h.update(kw)
    return h


def write_recording(path: Path, frames: list[CsiFrame], *, source_mode: str, events=(), footer=True) -> Path:
    w = RecordingWriter(path, _header(source_mode, sorted({f.link_id for f in frames})))
    pending = sorted(events, key=lambda e: e["host_monotonic_ns"])
    for f in frames:
        while pending and pending[0]["host_monotonic_ns"] <= f.host_arrival_monotonic_ns:
            w.write_event(pending.pop(0))
        w.write_frame(f)
    for e in pending:
        w.write_event(e)
    if footer:
        w.close({"ended_at_unix_ns": 1, "status": "COMPLETED", "reason": None})
    else:
        w._gz.flush()  # interrupted recording: no footer
        w._gz.close()
        w._raw.close()
    return path


@pytest.fixture
def synthetic_recording(tmp_path):
    frames = list(generate_frames(_scenario(), "orig-session"))
    # Place the recorded link events strictly inside the simulated 2..4 s gap.
    gap = max(zip(frames, frames[1:]), key=lambda ab: ab[1].host_arrival_monotonic_ns - ab[0].host_arrival_monotonic_ns)
    before, after = gap[0].host_arrival_monotonic_ns, gap[1].host_arrival_monotonic_ns
    events = [
        {"kind": "DISCONNECTED", "link_id": "tx1->rx1", "receiver_id": "rx1", "detail": "unplugged",
         "host_monotonic_ns": before + 10_000_000, "host_unix_ns": None},
        {"kind": "CONNECTED", "link_id": "tx1->rx1", "receiver_id": "rx1", "detail": "",
         "host_monotonic_ns": after - 10_000_000, "host_unix_ns": None},
    ]
    return write_recording(tmp_path / "sim.jsonl.gz", frames, source_mode="SIMULATION", events=events), frames


def replay(path, speed=SPEED, timeout=10.0):
    src = ReplaySource(path, session_id="replay-session", speed=speed)
    col = EventCollector()
    t0 = time.monotonic()
    src.start(col)
    assert col.wait_for(lambda ev: any(isinstance(e, EndOfStream) for e in ev), timeout)
    elapsed = time.monotonic() - t0
    src.stop(1.0)
    return src, col.snapshot(), elapsed


def test_replay_round_trip_preserves_gaps_and_flags(synthetic_recording):
    path, original = synthetic_recording
    src, events, elapsed = replay(path)
    out = [e.frame for e in events if isinstance(e, FrameEvent)]
    assert len(out) == len(original)
    for o, r in zip(original, out):
        assert r.source_mode == SourceMode.REPLAY
        assert r.session_id == "replay-session"
        assert "REPLAYED" in r.quality_flags
        assert "SYNTHETIC" in r.quality_flags  # original flags are kept
        assert r.recorded_host_arrival_unix_ns == o.host_arrival_unix_ns
        assert r.raw_csi == o.raw_csi and r.frame_counter == o.frame_counter
        assert r.device_timestamp_us == o.device_timestamp_us
    # Re-stamped spacing == original spacing / speed (to rounding).
    orig_dt = [(b.host_arrival_monotonic_ns - a.host_arrival_monotonic_ns) / SPEED for a, b in zip(original, original[1:])]
    new_dt = [b.host_arrival_monotonic_ns - a.host_arrival_monotonic_ns for a, b in zip(out, out[1:])]
    assert max(abs(x - y) for x, y in zip(orig_dt, new_dt)) <= 2
    # The 2 s disconnect gap stays a gap (2 s / 50 = 40 ms), both in the
    # stamps and in wall-clock emission.
    biggest = max(new_dt)
    assert 1.9e9 / SPEED <= biggest <= 2.2e9 / SPEED
    total_orig = (original[-1].host_arrival_monotonic_ns - original[0].host_arrival_monotonic_ns) / 1e9
    assert elapsed >= 0.8 * total_orig / SPEED
    # Re-stamped host times are on the replay clock (close to now).
    assert abs(out[-1].host_arrival_monotonic_ns - time.monotonic_ns()) < 5e9
    assert isinstance(events[-1], EndOfStream) and events[-1].reason == "END_OF_RECORDING"
    assert src.finished and src.frames_emitted == len(original)


def test_recorded_link_events_are_reemitted_in_order(synthetic_recording):
    path, _ = synthetic_recording
    _, events, _ = replay(path)
    seq = [e.kind if isinstance(e, LinkEvent) else "F" for e in events if not isinstance(e, EndOfStream)]
    i_disc, i_conn = seq.index("DISCONNECTED"), seq.index("CONNECTED")
    assert i_disc < i_conn
    assert "F" in seq[:i_disc] and "F" in seq[i_conn:]
    assert "F" not in seq[i_disc + 1 : i_conn]
    ev = next(e for e in events if isinstance(e, LinkEvent) and e.kind == "DISCONNECTED")
    assert ev.data["replayed"] is True and ev.detail == "unplugged"


def test_describe_and_original_is_synthetic(synthetic_recording):
    path, _ = synthetic_recording
    src = ReplaySource(path, session_id="replay-session", speed=2.0)
    d = src.describe()
    assert d["mode"] == "REPLAY"
    assert d["recording_id"] == "rec-001"
    assert d["original_session_id"] == "orig-session"
    assert d["original_source_mode"] == "SIMULATION"
    assert d["simulated"] is True
    assert src.link_ids() == ["tx1->rx1"]
    assert original_is_synthetic(path) is True


def test_live_recording_is_not_synthetic_and_replay_of_replay_keeps_origin(tmp_path):
    live = [make_frame(host_mono_ns=1_000_000_000 + i * 40_000_000, device_us=i * 40_000, counter=i) for i in range(20)]
    p = write_recording(tmp_path / "live.jsonl.gz", live, source_mode="LIVE")
    assert original_is_synthetic(p) is False
    src, events, _ = replay(p)
    assert src.describe()["simulated"] is False
    # A recording OF a replay of simulated data: header says REPLAY, frames say SYNTHETIC.
    sim = list(generate_frames(_scenario(), "x"))[:10]
    replayed = [dataclasses.replace(f, source_mode=SourceMode.REPLAY, quality_flags=f.quality_flags + ("REPLAYED",)) for f in sim]
    p2 = write_recording(tmp_path / "rr.jsonl.gz", replayed, source_mode="REPLAY")
    assert original_is_synthetic(p2) is True
    before_start = ReplaySource(p2, session_id="x").describe()
    assert before_start["simulated"] is True and before_start["original_source_mode"] == "REPLAY"
    _, ev2, _ = replay(p2)
    fr = [e.frame for e in ev2 if isinstance(e, FrameEvent)]
    assert all(f.quality_flags.count("REPLAYED") == 1 for f in fr)


def test_missing_host_timestamps_use_device_time_and_are_flagged(tmp_path):
    frames = [
        make_frame(host_mono_ns=None, device_us=10_000_000 + i * 100_000, counter=i) for i in range(10)
    ]
    frames[5] = make_frame(host_mono_ns=None, device_us=10_000_000 + 5 * 100_000 + 1_000_000, counter=5)
    for i in range(6, 10):
        frames[i] = make_frame(host_mono_ns=None, device_us=10_000_000 + i * 100_000 + 1_000_000, counter=i)
    p = write_recording(tmp_path / "nohost.jsonl.gz", frames, source_mode="LIVE")
    _, events, _ = replay(p, speed=10.0)
    out = [e.frame for e in events if isinstance(e, FrameEvent)]
    assert all("HOST_TIMESTAMP_UNAVAILABLE" in f.quality_flags for f in out)
    dt = [(b.host_arrival_monotonic_ns - a.host_arrival_monotonic_ns) / 1e6 for a, b in zip(out, out[1:])]
    assert dt[0] == pytest.approx(10.0, abs=0.01)  # 100 ms device spacing / speed 10
    assert dt[4] == pytest.approx(110.0, abs=0.01)  # the 1.1 s device gap is preserved


def test_interrupted_recording_without_footer_still_replays(tmp_path):
    frames = list(generate_frames(_scenario(), "o"))[:15]
    p = write_recording(tmp_path / "cut.jsonl.gz", frames, source_mode="SIMULATION", footer=False)
    _, events, _ = replay(p)
    assert sum(isinstance(e, FrameEvent) for e in events) == 15
    assert events[-1].reason == "END_OF_RECORDING"


def test_bad_files_are_rejected_up_front(tmp_path):
    bad = tmp_path / "bad.jsonl.gz"
    with gzip.open(bad, "wb") as fh:
        fh.write(b'{"type":"header","format":"something-else"}\n')
    with pytest.raises(RecordingFormatError):
        ReplaySource(bad, session_id="s")
    with pytest.raises(ValueError):
        ReplaySource(bad, session_id="s", speed=0)


def test_corrupt_record_mid_file_ends_with_error(tmp_path):
    p = tmp_path / "corrupt.jsonl.gz"
    frames = list(generate_frames(_scenario(), "o"))[:3]
    write_recording(p, frames, source_mode="SIMULATION")
    with gzip.open(p, "rb") as fh:
        lines = fh.read().splitlines(keepends=True)
    with gzip.open(p, "wb") as fh:
        fh.writelines(lines[:2] + [b"{not json\n"] + lines[2:])
    _, events, _ = replay(p)
    assert isinstance(events[-1], EndOfStream) and events[-1].reason.startswith("ERROR")


def test_stop_mid_replay_is_quick_and_emits_no_end(synthetic_recording):
    path, _ = synthetic_recording
    src = ReplaySource(path, session_id="r", speed=1.0)
    col = EventCollector()
    src.start(col)
    assert col.wait_for(lambda ev: len(ev) >= 3, 5)
    t0 = time.monotonic()
    src.stop(2.0)
    assert time.monotonic() - t0 < 0.5
    assert not any(isinstance(e, EndOfStream) for e in col.snapshot())
    assert not src.finished
