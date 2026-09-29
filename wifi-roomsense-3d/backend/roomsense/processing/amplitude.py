"""CSI frame -> per-subcarrier amplitude sample (capability B, first stage).

Only the amplitude ``|H|`` of the layout's primary segment is used. Phase is
deliberately ignored: raw ESP32 CSI phase contains per-packet offsets (CFO,
SFO, packet-detection delay) that need hardware-specific sanitisation, and no
such sanitisation has been validated for ESP32 boards in this project.

A frame is *rejected* (``None`` / :class:`FrameRejection`) rather than
repaired whenever its buffer does not match a documented layout. Nothing is
ever padded, truncated or re-ordered to force a shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from ..config import ProcessingConfig
from ..csi_layouts import extract_complex, layout_from_id
from ..schemas import CsiFrame, QualityFlag, SourceMode

__all__ = [
    "AmplitudeSample",
    "FrameRejection",
    "TIME_BASE_HOST",
    "TIME_BASE_DEVICE",
    "FLAG_DEVICE_TIME_BASE",
    "REJECT_UNKNOWN_LAYOUT",
    "REJECT_LAYOUT_MISMATCH",
    "REJECT_ALL_ZERO_CSI",
    "REJECT_NO_VALID_SUBCARRIERS",
    "REJECT_NO_TIMESTAMP",
    "REJECT_RX_STATE_ERROR",
    "REJECT_SOURCE_FLAG_MISMATCH",
    "cached_layout",
    "ensure_phase_disabled",
    "convert_frame",
    "frame_to_amplitude",
]

# Time bases. Samples of different time bases must never share a window.
TIME_BASE_HOST = "host_monotonic"
TIME_BASE_DEVICE = "device_timestamp"

# Processing-level flag added to a sample whose time axis had to fall back to
# the device clock because the host arrival time was missing.
FLAG_DEVICE_TIME_BASE = "DEVICE_TIME_BASE"

# Rejection reasons (stable strings, counted per link by the pipeline).
REJECT_UNKNOWN_LAYOUT = "UNKNOWN_LAYOUT"
REJECT_LAYOUT_MISMATCH = "LAYOUT_MISMATCH"
REJECT_ALL_ZERO_CSI = "ALL_ZERO_CSI"
REJECT_NO_VALID_SUBCARRIERS = "NO_VALID_SUBCARRIERS"
REJECT_NO_TIMESTAMP = "NO_TIMESTAMP"
REJECT_RX_STATE_ERROR = "RX_STATE_ERROR"
REJECT_SOURCE_FLAG_MISMATCH = "SOURCE_FLAG_MISMATCH"

# Parser flags that already mean "this buffer cannot be trusted as CSI".
_REJECTING_FLAGS: dict[str, str] = {
    QualityFlag.UNKNOWN_LAYOUT.value: REJECT_UNKNOWN_LAYOUT,
    QualityFlag.LAYOUT_MISMATCH.value: REJECT_LAYOUT_MISMATCH,
    QualityFlag.ALL_ZERO_CSI.value: REJECT_ALL_ZERO_CSI,
    # rx_state != 0 means the baseband reported a reception error; the CSI of
    # such a packet is not a valid channel estimate.
    QualityFlag.RX_STATE_ERROR.value: REJECT_RX_STATE_ERROR,
}


@dataclass(frozen=True, slots=True)
class AmplitudeSample:
    """Amplitude of one accepted frame.

    ``amp`` is float32 with ``NaN`` wherever ``valid`` is False (guard bands,
    DC, positions invalidated by ``first_word_invalid``, non-finite values).
    ``t_ns`` is on the time base named by ``time_base``; ``t_unix_ns`` is kept
    only for provenance and display and may be ``None``.
    """

    t_ns: int
    counter: int | None
    k: np.ndarray
    amp: np.ndarray
    valid: np.ndarray
    rssi: int | None
    flags: tuple[str, ...]
    layout_id: str
    t_unix_ns: int | None = None
    time_base: str = TIME_BASE_HOST
    # "transmitter" (end-to-end, over-the-air loss visible) or "receiver"
    # (only device queue/serial drops visible) or None.
    counter_source: str | None = None


@dataclass(frozen=True, slots=True)
class FrameRejection:
    reason: str
    detail: str = ""


@lru_cache(maxsize=64)
def cached_layout(layout_id: str | None):
    """``layout_from_id`` memoised (layouts are frozen; the set is small)."""
    return layout_from_id(layout_id)


def ensure_phase_disabled(cfg: ProcessingConfig | None) -> None:
    """Refuse to run with ``use_phase=True``.

    Raising (instead of silently ignoring the flag) makes it impossible to
    believe phase features are in use when they are not.
    """
    if cfg is not None and cfg.use_phase:
        raise NotImplementedError(
            "processing.use_phase=True is not supported: raw ESP32 CSI phase carries per-packet "
            "CFO/SFO/detection-delay offsets that need hardware-specific sanitisation, and no such "
            "sanitisation has been validated for ESP32 in this project. Only amplitude is used."
        )


def _read_only(a: np.ndarray) -> np.ndarray:
    a.setflags(write=False)
    return a


def convert_frame(frame: CsiFrame, cfg: ProcessingConfig | None = None) -> AmplitudeSample | FrameRejection:
    """Convert a frame to an :class:`AmplitudeSample` or say why it was rejected."""
    ensure_phase_disabled(cfg)

    for flag in frame.quality_flags:
        reason = _REJECTING_FLAGS.get(flag)
        if reason is not None:
            return FrameRejection(reason, f"frame carries parser flag {flag}")

    # Simulated or replayed data must never pass as live measurements.
    mode = SourceMode(frame.source_mode)
    if QualityFlag.SYNTHETIC.value in frame.quality_flags and mode != SourceMode.SIMULATION:
        return FrameRejection(REJECT_SOURCE_FLAG_MISMATCH, f"SYNTHETIC frame in a {mode.value} source")
    if QualityFlag.REPLAYED.value in frame.quality_flags and mode == SourceMode.LIVE:
        return FrameRejection(REJECT_SOURCE_FLAG_MISMATCH, "REPLAYED frame in a LIVE source")

    layout = cached_layout(frame.layout_id)
    if layout is None:
        return FrameRejection(REJECT_UNKNOWN_LAYOUT, f"layout_id {frame.layout_id!r} is not a documented layout")

    n_raw = len(frame.raw_csi)
    if n_raw != layout.total_values or frame.csi_len != n_raw:
        return FrameRejection(
            REJECT_LAYOUT_MISMATCH,
            f"{layout.layout_id} needs {layout.total_values} values; frame has {n_raw} (declared {frame.csi_len})",
        )

    raw = np.asarray(frame.raw_csi, dtype=np.int32)
    if not raw.any():
        return FrameRejection(REJECT_ALL_ZERO_CSI, "every CSI value is zero")

    # Time base: host arrival (monotonic) is the only clock shared by every
    # link on this host. The device clock is a per-receiver fallback.
    flags = list(frame.quality_flags)
    if frame.host_arrival_monotonic_ns is not None:
        t_ns = int(frame.host_arrival_monotonic_ns)
        time_base = TIME_BASE_HOST
    elif frame.device_timestamp_unwrapped_us is not None:
        t_ns = int(frame.device_timestamp_unwrapped_us) * 1000
        time_base = TIME_BASE_DEVICE
        flags.append(FLAG_DEVICE_TIME_BASE)
    else:
        return FrameRejection(REJECT_NO_TIMESTAMP, "neither host arrival time nor device timestamp is available")

    try:
        k, csi, valid = extract_complex(raw, layout, frame.first_word_invalid)
    except ValueError as exc:  # defensive: shape was checked above
        return FrameRejection(REJECT_LAYOUT_MISMATCH, str(exc))

    amp = np.abs(csi).astype(np.float32)
    valid = valid & np.isfinite(amp)
    amp[~valid] = np.nan
    if not valid.any() or not np.any(amp[valid] > 0):
        return FrameRejection(REJECT_NO_VALID_SUBCARRIERS, "no occupied subcarrier carries a non-zero value")

    if frame.transmitter_counter is not None:
        counter, counter_source = int(frame.transmitter_counter), "transmitter"
    elif frame.frame_counter_unwrapped is not None:
        counter, counter_source = int(frame.frame_counter_unwrapped), "receiver"
    else:
        counter, counter_source = None, None

    return AmplitudeSample(
        t_ns=t_ns,
        counter=counter,
        k=_read_only(np.asarray(k, dtype=np.int32)),
        amp=_read_only(amp),
        valid=_read_only(valid),
        rssi=frame.rssi_dbm,
        flags=tuple(flags),
        layout_id=layout.layout_id,
        t_unix_ns=frame.host_arrival_unix_ns,
        time_base=time_base,
        counter_source=counter_source,
    )


def frame_to_amplitude(frame: CsiFrame, cfg: ProcessingConfig | None = None) -> AmplitudeSample | None:
    """Contract entry point: the sample, or ``None`` if the frame is rejected.

    Use :func:`convert_frame` when the rejection reason is needed.
    """
    out = convert_frame(frame, cfg)
    return out if isinstance(out, AmplitudeSample) else None
