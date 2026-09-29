"""Window quality grading. Tests software behaviour on synthetic frames only."""

from __future__ import annotations

import numpy as np
import pytest

from .proc_helpers import Scenario, generate, make_frame
from roomsense.config import ProcessingConfig
from roomsense.processing.amplitude import frame_to_amplitude
from roomsense.processing.quality import assess_quality, loss_from_counters, quality_at_least
from roomsense.processing.windows import Window, WindowRejection, build_window
from roomsense.schemas import QualityLevel

CFG = ProcessingConfig()


def _window(frames, cfg: ProcessingConfig = CFG) -> Window | WindowRejection:
    s = [x for x in (frame_to_amplitude(f) for f in frames) if x is not None]
    return build_window(s, end_ns=s[-1].t_ns, cfg=cfg, link_id="L")


def test_good_quality_clean_stream() -> None:
    w = _window(generate(Scenario(duration_s=3.0)))
    q = assess_quality(w, expected_rate_hz=25.0, cfg=CFG)
    assert q.level == QualityLevel.GOOD
    assert 23.0 < q.packet_rate_hz < 27.0
    assert q.loss_fraction == 0.0
    assert q.rssi_dbm_median == -55.0
    assert q.valid_subcarrier_fraction == 1.0
    assert q.timing_jitter_ms is not None and q.timing_jitter_ms < 5.0
    assert "SYNTHETIC" in q.flags


def test_degraded_on_low_rate() -> None:
    w = _window(generate(Scenario(duration_s=3.0, rate_hz=15.0)))
    q = assess_quality(w, expected_rate_hz=25.0, cfg=CFG)
    assert q.level == QualityLevel.DEGRADED and "LOW_PACKET_RATE" in q.flags


def test_degraded_on_counter_loss() -> None:
    w = _window(generate(Scenario(duration_s=3.0, loss_fraction=0.3, seed=4)))
    q = assess_quality(w, expected_rate_hz=None, cfg=CFG)  # rate not graded here
    assert q.loss_fraction is not None and 0.2 < q.loss_fraction < 0.5
    assert q.level == QualityLevel.DEGRADED and "HIGH_PACKET_LOSS" in q.flags
    assert "EXPECTED_RATE_UNKNOWN" in q.flags


def test_bad_on_very_low_rate() -> None:
    cfg = ProcessingConfig(min_frames_per_window=4)
    w = _window(generate(Scenario(duration_s=3.0, rate_hz=6.0)), cfg)
    q = assess_quality(w, expected_rate_hz=25.0, cfg=cfg)
    assert q.level == QualityLevel.BAD and "VERY_LOW_PACKET_RATE" in q.flags


def test_bad_on_low_valid_subcarrier_fraction() -> None:
    cfg = ProcessingConfig(min_valid_subcarrier_fraction=0.99)
    rng = np.random.default_rng(0)
    frames = [make_frame(tuple(int(v) for v in rng.integers(10, 40, 128)), t_s=i * 0.04, first_word_invalid=True)
              for i in range(60)]
    q = assess_quality(_window(frames, cfg), expected_rate_hz=25.0, cfg=cfg)
    assert q.valid_subcarrier_fraction == 51 / 52
    assert q.level == QualityLevel.BAD and "LOW_VALID_SUBCARRIER_FRACTION" in q.flags


def test_gap_in_window_degrades() -> None:
    w = _window(generate(Scenario(duration_s=4.0, gaps=[(2.5, 2.9)], jitter_s=0.0)))
    assert isinstance(w, Window) and w.gaps
    q = assess_quality(w, expected_rate_hz=25.0, cfg=CFG)
    assert q.max_gap_s is not None and q.max_gap_s > CFG.max_gap_s
    assert q.level == QualityLevel.DEGRADED and "GAP_IN_WINDOW" in q.flags


def test_unavailable_and_rejected() -> None:
    q = assess_quality(None, expected_rate_hz=25.0, cfg=CFG, rejected_frames=3)
    assert q.level == QualityLevel.UNAVAILABLE and q.rejected_frames == 3
    rej = WindowRejection("L", "INSUFFICIENT_FRAMES", "x", n_frames=5)
    q = assess_quality(rej, expected_rate_hz=25.0, cfg=CFG)
    assert q.level == QualityLevel.BAD and q.frames_in_window == 5
    assert "WINDOW_REJECTED" in q.flags and "INSUFFICIENT_FRAMES" in q.flags


def test_many_rejected_frames_degrade() -> None:
    w = _window(generate(Scenario(duration_s=3.0)))
    q = assess_quality(w, expected_rate_hz=25.0, cfg=CFG, rejected_frames=40)
    assert q.level == QualityLevel.DEGRADED and "MANY_REJECTED_FRAMES" in q.flags


def test_loss_is_never_guessed() -> None:
    assert loss_from_counters((1, 2, None, 4))[0] is None
    assert loss_from_counters((5, 6, 1, 2))[0] is None  # reset/wrap -> unknown
    assert loss_from_counters((1, 2), ("receiver", "transmitter"))[0] is None
    assert loss_from_counters((1, 2, 4, 5))[0] == pytest.approx(0.2)
    w = _window([make_frame(tuple([9] * 128), t_s=i * 0.04, counter=None) for i in range(60)])
    q = assess_quality(w, expected_rate_hz=25.0, cfg=CFG)
    assert q.loss_fraction is None


def test_quality_order() -> None:
    assert quality_at_least("GOOD", "DEGRADED")
    assert quality_at_least(QualityLevel.DEGRADED, QualityLevel.DEGRADED)
    assert not quality_at_least("BAD", "DEGRADED")
    assert not quality_at_least("UNAVAILABLE", "BAD")
