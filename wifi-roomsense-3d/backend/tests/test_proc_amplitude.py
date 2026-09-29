"""Frame -> amplitude conversion (software behaviour on synthetic frames only;
says nothing about sensing accuracy)."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from .proc_helpers import LAYOUT, LAYOUT_ID, make_frame, t_ns
from roomsense.config import AppConfig, ProcessingConfig
from roomsense.csi_layouts import extract_complex
from roomsense.processing.amplitude import (
    FLAG_DEVICE_TIME_BASE,
    TIME_BASE_DEVICE,
    TIME_BASE_HOST,
    AmplitudeSample,
    FrameRejection,
    convert_frame,
    frame_to_amplitude,
)
from roomsense.processing.features import extract_features
from roomsense.processing.pipeline import ProcessingEngine
from roomsense.schemas import QualityFlag, SourceMode


def _raw(imag: np.ndarray, real: np.ndarray) -> tuple[int, ...]:
    out = np.empty(2 * imag.size, dtype=int)
    out[0::2] = imag
    out[1::2] = real
    return tuple(int(v) for v in out)


def test_imag_first_real_second_pairing() -> None:
    n = LAYOUT.n_positions
    p = np.arange(n)
    imag = p % 5  # differs between neighbouring positions
    real = np.full(n, 10)
    frame = make_frame(_raw(imag, real), t_s=0.0)

    # The shared helper puts the imaginary part first.
    _, csi, _ = extract_complex(frame.raw_csi, LAYOUT, False)
    assert np.array_equal(csi.imag.astype(int), imag)
    assert np.array_equal(csi.real.astype(int), real)

    s = frame_to_amplitude(frame)
    assert isinstance(s, AmplitudeSample)
    expected = np.sqrt(real.astype(float) ** 2 + imag.astype(float) ** 2)
    valid = LAYOUT.valid_position_mask()
    assert np.allclose(s.amp[valid], expected[valid], atol=1e-5)
    # A pairing shifted by one value (real, next imag) would give a different amplitude.
    shifted = np.sqrt(real.astype(float) ** 2 + np.roll(imag, -1).astype(float) ** 2)
    assert not np.allclose(s.amp[valid], shifted[valid])


def test_invalid_positions_are_nan_and_masked() -> None:
    rng = np.random.default_rng(0)
    raw = tuple(int(v) for v in rng.integers(-40, 40, size=128))
    s = frame_to_amplitude(make_frame(raw, t_s=0.0))
    assert s is not None
    occupied = LAYOUT.valid_position_mask()
    assert np.array_equal(s.valid, occupied)
    assert np.all(np.isnan(s.amp[~occupied]))
    assert np.all(np.isfinite(s.amp[occupied]))
    assert s.layout_id == LAYOUT_ID
    assert not s.amp.flags.writeable


def test_first_word_invalid_masks_first_two_positions() -> None:
    raw = tuple([7] * 128)
    ok = frame_to_amplitude(make_frame(raw, t_s=0.0, first_word_invalid=False))
    bad = frame_to_amplitude(make_frame(raw, t_s=0.0, first_word_invalid=True))
    assert ok is not None and bad is not None
    # Position 1 is k=+1 (occupied) for the "secondary none" layout.
    assert LAYOUT.k_indices()[1] == 1
    assert ok.valid[1] and not bad.valid[1]
    assert not bad.valid[0]  # k=0 is DC anyway
    assert np.isnan(bad.amp[1])
    assert np.array_equal(ok.valid[2:], bad.valid[2:])


@pytest.mark.parametrize(
    "kwargs,raw_len,reason",
    [
        ({"layout_id": None}, 128, "UNKNOWN_LAYOUT"),
        ({"layout_id": "classic.bogus.sec_none.total128.LLTF64"}, 128, "UNKNOWN_LAYOUT"),
        ({}, 126, "LAYOUT_MISMATCH"),
        ({"extra_flags": (QualityFlag.UNKNOWN_LAYOUT.value,)}, 128, "UNKNOWN_LAYOUT"),
        ({"extra_flags": (QualityFlag.RX_STATE_ERROR.value,)}, 128, "RX_STATE_ERROR"),
    ],
)
def test_rejections(kwargs: dict, raw_len: int, reason: str) -> None:
    frame = make_frame(tuple([5] * raw_len), t_s=0.0, **kwargs)
    out = convert_frame(frame)
    assert isinstance(out, FrameRejection)
    assert out.reason == reason
    assert frame_to_amplitude(frame) is None


def test_all_zero_csi_rejected() -> None:
    out = convert_frame(make_frame(tuple([0] * 128), t_s=0.0))
    assert isinstance(out, FrameRejection) and out.reason == "ALL_ZERO_CSI"


def test_zero_on_every_occupied_subcarrier_rejected() -> None:
    raw = np.zeros(128, dtype=int)
    guard = np.flatnonzero(~LAYOUT.valid_position_mask())
    raw[2 * guard] = 9  # energy only on guard/DC positions
    out = convert_frame(make_frame(tuple(int(v) for v in raw), t_s=0.0))
    assert isinstance(out, FrameRejection) and out.reason == "NO_VALID_SUBCARRIERS"


def test_time_base_host_and_device_fallback() -> None:
    raw = tuple([6] * 128)
    host = frame_to_amplitude(make_frame(raw, t_s=1.5))
    assert host is not None
    assert host.t_ns == t_ns(1.5) and host.time_base == TIME_BASE_HOST
    assert host.t_unix_ns is not None

    dev = frame_to_amplitude(make_frame(raw, t_s=None, device_timestamp_us=123_456))
    assert dev is not None
    assert dev.t_ns == 123_456_000 and dev.time_base == TIME_BASE_DEVICE
    assert FLAG_DEVICE_TIME_BASE in dev.flags
    assert dev.t_unix_ns is None  # provenance time stays unavailable, never invented

    none = convert_frame(make_frame(raw, t_s=None))
    assert isinstance(none, FrameRejection) and none.reason == "NO_TIMESTAMP"


def test_counter_and_metadata_passthrough() -> None:
    s = frame_to_amplitude(make_frame(tuple([6] * 128), t_s=0.0, counter=42, rssi=None))
    assert s is not None
    assert s.counter == 42 and s.counter_source == "receiver"
    assert s.rssi is None  # missing metadata stays None
    assert QualityFlag.SYNTHETIC.value in s.flags


def test_phase_processing_refused() -> None:
    cfg = ProcessingConfig(use_phase=True)
    frame = make_frame(tuple([6] * 128), t_s=0.0)
    with pytest.raises(NotImplementedError, match="phase"):
        convert_frame(frame, cfg)
    with pytest.raises(NotImplementedError):
        ProcessingEngine(AppConfig(processing=cfg))
    from roomsense.processing.windows import Window, build_window

    samples = [frame_to_amplitude(make_frame(tuple([6 + (i % 3)] * 128), t_s=i * 0.04)) for i in range(50)]
    w = build_window(samples, end_ns=samples[-1].t_ns, cfg=ProcessingConfig(), link_id="l")
    assert isinstance(w, Window)
    with pytest.raises(NotImplementedError):
        extract_features(w, cfg)


@pytest.mark.parametrize(
    "mode,rejected",
    [
        (SourceMode.LIVE, True),  # make_frame always adds the SYNTHETIC flag
        (SourceMode.REPLAY, True),
        (SourceMode.SIMULATION, False),
    ],
)
def test_synthetic_frames_never_pass_as_measurements(mode: SourceMode, rejected: bool) -> None:
    out = convert_frame(make_frame(tuple([6] * 128), t_s=0.0, source_mode=mode))
    assert isinstance(out, FrameRejection) is rejected
    if rejected:
        assert out.reason == "SOURCE_FLAG_MISMATCH"  # type: ignore[union-attr]


def test_replayed_flag_in_live_source_rejected() -> None:
    frame = make_frame(tuple([6] * 128), t_s=0.0, source_mode=SourceMode.LIVE)
    live = dataclasses.replace(frame, quality_flags=(QualityFlag.REPLAYED.value,))
    out = convert_frame(live)
    assert isinstance(out, FrameRejection) and out.reason == "SOURCE_FLAG_MISMATCH"
    ok = convert_frame(dataclasses.replace(frame, quality_flags=()))
    assert isinstance(ok, AmplitudeSample)
