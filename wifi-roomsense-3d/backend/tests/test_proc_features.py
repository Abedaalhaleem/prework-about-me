"""Amplitude features. These tests use synthetic windows only. They check that
the code computes the documented quantities; they do not show that the
features detect people through walls."""

from __future__ import annotations

import json

import numpy as np

from .proc_helpers import LAYOUT, Scenario, generate
from roomsense.config import ProcessingConfig
from roomsense.processing.amplitude import frame_to_amplitude
from roomsense.processing.features import FEATURE_NAMES, extract_features, hampel_filter
from roomsense.processing.windows import Window, build_window

CFG = ProcessingConfig()


def _window(sc: Scenario, cfg: ProcessingConfig = CFG) -> Window:
    s = [x for x in (frame_to_amplitude(f) for f in generate(sc)) if x is not None]
    w = build_window(s, end_ns=s[-1].t_ns, cfg=cfg, link_id="L")
    assert isinstance(w, Window)
    return w


def _manual_window(t: np.ndarray, amp: np.ndarray, valid: np.ndarray | None = None) -> Window:
    n, m = amp.shape
    return Window(
        link_id="L", t_start_ns=0, t_end_ns=int(t[-1] * 1e9), t=t, amp=amp,
        valid=np.ones(m, dtype=bool) if valid is None else valid, counters=(None,) * n, rssi=(None,) * n,
        n_frames=n, gaps=[], k=np.arange(m, dtype=np.int32), occupied=np.ones(m, dtype=bool), t_frames=t,
    )


def test_names_shapes_and_quiet_vs_motion() -> None:
    quiet = extract_features(_window(Scenario(duration_s=3.0, seed=1)), CFG)
    moving = extract_features(_window(Scenario(duration_s=3.0, motion=[(0, 3)], seed=1)), CFG)
    assert quiet.names == FEATURE_NAMES == ("amp_cv_median", "amp_tdiff_median")
    assert quiet.values.shape == (2,) and quiet.is_finite()
    assert quiet.profile.shape == (LAYOUT.n_positions,)
    assert set(quiet.per_subcarrier) == set(FEATURE_NAMES)
    assert quiet.n_valid_subcarriers == 52
    # Invalid positions (guard/DC) are NaN in the profile, never zero-filled.
    assert np.all(np.isnan(quiet.profile[~LAYOUT.valid_position_mask()]))
    assert moving.value("amp_cv_median") > 3 * quiet.value("amp_cv_median")
    assert moving.value("amp_tdiff_median") > quiet.value("amp_tdiff_median")


def test_tdiff_ignores_pairs_across_long_gaps() -> None:
    # Two flat plateaus separated by a 1 s jump in time. Across the jump the
    # level changes a lot. The pair spanning the jump must be ignored.
    t = np.concatenate([np.arange(0, 1.0, 0.04), 2.0 + np.arange(0, 1.0, 0.04)])
    amp = np.where(t[:, None] < 1.5, 10.0, 20.0) * np.ones((t.size, 4))
    fv = extract_features(_manual_window(t, amp), ProcessingConfig(hampel_window=0))
    assert fv.value("amp_tdiff_median") == 0.0
    # Sanity: a small max_gap_s that also excludes normal pairs gives no tdiff.
    fv2 = extract_features(_manual_window(t, amp), ProcessingConfig(hampel_window=0, max_gap_s=0.01))
    assert np.isnan(fv2.value("amp_tdiff_median"))


def test_median_is_robust_to_a_few_noisy_subcarriers() -> None:
    rng = np.random.default_rng(0)
    t = np.arange(0, 2.0, 0.04)
    amp = 30.0 + 0.3 * rng.standard_normal((t.size, 20))
    base = extract_features(_manual_window(t, amp.copy()), ProcessingConfig(hampel_window=0))
    amp[:, :3] = 30.0 + 10.0 * rng.standard_normal((t.size, 3))  # 3 of 20 very noisy
    noisy = extract_features(_manual_window(t, amp), ProcessingConfig(hampel_window=0))
    assert noisy.value("amp_cv_median") < 1.5 * base.value("amp_cv_median")


def test_hampel_removes_isolated_spike() -> None:
    t = np.arange(0, 2.0, 0.04)
    amp = np.full((t.size, 3), 30.0) + 0.2 * np.sin(np.arange(t.size))[:, None]
    amp[20, 1] = 90.0
    filtered, n = hampel_filter(amp, 5, 3.0)
    assert n >= 1 and abs(filtered[20, 1] - 30.0) < 1.0
    no_filter = extract_features(_manual_window(t, amp), ProcessingConfig(hampel_window=0))
    with_filter = extract_features(_manual_window(t, amp), ProcessingConfig(hampel_window=5))
    assert with_filter.per_subcarrier["amp_cv_median"][1] < 0.2 * no_filter.per_subcarrier["amp_cv_median"][1]
    assert with_filter.n_outliers_replaced >= 1


def test_hampel_keeps_nan_and_disabled_is_identity() -> None:
    x = np.array([[1.0, np.nan], [1.0, 2.0], [50.0, 2.0], [1.0, 2.0], [1.0, np.nan]])
    y, _ = hampel_filter(x, 2, 3.0)
    assert np.isnan(y[0, 1]) and np.isnan(y[4, 1])
    y0, n0 = hampel_filter(x, 0, 3.0)
    assert n0 == 0 and np.array_equal(np.isnan(y0), np.isnan(x))


def test_irregular_sampling_is_tolerated() -> None:
    fv = extract_features(_window(Scenario(duration_s=3.0, jitter_s=0.015, loss_fraction=0.2, seed=9)), CFG)
    assert fv.is_finite()


def test_no_valid_subcarriers_gives_nan_not_zero() -> None:
    t = np.arange(0, 2.0, 0.04)
    amp = np.full((t.size, 4), np.nan)
    fv = extract_features(_manual_window(t, amp, valid=np.zeros(4, dtype=bool)), CFG)
    assert not fv.is_finite() and fv.n_valid_subcarriers == 0


def test_feature_vector_to_dict_is_json_safe() -> None:
    fv = extract_features(_window(Scenario(duration_s=3.0)), CFG)
    d = fv.to_dict()
    json.dumps(d, allow_nan=False)
    assert d["names"] == list(FEATURE_NAMES)
    assert d["profile"][0] is None  # DC position -> null, not NaN
    assert not fv.values.flags.writeable
