"""Gap-aware windowing. Software behaviour on synthetic frames; no claims about
sensing accuracy."""

from __future__ import annotations

import numpy as np
import pytest

from .proc_helpers import Scenario, generate, make_frame, t_ns
from roomsense.config import ProcessingConfig
from roomsense.csi_layouts import resolve_layout
from roomsense.processing.amplitude import frame_to_amplitude
from roomsense.processing.windows import (
    INSUFFICIENT_CONTIGUOUS_SPAN,
    INSUFFICIENT_FRAMES,
    LAYOUT_CHANGED,
    NO_DATA,
    TIME_BASE_CHANGED,
    Window,
    WindowRejection,
    build_window,
)

CFG = ProcessingConfig()  # window 2 s, max_gap 0.25 s, min 20 frames


def _samples(sc: Scenario):
    return [s for s in (frame_to_amplitude(f) for f in generate(sc)) if s is not None]


def test_contiguous_window() -> None:
    s = _samples(Scenario(duration_s=4.0, jitter_s=0.0))
    w = build_window(s, end_ns=s[-1].t_ns, cfg=CFG, link_id="L")
    assert isinstance(w, Window)
    assert 49 <= w.n_frames <= 51
    assert w.gaps == []
    assert w.amp.shape == (w.n_frames, 64)
    assert w.valid.sum() == 52
    assert w.t[0] == 0.0 and w.t[-1] == pytest.approx(w.span_s)
    assert not w.resampled
    assert w.t_unix_start_ns is not None and w.t_unix_end_ns is not None


def test_two_second_hole_is_never_bridged() -> None:
    # Frames 0..3 s, nothing 3..5 s, then frames again.
    s = _samples(Scenario(duration_s=8.0, gaps=[(3.0, 5.0)], jitter_s=0.0))
    after = [x for x in s if x.t_ns >= t_ns(5.0)]

    # Shortly after the hole: the newest segment is too short -> rejection.
    end = after[7].t_ns  # 8 frames after the gap
    w = build_window(s, end_ns=end, cfg=CFG, link_id="L")
    assert isinstance(w, WindowRejection)
    assert w.reason == INSUFFICIENT_FRAMES
    assert len(w.gaps) == 1
    g0, g1 = w.gaps[0]
    assert (g1 - g0) / 1e9 == pytest.approx(2.04, abs=0.05)

    # Later: only the post-gap segment is used, with real samples only.
    end = after[30].t_ns  # 1.2 s after the gap
    w = build_window(s, end_ns=end, cfg=CFG, link_id="L")
    assert isinstance(w, Window)
    assert w.t_start_ns >= t_ns(5.0)
    assert w.n_frames == 31
    assert len(w.gaps) == 1  # the hole that began before the window is still reported
    real_times = {x.t_ns for x in after[:31]}
    assert {w.t_start_ns + int(round(v * 1e9)) for v in w.t_frames} <= {t + d for t in real_times for d in (-1, 0, 1)}


def test_short_gap_inside_window_keeps_only_newest_segment() -> None:
    s = _samples(Scenario(duration_s=6.0, gaps=[(3.0, 3.5)], jitter_s=0.0))
    end = [x for x in s if x.t_ns <= t_ns(4.8) + 1][-1].t_ns
    w = build_window(s, end_ns=end, cfg=CFG, link_id="L")
    assert isinstance(w, Window)
    assert w.t_start_ns == t_ns(3.52)  # first frame on the 40 ms grid after the gap
    assert w.frames_discarded_before_gap == 5  # 2.80, 2.84, ..., 2.96 s
    assert len(w.gaps) == 1 and w.gaps[0] == (t_ns(2.96), t_ns(3.52))
    assert np.all(np.diff(w.t) <= CFG.max_gap_s)


def test_resampling_stays_inside_newest_segment() -> None:
    cfg = ProcessingConfig(resample_hz=20.0)
    s = _samples(Scenario(duration_s=8.0, gaps=[(3.0, 5.0)], jitter_s=0.01))
    after = [x for x in s if x.t_ns >= t_ns(5.0)]
    end = after[35].t_ns
    w = build_window(s, end_ns=end, cfg=cfg, link_id="L")
    assert isinstance(w, Window) and w.resampled
    assert w.t_start_ns >= t_ns(4.98)  # first post-gap frame (+-10 ms jitter)
    assert w.t[0] == 0.0 and w.t[-1] <= w.span_s + 1e-9
    assert np.allclose(np.diff(w.t), 0.05)
    # Quality inputs keep the raw arrival times.
    assert w.t_frames.size == w.n_frames >= 36


def test_insufficient_span() -> None:
    s = _samples(Scenario(duration_s=0.8, rate_hz=40.0, jitter_s=0.0))
    w = build_window(s, end_ns=s[-1].t_ns, cfg=CFG, link_id="L")
    assert isinstance(w, WindowRejection)
    assert w.reason == INSUFFICIENT_CONTIGUOUS_SPAN


def test_no_data() -> None:
    s = _samples(Scenario(duration_s=2.0))
    w = build_window(s, end_ns=s[-1].t_ns + int(10e9), cfg=CFG, link_id="L")
    assert isinstance(w, WindowRejection) and w.reason == NO_DATA
    assert isinstance(build_window([], end_ns=0, cfg=CFG, link_id="L"), WindowRejection)


def test_layout_change_rejected() -> None:
    s = _samples(Scenario(duration_s=3.0, jitter_s=0.0))
    other = resolve_layout(family="classic", total_values=256, ltf_config="lltf_htltf_stbc",
                           secondary_channel=0, sig_mode=1, cwb=0, stbc=0)
    assert other is not None
    extra = frame_to_amplitude(make_frame(tuple([9] * 256), t_s=3.0, layout_id=other.layout_id))
    assert extra is not None
    w = build_window(s + [extra], end_ns=extra.t_ns, cfg=CFG, link_id="L")
    assert isinstance(w, WindowRejection) and w.reason == LAYOUT_CHANGED


def test_time_base_mix_rejected() -> None:
    s = _samples(Scenario(duration_s=3.0, jitter_s=0.0))
    dev = frame_to_amplitude(make_frame(tuple([9] * 128), t_s=None, device_timestamp_us=s[-1].t_ns // 1000))
    assert dev is not None
    w = build_window(s + [dev], end_ns=s[-1].t_ns, cfg=CFG, link_id="L")
    assert isinstance(w, WindowRejection) and w.reason == TIME_BASE_CHANGED


def test_subcarrier_valid_needs_90_percent_of_frames() -> None:
    rng = np.random.default_rng(3)

    def run(fraction_invalid: float) -> bool:
        frames = []
        for i in range(50):
            raw = tuple(int(v) for v in rng.integers(10, 40, size=128))
            frames.append(make_frame(raw, t_s=i * 0.04, first_word_invalid=(i < fraction_invalid * 50)))
        s = [frame_to_amplitude(f) for f in frames]
        w = build_window(s, end_ns=s[-1].t_ns, cfg=CFG, link_id="L")
        assert isinstance(w, Window)
        return bool(w.valid[1])  # position 1 is k=+1

    assert run(0.06) is True
    assert run(0.20) is False
