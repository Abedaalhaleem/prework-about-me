"""Unwrapping of device counters and 32-bit microsecond timestamps.

ESP32 firmware reports two wrapping quantities:

* record / packet sequence numbers (u32 in RoomSense firmware; a C ``int``
  printed with ``%d`` in the upstream esp-csi examples, so large values show
  up as negative numbers), and
* ``rx_ctrl.timestamp``, a u32 microsecond counter that wraps about every
  71.6 minutes (and is also printed with ``%d`` upstream).

The host turns them into monotonically increasing integers and reports *why*
a value did not simply advance, using :class:`~roomsense.schemas.QualityFlag`
values. A board reboot is never silently merged into the previous timeline:
it starts a new epoch and is flagged, so downstream code can split windows.
"""

from __future__ import annotations

from ..schemas import QualityFlag

__all__ = ["CounterUnwrapper", "TimestampUnwrapper", "to_unsigned"]


def to_unsigned(raw: int, bits: int) -> int:
    """Map a raw reported value to ``[0, 2**bits)``.

    Values printed with ``%d`` from an unsigned C field appear as negative
    numbers once the top bit is set; they are mapped back to the unsigned
    value. Anything that is not representable in ``bits`` (signed or
    unsigned) is corrupt input and raises ``ValueError`` instead of being
    masked into range, which would hide the corruption.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"counter value must be an int, got {type(raw).__name__}")
    modulus = 1 << bits
    value = raw + modulus if -(modulus >> 1) <= raw < 0 else raw
    if not 0 <= value < modulus:
        raise ValueError(f"value {raw} is outside the {bits}-bit range")
    return value


class CounterUnwrapper:
    """Unwrap an N-bit counter that normally advances by one per record.

    ``update(raw)`` returns ``(unwrapped, flags)``:

    * forward step of 1 -> no flag;
    * forward step > 1 (within ``reset_threshold``) -> ``COUNTER_GAP``;
    * numeric decrease that is a short step across ``2**bits`` ->
      ``COUNTER_ROLLOVER`` (plus ``COUNTER_GAP`` if records were skipped);
    * any other jump (backwards, or implausibly far forwards) ->
      ``COUNTER_RESET``. A new epoch starts so that the unwrapped value
      still increases by exactly one; the size of the jump is unknowable.
    * a repeated value keeps the previous unwrapped value (no flag): the
      counter did not advance, and inventing progress would be wrong.

    ``reset_threshold`` is the largest forward distance still treated as
    genuine progress. The default (2**24 for 32-bit counters, half the range
    for narrower ones) is far beyond any realistic loss burst at CSI packet
    rates (2**24 records at 1 kHz is more than 4 hours) while still telling
    a reboot near the top of the range apart from a wrap.
    """

    def __init__(self, bits: int = 32, reset_threshold: int | None = None) -> None:
        if not 2 <= bits <= 64:
            raise ValueError("bits must be in 2..64")
        self.bits = bits
        self.modulus = 1 << bits
        default_threshold = min(self.modulus >> 1, 1 << 24)
        self.reset_threshold = default_threshold if reset_threshold is None else int(reset_threshold)
        if not 1 <= self.reset_threshold < self.modulus:
            raise ValueError("reset_threshold must be in [1, 2**bits)")
        self._last_raw: int | None = None
        self._last_unwrapped: int | None = None
        self._offset = 0  # unwrapped = offset + raw within the current epoch
        self.epochs = 0  # number of COUNTER_RESET events seen
        self.rollovers = 0

    @property
    def last_unwrapped(self) -> int | None:
        return self._last_unwrapped

    def reset(self) -> None:
        """Forget history (e.g. when a new session starts)."""
        self._last_raw = None
        self._last_unwrapped = None
        self._offset = 0

    def update(self, raw: int) -> tuple[int, list[str]]:
        value = to_unsigned(raw, self.bits)
        flags: list[str] = []
        if self._last_raw is None or self._last_unwrapped is None:
            self._offset = 0
        else:
            forward = (value - self._last_raw) % self.modulus
            wrapped = value < self._last_raw
            if value == self._last_raw:
                pass  # duplicate: offset unchanged, unwrapped does not advance
            elif forward <= self.reset_threshold:
                if wrapped:
                    self._offset += self.modulus
                    self.rollovers += 1
                    flags.append(QualityFlag.COUNTER_ROLLOVER.value)
                if forward > 1:
                    flags.append(QualityFlag.COUNTER_GAP.value)
            else:
                # Not explainable as progress: start a new epoch that keeps
                # the unwrapped sequence strictly increasing.
                self._offset = self._last_unwrapped + 1 - value
                self.epochs += 1
                flags.append(QualityFlag.COUNTER_RESET.value)
        unwrapped = self._offset + value
        self._last_raw = value
        self._last_unwrapped = unwrapped
        return unwrapped, flags


class TimestampUnwrapper:
    """Unwrap ``rx_ctrl.timestamp`` (u32 microseconds, wraps ~71.6 min).

    * forward movement -> no flag (gaps in time are normal: packets can be
      lost or the transmitter can pause);
    * numeric decrease that fits a wrap with at most ``max_wrap_gap_us`` of
      elapsed time -> ``TIMESTAMP_ROLLOVER``;
    * any other decrease -> ``TIMESTAMP_NON_MONOTONIC`` (reboot, clock
      reset, or reordering). A new epoch starts one microsecond after the
      previous unwrapped value so the sequence keeps increasing, and the
      flag tells consumers (e.g. :class:`~.alignment.ClockModel`) that the
      device/host clock relation must be re-learned.

    The default ``max_wrap_gap_us`` of 10 minutes separates a genuine wrap
    from a reboot of a board that had been up for most of an hour. A real
    wrap hidden inside a longer outage is reported as non-monotonic, which is
    the conservative outcome.
    """

    BITS = 32

    def __init__(self, max_wrap_gap_us: int = 600_000_000) -> None:
        self.modulus = 1 << self.BITS
        if not 1 <= max_wrap_gap_us < self.modulus:
            raise ValueError("max_wrap_gap_us must be in [1, 2**32)")
        self.max_wrap_gap_us = int(max_wrap_gap_us)
        self._last_raw: int | None = None
        self._last_unwrapped: int | None = None
        self._offset = 0
        self.rollovers = 0
        self.discontinuities = 0

    @property
    def last_unwrapped(self) -> int | None:
        return self._last_unwrapped

    def reset(self) -> None:
        self._last_raw = None
        self._last_unwrapped = None
        self._offset = 0

    def update(self, raw_us: int) -> tuple[int, list[str]]:
        value = to_unsigned(raw_us, self.BITS)
        flags: list[str] = []
        if self._last_raw is None or self._last_unwrapped is None:
            self._offset = 0
        elif value < self._last_raw:
            forward = value + self.modulus - self._last_raw
            if forward <= self.max_wrap_gap_us:
                self._offset += self.modulus
                self.rollovers += 1
                flags.append(QualityFlag.TIMESTAMP_ROLLOVER.value)
            else:
                self._offset = self._last_unwrapped + 1 - value
                self.discontinuities += 1
                flags.append(QualityFlag.TIMESTAMP_NON_MONOTONIC.value)
        unwrapped = self._offset + value
        self._last_raw = value
        self._last_unwrapped = unwrapped
        return unwrapped, flags
