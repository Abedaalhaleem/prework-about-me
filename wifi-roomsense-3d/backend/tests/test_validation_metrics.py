"""Validation metrics on small hand-computed cases.

Reference values: exact Poisson (Garwood) 95% limits for k = 0, 1, 2 are
[0, 3.689], [0.0253, 5.572], [0.2422, 7.225]; Wilson 95% intervals are
8/10 -> [0.4902, 0.9433] and 10/10 -> [0.7225, 1.0].
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from roomsense.validation.metrics import (
    INSUFFICIENT_EVENTS,
    MOTION,
    NO_DATA,
    NO_MOTION,
    Interval,
    Timeline,
    combine_any_link,
    combine_false_alarms,
    detection_fraction,
    evaluate_events,
    false_alarms,
    false_all_clear_fraction,
    latency_summary,
    observed_time,
    pair_labelled_events,
    poisson_rate_ci,
    sample_size_warnings,
    stationary_detection_fraction,
    wilson_interval,
)

S = 1_000_000_000
Q, M, U, OFF = NO_MOTION, MOTION, "UNKNOWN", "SENSOR_OFFLINE"


def tl(states: list[str], *, step_s: float = 1.0, start_s: float = 0.0, span_s: float | None = None,
       hold_s: float = 5.0) -> Timeline:
    samples = [(int((start_s + i * step_s) * S), s) for i, s in enumerate(states)]
    end = span_s if span_s is not None else start_s + len(states) * step_s
    return Timeline.from_samples(samples, start_ns=int(start_s * S), end_ns=int(end * S), max_hold_s=hold_s)


def segs(t: Timeline) -> list[tuple[float, float, str]]:
    return [(s.start_ns / S, s.end_ns / S, s.state) for s in t.segments]


# ---------------------------------------------------------------- timelines


def test_timeline_segments_merge_and_hold() -> None:
    t = tl([Q, Q, M, M, Q, U, U, Q, M, Q])
    assert segs(t) == [(0, 2, Q), (2, 4, M), (4, 5, Q), (5, 7, U), (7, 8, Q), (8, 9, M), (9, 10, Q)]
    gap = Timeline.from_samples([(0, Q), (20 * S, Q)], start_ns=0, end_ns=30 * S, max_hold_s=5)
    assert segs(gap) == [(0, 5, Q), (5, 20, NO_DATA), (20, 25, Q), (25, 30, NO_DATA)]
    # unsorted input and duplicate timestamps (the later sample wins)
    t2 = Timeline.from_samples([(2 * S, Q), (0, Q), (2 * S, M)], start_ns=0, end_ns=4 * S, max_hold_s=5)
    assert segs(t2) == [(0, 2, Q), (2, 4, M)]
    # a decision made before the interval still holds into it, up to the hold time
    carried = Timeline.from_samples([(-2 * S, M)], start_ns=0, end_ns=10 * S, max_hold_s=5)
    assert segs(carried) == [(0, 3, M), (3, 10, NO_DATA)]
    assert carried.state_at(1 * S) == M and carried.state_at(50 * S) == NO_DATA


def test_timeline_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        Timeline.from_samples([(0, "PRESENT")], start_ns=0, end_ns=S, max_hold_s=1)
    with pytest.raises(ValueError):
        Timeline.from_samples([], start_ns=S, end_ns=0, max_hold_s=1)
    with pytest.raises(ValueError):
        Timeline.from_samples([], start_ns=0, end_ns=S, max_hold_s=0)


def test_clip_outside_span_is_no_data() -> None:
    t = tl([Q, Q, Q, Q])
    assert segs(t.clip(-2 * S, 6 * S)) == [(-2, 0, NO_DATA), (0, 4, Q), (4, 6, NO_DATA)]
    assert t.clip(3 * S, 3 * S).segments == ()


def test_any_link_combination_rules() -> None:
    a = tl([Q] * 10)
    b = tl([Q, Q, M, M, Q, OFF, OFF, Q, Q, Q])
    c = combine_any_link([a, b])
    assert segs(c) == [(0, 2, Q), (2, 4, M), (4, 5, Q), (5, 7, U), (7, 10, Q)]
    both_off = combine_any_link([tl([OFF] * 3), tl([OFF, OFF, OFF])])
    assert segs(both_off) == [(0, 3, OFF)]
    mixed_off = combine_any_link([tl([OFF] * 3), tl([OFF] * 3, span_s=3, hold_s=0.5)])
    assert segs(mixed_off) == [(0, 3, OFF)]  # offline + no data stays offline, never quiet
    no_data = combine_any_link([Timeline.from_samples([], start_ns=0, end_ns=3 * S, max_hold_s=1)] * 2)
    assert segs(no_data) == [(0, 3, NO_DATA)]
    cal = combine_any_link([tl(["CALIBRATING"] * 2), tl([Q, Q])])
    assert segs(cal) == [(0, 2, U)]  # calibrating + quiet is not a decision
    assert segs(combine_any_link([tl(["CALIBRATING"] * 2)] * 2)) == [(0, 2, "CALIBRATING")]
    with pytest.raises(ValueError):
        combine_any_link([])
    with pytest.raises(ValueError):
        combine_any_link([tl([Q] * 3), tl([Q] * 4)])


# ---------------------------------------------------------------- CIs


def test_poisson_exact_ci_hand_values() -> None:
    r0 = poisson_rate_ci(0, 4.0)
    assert r0.rate_per_hour == 0.0 and r0.ci_low == 0.0
    assert r0.ci_high == pytest.approx(3.68888 / 4.0, abs=1e-4)
    r1 = poisson_rate_ci(1, 1.0)
    assert (r1.ci_low, r1.ci_high) == (pytest.approx(0.02532, abs=1e-4), pytest.approx(5.5716, abs=1e-3))
    r2 = poisson_rate_ci(2, 2.0)
    assert r2.rate_per_hour == 1.0
    assert r2.ci_low == pytest.approx(0.24217 / 2, abs=1e-4) and r2.ci_high == pytest.approx(7.2247 / 2, abs=1e-3)
    # the documented consequence in the criteria file: 0 alarms in 2 h is not enough for <= 1/h
    assert poisson_rate_ci(0, 2.0).ci_high > 1.0 and poisson_rate_ci(0, 3.7).ci_high <= 1.0


def test_poisson_without_exposure_returns_none_with_reason() -> None:
    for exposure in (None, 0.0, float("nan")):
        r = poisson_rate_ci(3, exposure)
        assert r.rate_per_hour is None and r.ci_low is None and r.ci_high is None
        assert r.reason and "no decision time" in r.reason


def test_wilson_hand_values() -> None:
    w = wilson_interval(8, 10)
    assert w.fraction == 0.8
    assert w.ci_low == pytest.approx(0.49016, abs=1e-4) and w.ci_high == pytest.approx(0.94332, abs=1e-4)
    full = wilson_interval(10, 10)
    assert full.ci_low == pytest.approx(0.72248, abs=1e-4) and full.ci_high == 1.0
    assert wilson_interval(20, 20).ci_low == pytest.approx(0.83887, abs=1e-4)
    empty = wilson_interval(0, 0)
    assert empty.fraction is None and empty.ci_low is None and "no events" in (empty.reason or "")
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


# ---------------------------------------------------------------- false alarms


def test_false_alarms_per_hour_excludes_unknown_time() -> None:
    t = tl([Q, Q, M, M, Q, U, U, Q, M, Q])  # 2 onsets, 8 s decision, 2 s UNKNOWN
    fa = false_alarms(t)
    assert fa.onsets == 2 and [x / S for x in fa.onset_times_ns] == [2, 8]
    assert fa.observed.decision_s == 8 and fa.observed.unknown_s == 2 and fa.observed.total_s == 10
    assert fa.rate.exposure_hours == pytest.approx(8 / 3600)
    assert fa.rate.rate_per_hour == pytest.approx(2 / (8 / 3600))
    same_decisions_more_unknown = tl([Q, Q, M, M, Q, U, U, U, U, U, U, Q, M, Q])
    assert false_alarms(same_decisions_more_unknown).rate.rate_per_hour == pytest.approx(fa.rate.rate_per_hour)


def test_false_alarms_explicit_intervals_and_carried_motion() -> None:
    t = tl([Q, M, M, M, Q, Q, Q, Q, M, Q])
    # an empty interval starting inside a MOTION period counts that MOTION (conservative)
    fa = false_alarms(t, [Interval(2 * S, 6 * S, "EMPTY")])
    assert fa.onsets == 1 and fa.observed.decision_s == 4
    pooled = combine_false_alarms([fa, false_alarms(t, [Interval(6 * S, 10 * S)])])
    assert pooled.onsets == 2 and pooled.observed.decision_s == 8
    assert pooled.rate.rate_per_hour == pytest.approx(2 / (8 / 3600))


def test_empty_inputs_give_none_with_reason() -> None:
    nothing = Timeline.from_samples([], start_ns=0, end_ns=60 * S, max_hold_s=5)
    fa = false_alarms(nothing)
    assert fa.onsets == 0 and fa.observed.decision_s == 0 and fa.observed.no_data_s == 60
    assert fa.rate.rate_per_hour is None and fa.rate.reason
    assert fa.warnings  # less than half of the time was decision time
    assert combine_false_alarms([]).rate.rate_per_hour is None
    zero_span = Timeline.from_samples([], start_ns=0, end_ns=0, max_hold_s=5)
    assert false_alarms(zero_span).rate.rate_per_hour is None
    assert evaluate_events(nothing, [], tolerance_s=3) == []
    d = detection_fraction([])
    assert d.fraction is None and d.reason
    lat = latency_summary([])
    assert lat.median_s is None and lat.p90_s is None and lat.reason
    st = stationary_detection_fraction(nothing, [], settle_s=5)
    assert st.fraction is None and st.reason == "no intervals"


# ---------------------------------------------------------------- events


def _event_timeline() -> Timeline:
    # 0-12 Q, 12-15 M, 15-30 Q, 30-35 UNKNOWN, 35-38 Q, 38-50 M, 50-60 Q, 60-80 Q, 80-81 M (onset at 80)
    states = [Q] * 12 + [M] * 3 + [Q] * 15 + [U] * 5 + [Q] * 3 + [M] * 12 + [Q] * 30 + [M] + [Q] * 9
    return tl(states)


def test_event_recall_latency_and_missed_events() -> None:
    t = _event_timeline()
    events = [
        Interval(10 * S, 20 * S, "MOVING", "e1"),  # onset at 12 -> latency 2
        Interval(20 * S, 28 * S, "MOVING", "e2"),  # nothing -> missed
        Interval(30 * S, 33 * S, "MOVING", "e3"),  # UNKNOWN + NO_DATA window after tolerance? 30-36 -> mostly unknown
        Interval(40 * S, 45 * S, "MOVING", "e4"),  # MOTION already active since 38 -> carried over
        Interval(70 * S, 77 * S, "MOVING", "e5"),  # onset at 80 = end + 3 s tolerance boundary -> missed
        Interval(71 * S, 78 * S, "MOVING", "e6"),  # onset at 80 within end + 3 -> latency 9
    ]
    out = {o.event_id: o for o in evaluate_events(t, events, tolerance_s=3.0)}
    assert out["e1"].detected and out["e1"].latency_s == 2.0 and not out["e1"].motion_at_start
    assert not out["e2"].detected and "no MOTION_DETECTED onset" in (out["e2"].reason or "")
    assert not out["e3"].detected and out["e3"].decision_s == 1.0  # 35-36 only
    assert out["e4"].detected and out["e4"].motion_at_start and out["e4"].latency_s is None
    assert not out["e5"].detected  # window is [70, 80): the onset at 80 is outside
    assert out["e6"].detected and out["e6"].latency_s == 9.0

    recall = detection_fraction(list(out.values()), carried_over="exclude")
    assert (recall.successes, recall.trials) == (2, 5)  # e4 excluded
    conservative = detection_fraction(list(out.values()), carried_over="count_as_detected")
    assert (conservative.successes, conservative.trials) == (3, 6)
    with pytest.raises(ValueError):
        detection_fraction([], carried_over="maybe")  # type: ignore[arg-type]

    lat = latency_summary(list(out.values()))
    assert lat.n == 2 and lat.median_s == 5.5 and lat.p90_s == pytest.approx(2 + 0.9 * 7) and lat.max_s == 9.0


def test_event_fully_unobserved_counts_as_missed_with_reason() -> None:
    t = Timeline.from_samples([(0, OFF)], start_ns=0, end_ns=30 * S, max_hold_s=60)
    o = evaluate_events(t, [Interval(5 * S, 10 * S, "MOVING")], tolerance_s=3)[0]
    assert not o.detected and o.decision_s == 0 and "no decision time" in (o.reason or "")


# ---------------------------------------------------------------- time fractions


def test_stationary_detection_fraction() -> None:
    t = tl([Q] * 5 + [M] * 3 + [Q] * 12)  # M during 5-8
    st = stationary_detection_fraction(t, [Interval(0, 20 * S, "STILL"), Interval(0, 3 * S, "STILL")], settle_s=5)
    assert st.fraction == pytest.approx(3 / 15) and st.intervals_used == 1 and st.intervals_skipped == 1
    only_short = stationary_detection_fraction(t, [Interval(0, 3 * S, "STILL")], settle_s=5)
    assert only_short.fraction is None and "shorter" in (only_short.reason or "")
    unobserved = stationary_detection_fraction(tl([U] * 20), [Interval(0, 20 * S)], settle_s=5)
    assert unobserved.fraction is None and "no decision time" in (unobserved.reason or "")


def test_false_all_clear_fraction_uses_total_time() -> None:
    t = tl([Q] * 8 + [OFF] * 12)
    fac = false_all_clear_fraction(t, [Interval(0, 20 * S, "DISCONNECT")], grace_s=5)
    assert fac.fraction == pytest.approx(3 / 15) and fac.denominator == "total"
    clean = false_all_clear_fraction(tl([Q] * 5 + [OFF] * 15), [Interval(0, 20 * S)], grace_s=5)
    assert clean.fraction == 0.0


# ---------------------------------------------------------------- pairing / warnings


@dataclass
class Ev:
    t_unix_ns: int
    kind: str
    label: str
    event_id: str | None = None


def test_pair_labelled_events() -> None:
    evs = [
        Ev(1 * S, "START", "moving", "a"), Ev(5 * S, "END", "MOVING"),
        Ev(6 * S, "END", "MOVING"),  # unmatched END
        Ev(7 * S, "START", "MOVING"), Ev(8 * S, "START", "MOVING", "b"), Ev(9 * S, "END", "MOVING"),  # double START
        Ev(10 * S, "START", "MOVING"), Ev(10 * S, "END", "MOVING"),  # zero length
        Ev(11 * S, "MARK", "door slam"),
        Ev(12 * S, "START", "DOOR"), Ev(13 * S, "END", "DOOR"),  # not requested
        Ev(14 * S, "START", "MOVING"),  # never ends
        Ev(99 * S, "START", "MOVING"), Ev(100 * S, "END", "MOVING"),  # outside the window
    ]
    res = pair_labelled_events(evs, ["MOVING"], window_start_ns=0, window_end_ns=50 * S)
    assert [(i.start_ns / S, i.end_ns / S, i.event_id) for i in res.intervals["MOVING"]] == [(1, 5, "a"), (8, 9, "b")]
    assert res.marks == [(11 * S, "DOOR SLAM")]
    assert res.ignored_labels == {"DOOR": 2}
    joined = " | ".join(res.warnings)
    for fragment in ("without a matching START", "had no END", "zero-length", "never ended"):
        assert fragment in joined
    assert pair_labelled_events([], ["X"]).intervals == {"X": []}


def test_sample_size_warnings() -> None:
    w = sample_size_warnings(n_events=5, min_events=10, observation_hours=1.0, min_hours=2.0, what="S2")
    assert w[0] == f"S2: {INSUFFICIENT_EVENTS} (5 < 10)" and "insufficient observation time" in w[1]
    assert sample_size_warnings(n_events=10, min_events=10) == []


def test_observed_time_totals_add_up() -> None:
    t = tl([Q, M, U, OFF, "CALIBRATING"], span_s=8, hold_s=1)
    o = observed_time(t)
    parts = o.decision_s + o.unknown_s + o.offline_s + o.calibrating_s + o.no_data_s
    assert parts == pytest.approx(o.total_s) == 8 and o.no_data_s == 3
