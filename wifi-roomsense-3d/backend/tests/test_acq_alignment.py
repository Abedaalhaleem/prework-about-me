"""Clock model and window alignment on SYNTHETIC clock data (software only)."""

from __future__ import annotations

import numpy as np
import pytest

import roomsense.acquisition.alignment as alignment
from roomsense.acquisition.alignment import ClockModel, align_link_windows
from roomsense.acquisition.rollover import TimestampUnwrapper


def synthetic_clock(drift_ppm: float, seconds: float = 600.0, rate_hz: float = 25.0, seed: int = 1,
                    outlier_fraction: float = 0.05, device_start_us: int = 5_000_000):
    """Made-up clock pairs: device µs counter vs host ns with USB-like latency."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 1.0 / rate_hz)
    dev_us = device_start_us + np.round(t * 1e6).astype(np.int64)
    latency_ns = rng.exponential(1.0e6, t.size) + 0.5e6  # ~1.5 ms mean
    outliers = rng.random(t.size) < outlier_fraction
    latency_ns[outliers] += rng.uniform(20e6, 80e6, outliers.sum())  # USB batching / scheduling stalls
    host_ns = (7_000_000_000 + t * 1e9 * (1 + drift_ppm * 1e-6) + latency_ns).astype(np.int64)
    return dev_us, host_ns


@pytest.mark.parametrize("drift", [-35.0, 0.0, 42.0])
def test_drift_estimate_on_synthetic_clock_data(drift):
    cm = ClockModel()
    dev, host = synthetic_clock(drift)
    for d, h in zip(dev, host):
        cm.update(int(d), int(h))
    assert cm.drift_ppm() == pytest.approx(drift, abs=3.0)
    assert 0.1 < cm.residual_ms() < 3.0
    elapsed_s = (dev[-1] - dev[0]) / 1e6
    assert cm.offset_ms() == pytest.approx(drift * 1e-6 * elapsed_s * 1e3, abs=2.0)
    assert cm.n_points <= 600


def test_no_estimate_until_enough_history():
    cm = ClockModel()
    assert cm.drift_ppm() is None and cm.offset_ms() is None and cm.residual_ms() is None
    dev, host = synthetic_clock(10.0, seconds=3.0)
    for d, h in zip(dev, host):
        cm.update(int(d), int(h))
    assert cm.drift_ppm() is None  # span below min_span_s


def test_backwards_device_time_resets_model():
    cm = ClockModel()
    dev, host = synthetic_clock(20.0, seconds=60.0)
    for d, h in zip(dev, host):
        cm.update(int(d), int(h))
    assert cm.drift_ppm() is not None
    cm.update(1_000, int(host[-1]) + 40_000_000)  # reboot: new clock epoch
    assert cm.resets == 1 and cm.n_points == 1 and cm.drift_ppm() is None


def test_rollover_handled_by_feeding_unwrapped_timestamps():
    dev, host = synthetic_clock(25.0, seconds=300.0, device_start_us=2**32 - 100_000_000)
    raw = dev & 0xFFFFFFFF  # device counter wraps ~100 s in
    unwrap = TimestampUnwrapper()
    cm = ClockModel()
    flags_seen = []
    for r, h in zip(raw, host):
        u, flags = unwrap.update(int(r))
        flags_seen += flags
        cm.update(u, int(h))
    assert flags_seen.count("TIMESTAMP_ROLLOVER") == 1
    assert cm.drift_ppm() == pytest.approx(25.0, abs=4.0)
    assert cm.resets == 0


def test_align_link_windows():
    ok, reason = align_link_windows({"a": 1_000_000_000, "b": 1_100_000_000}, 0.25)
    assert ok and "100.0 ms" in reason
    ok, reason = align_link_windows({"a": 1_000_000_000, "b": 1_400_000_000, "c": 1_050_000_000}, 0.25)
    assert not ok and "a" in reason and "b" in reason
    assert align_link_windows({"a": 5}, 0.25) == (True, "single link; nothing to align")
    assert align_link_windows({}, 0.25)[0] is False
    assert align_link_windows({"a": 1, "b": None}, 0.25)[0] is False  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        align_link_windows({"a": 1}, 0)


def test_module_states_the_limits_plainly():
    doc = " ".join((alignment.__doc__ or "").split())
    assert "do NOT create RF phase synchronisation" in doc
    assert "angle-of-arrival" in doc and "time-of-flight" in doc
