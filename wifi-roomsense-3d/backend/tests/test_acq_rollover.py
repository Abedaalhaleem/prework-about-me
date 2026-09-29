"""Counter / timestamp unwrapping (software behaviour on made-up sequences)."""

from __future__ import annotations

import pytest

from roomsense.acquisition.rollover import CounterUnwrapper, TimestampUnwrapper, to_unsigned

M32 = 1 << 32
ROLL = "COUNTER_ROLLOVER"
GAP = "COUNTER_GAP"
RESET = "COUNTER_RESET"


def run(unwrapper, seq):
    return [unwrapper.update(v) for v in seq]


def test_counter_normal_sequence_has_no_flags():
    out = run(CounterUnwrapper(), [10, 11, 12, 13])
    assert [u for u, _ in out] == [10, 11, 12, 13]
    assert all(flags == [] for _, flags in out)


def test_counter_rollover_at_2_pow_32():
    out = run(CounterUnwrapper(), [M32 - 2, M32 - 1, 0, 1])
    assert [u for u, _ in out] == [M32 - 2, M32 - 1, M32, M32 + 1]
    assert out[2][1] == [ROLL]
    assert out[3][1] == []


def test_counter_rollover_with_gap_flags_both():
    u = CounterUnwrapper()
    u.update(M32 - 2)
    unwrapped, flags = u.update(3)
    assert unwrapped == M32 + 3
    assert flags == [ROLL, GAP]


def test_counter_forward_gap():
    u = CounterUnwrapper()
    u.update(5)
    assert u.update(9) == (9, [GAP])


def test_counter_reset_starts_new_epoch_and_keeps_increasing():
    u = CounterUnwrapper()
    u.update(1000)
    unwrapped, flags = u.update(5)
    assert flags == [RESET]
    assert unwrapped == 1001
    assert u.update(6) == (1002, [])
    assert u.epochs == 1


def test_counter_implausible_forward_jump_is_a_reset():
    u = CounterUnwrapper()
    u.update(5)
    unwrapped, flags = u.update(3_000_000_000)
    assert flags == [RESET]
    assert unwrapped == 6


def test_counter_reboot_near_top_of_range_is_reset_not_rollover():
    # 3e9 -> 0 would be a "wrap" by half-range logic; it is a reboot.
    u = CounterUnwrapper()
    u.update(3_000_000_000)
    assert u.update(0)[1] == [RESET]


def test_counter_negative_values_printed_with_percent_d_are_mapped_to_u32():
    u = CounterUnwrapper()
    out = run(u, [-2, -1, 0])  # upstream %d of 0xFFFFFFFE, 0xFFFFFFFF, then wrap
    assert [x for x, _ in out] == [M32 - 2, M32 - 1, M32]
    assert out[2][1] == [ROLL]
    assert to_unsigned(-1, 32) == M32 - 1


def test_counter_duplicate_value_does_not_advance():
    u = CounterUnwrapper()
    u.update(7)
    assert u.update(7) == (7, [])


@pytest.mark.parametrize("bad", [M32, -(1 << 31) - 1, 1 << 40])
def test_counter_rejects_values_outside_range(bad):
    with pytest.raises(ValueError):
        CounterUnwrapper().update(bad)


@pytest.mark.parametrize("bad", [1.5, True, "3"])
def test_counter_rejects_non_int(bad):
    with pytest.raises(ValueError):
        CounterUnwrapper().update(bad)  # type: ignore[arg-type]


def test_counter_custom_bit_width():
    u = CounterUnwrapper(bits=8)
    out = run(u, [254, 255, 0, 1])
    assert [x for x, _ in out] == [254, 255, 256, 257]
    with pytest.raises(ValueError):
        u.update(256)


def test_counter_bad_constructor_arguments():
    with pytest.raises(ValueError):
        CounterUnwrapper(bits=1)
    with pytest.raises(ValueError):
        CounterUnwrapper(bits=8, reset_threshold=256)


def test_counter_many_rollovers_accumulate():
    u = CounterUnwrapper(bits=8)
    last = None
    for i in range(1000):
        last, _ = u.update(i % 256)
    assert last == 999
    assert u.rollovers == 3


def test_timestamp_rollover_within_wrap_window():
    t = TimestampUnwrapper()
    t.update(M32 - 1_000)
    unwrapped, flags = t.update(500)
    assert flags == ["TIMESTAMP_ROLLOVER"]
    assert unwrapped == M32 + 500


def test_timestamp_forward_gaps_are_not_flagged():
    t = TimestampUnwrapper()
    t.update(1_000)
    assert t.update(60_000_000) == (60_000_000, [])


def test_timestamp_backwards_jump_is_non_monotonic_and_still_increasing():
    t = TimestampUnwrapper()
    t.update(5_000_000)
    unwrapped, flags = t.update(4_999_000)
    assert flags == ["TIMESTAMP_NON_MONOTONIC"]
    assert unwrapped == 5_000_001
    nxt, flags2 = t.update(5_000_000)
    assert flags2 == [] and nxt == 5_001_001


def test_timestamp_reboot_after_long_uptime_is_non_monotonic_not_rollover():
    t = TimestampUnwrapper()
    t.update(3_000_000_000)  # 50 min uptime
    assert t.update(1_000_000)[1] == ["TIMESTAMP_NON_MONOTONIC"]


def test_timestamp_negative_percent_d_values():
    t = TimestampUnwrapper()
    assert t.update(-1)[0] == M32 - 1
    assert t.update(10)[1] == ["TIMESTAMP_ROLLOVER"]


def test_timestamp_rejects_out_of_range():
    with pytest.raises(ValueError):
        TimestampUnwrapper().update(M32)
