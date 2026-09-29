"""Pure validation metrics over decision timelines and labelled intervals.

Inputs
------
* A **decision timeline**: ``(t_unix_ns, state)`` samples for one link (one
  sample per detector output), turned into piecewise-constant segments by
  :meth:`Timeline.from_samples`. A sample's state holds until the next sample,
  but for at most ``max_hold_s``; after that the link is ``NO_DATA``
  (unobserved). :func:`combine_any_link` builds the any-link OR combination.
* **Labelled intervals** (:class:`Interval`), built from the operator's
  START/END label events with :func:`pair_labelled_events`.

Decision time
-------------
Only ``MOTION_DETECTED`` and ``NO_MOTION_DETECTED`` are decisions. UNKNOWN,
CALIBRATING, SENSOR_OFFLINE and NO_DATA time is reported separately and is
excluded from every rate denominator.

Honesty rules
-------------
No function invents a number: with no decision time or no events the value is
``None`` and ``reason`` says why. Rates carry exact Poisson intervals and
proportions carry Wilson intervals because the sample sizes here are small.
Conservative choices are documented where they are made (e.g. MOTION that is
already active at the start of an empty interval counts as a false alarm).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Sequence

import numpy as np
from scipy.stats import chi2, norm

from ..schemas import ActivityState

__all__ = [
    "NO_DATA",
    "MOTION",
    "NO_MOTION",
    "DECISION_STATES",
    "KNOWN_STATES",
    "Segment",
    "Interval",
    "Timeline",
    "combine_any_link",
    "ObservedTime",
    "observed_time",
    "RateEstimate",
    "poisson_rate_ci",
    "ProportionEstimate",
    "wilson_interval",
    "FalseAlarmResult",
    "false_alarms",
    "combine_false_alarms",
    "EventOutcome",
    "CarriedOverPolicy",
    "evaluate_events",
    "detection_fraction",
    "LatencySummary",
    "latency_summary",
    "TimeFraction",
    "time_in_state_fraction",
    "stationary_detection_fraction",
    "false_all_clear_fraction",
    "PairingResult",
    "pair_labelled_events",
    "sample_size_warnings",
    "INSUFFICIENT_EVENTS",
]

NO_DATA = "NO_DATA"
MOTION = ActivityState.MOTION_DETECTED.value
NO_MOTION = ActivityState.NO_MOTION_DETECTED.value
DECISION_STATES = frozenset({MOTION, NO_MOTION})
KNOWN_STATES = frozenset({s.value for s in ActivityState} | {NO_DATA})
_OFFLINE_LIKE = frozenset({ActivityState.SENSOR_OFFLINE.value, NO_DATA})

INSUFFICIENT_EVENTS = "insufficient events"

_NS = 1_000_000_000


def _ns(seconds: float) -> int:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("durations must be finite and >= 0")
    return int(round(seconds * _NS))


def _state_value(state: Any) -> str:
    value = state.value if hasattr(state, "value") else str(state)
    if value not in KNOWN_STATES:
        raise ValueError(f"unknown decision state {value!r}")
    return value


# ---------------------------------------------------------------------------
# Timelines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    start_ns: int
    end_ns: int  # exclusive
    state: str

    @property
    def duration_s(self) -> float:
        return (self.end_ns - self.start_ns) / _NS


@dataclass(frozen=True)
class Interval:
    """A labelled ground-truth interval ``[start_ns, end_ns)``."""

    start_ns: int
    end_ns: int
    label: str = ""
    event_id: str | None = None

    def __post_init__(self) -> None:
        if self.end_ns < self.start_ns:
            raise ValueError("interval end before start")

    @property
    def duration_s(self) -> float:
        return (self.end_ns - self.start_ns) / _NS


class _SegmentBuilder:
    def __init__(self, start_ns: int, end_ns: int) -> None:
        self.start_ns, self.end_ns = start_ns, end_ns
        self.segs: list[Segment] = []

    def add(self, a: int, b: int, state: str) -> None:
        a, b = max(a, self.start_ns), min(b, self.end_ns)
        if b <= a:
            return
        if self.segs and self.segs[-1].state == state and self.segs[-1].end_ns == a:
            self.segs[-1] = Segment(self.segs[-1].start_ns, b, state)  # merge equal neighbours
        else:
            self.segs.append(Segment(a, b, state))


@dataclass(frozen=True)
class Timeline:
    """Piecewise-constant decision states covering ``[start_ns, end_ns)``.

    Adjacent segments always differ in state, so every MOTION segment is one
    distinct MOTION period.
    """

    start_ns: int
    end_ns: int
    segments: tuple[Segment, ...]

    @classmethod
    def from_samples(
        cls,
        samples: Iterable[tuple[int, Any]],
        *,
        start_ns: int,
        end_ns: int,
        max_hold_s: float,
    ) -> "Timeline":
        """Build a timeline from ``(t_unix_ns, state)`` samples.

        Samples may be unsorted; for equal timestamps the last one given wins.
        Samples before ``start_ns`` still count while they are held (the
        decision in force when the interval began).
        """
        if end_ns < start_ns:
            raise ValueError("end_ns before start_ns")
        if not math.isfinite(max_hold_s) or max_hold_s <= 0:
            raise ValueError("max_hold_s must be > 0")
        hold = _ns(max_hold_s)
        ordered = sorted(((int(t), _state_value(s)) for t, s in samples), key=lambda p: p[0])
        pts: list[tuple[int, str]] = []
        for t, s in ordered:  # stable sort => later input wins on ties
            if pts and pts[-1][0] == t:
                pts[-1] = (t, s)
            else:
                pts.append((t, s))
        b = _SegmentBuilder(start_ns, end_ns)
        cursor = start_ns
        for i, (t, s) in enumerate(pts):
            if t >= end_ns:
                break
            nxt = pts[i + 1][0] if i + 1 < len(pts) else None
            seg_end = t + hold if nxt is None else min(nxt, t + hold)
            if t > cursor:
                b.add(cursor, t, NO_DATA)
            b.add(t, seg_end, s)
            cursor = max(cursor, seg_end)
        if cursor < end_ns:
            b.add(cursor, end_ns, NO_DATA)
        return cls(start_ns, end_ns, tuple(b.segs))

    @classmethod
    def constant(cls, state: str, *, start_ns: int, end_ns: int) -> "Timeline":
        state = _state_value(state)
        segs = (Segment(start_ns, end_ns, state),) if end_ns > start_ns else ()
        return cls(start_ns, end_ns, segs)

    def clip(self, start_ns: int, end_ns: int) -> "Timeline":
        """The part of the timeline inside ``[start_ns, end_ns)``. Time outside
        the timeline's own span is NO_DATA (never assumed quiet)."""
        if end_ns <= start_ns:
            return Timeline(start_ns, max(start_ns, end_ns), ())
        b = _SegmentBuilder(start_ns, end_ns)
        if start_ns < self.start_ns:
            b.add(start_ns, self.start_ns, NO_DATA)
        for seg in self.segments:
            if seg.end_ns <= start_ns or seg.start_ns >= end_ns:
                continue
            b.add(seg.start_ns, seg.end_ns, seg.state)
        if end_ns > self.end_ns:
            b.add(max(self.end_ns, start_ns), end_ns, NO_DATA)
        return Timeline(start_ns, end_ns, tuple(b.segs))

    def segment_at(self, t_ns: int) -> Segment | None:
        for seg in self.segments:
            if seg.start_ns <= t_ns < seg.end_ns:
                return seg
        return None

    def state_at(self, t_ns: int) -> str:
        seg = self.segment_at(t_ns)
        return NO_DATA if seg is None else seg.state

    def durations_s(self) -> dict[str, float]:
        out = {s: 0.0 for s in sorted(KNOWN_STATES)}
        for seg in self.segments:
            out[seg.state] += seg.duration_s
        return out

    def motion_onsets(self) -> list[int]:
        """Start time of every MOTION period. A period already active at the
        timeline start counts (at ``start_ns``): callers that clip to an
        interval therefore count MOTION carried into it, which is the
        conservative choice for false alarms."""
        return [seg.start_ns for seg in self.segments if seg.state == MOTION]

    @property
    def duration_s(self) -> float:
        return (self.end_ns - self.start_ns) / _NS


def _combine_states(states: Sequence[str]) -> str:
    if any(s == MOTION for s in states):
        return MOTION
    first = states[0]
    if all(s == first for s in states):
        return first
    if all(s in _OFFLINE_LIKE for s in states):
        return ActivityState.SENSOR_OFFLINE.value
    # Some links quiet, others unobserved: the unobserved area could hold
    # motion, so the combination cannot claim NO_MOTION.
    return ActivityState.UNKNOWN.value


def combine_any_link(timelines: Sequence[Timeline]) -> Timeline:
    """Any-link OR combination.

    MOTION if any link reports MOTION; NO_MOTION only if every link reports
    NO_MOTION; all-offline/no-data stays offline; any other mix is UNKNOWN.
    All timelines must cover the same span.
    """
    if not timelines:
        raise ValueError("combine_any_link needs at least one timeline")
    start, end = timelines[0].start_ns, timelines[0].end_ns
    if any(t.start_ns != start or t.end_ns != end for t in timelines):
        raise ValueError("timelines must cover the same span")
    bounds = sorted({start, end} | {x for t in timelines for s in t.segments for x in (s.start_ns, s.end_ns)})
    idx = [0] * len(timelines)
    b = _SegmentBuilder(start, end)
    for a, z in zip(bounds, bounds[1:]):
        states = []
        for k, tl in enumerate(timelines):
            segs = tl.segments
            while idx[k] < len(segs) and segs[idx[k]].end_ns <= a:
                idx[k] += 1
            if idx[k] < len(segs) and segs[idx[k]].start_ns <= a:
                states.append(segs[idx[k]].state)
            else:
                states.append(NO_DATA)
        b.add(a, z, _combine_states(states))
    return Timeline(start, end, tuple(b.segs))


# ---------------------------------------------------------------------------
# Observation time
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedTime:
    total_s: float
    decision_s: float  # MOTION + NO_MOTION: the only time rates are computed over
    motion_s: float
    no_motion_s: float
    unknown_s: float
    calibrating_s: float
    offline_s: float
    no_data_s: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def decision_hours(self) -> float:
        return self.decision_s / 3600.0


def observed_time(timeline: Timeline) -> ObservedTime:
    d = timeline.durations_s()
    return ObservedTime(
        total_s=timeline.duration_s,
        decision_s=d[MOTION] + d[NO_MOTION],
        motion_s=d[MOTION],
        no_motion_s=d[NO_MOTION],
        unknown_s=d[ActivityState.UNKNOWN.value],
        calibrating_s=d[ActivityState.CALIBRATING.value],
        offline_s=d[ActivityState.SENSOR_OFFLINE.value],
        no_data_s=d[NO_DATA],
    )


def _sum_observed(items: Sequence[ObservedTime]) -> ObservedTime:
    fields = ObservedTime.__dataclass_fields__.keys()
    return ObservedTime(**{f: float(sum(getattr(o, f) for o in items)) for f in fields})


# ---------------------------------------------------------------------------
# Confidence intervals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateEstimate:
    count: int | None
    exposure_hours: float | None
    rate_per_hour: float | None
    ci_low: float | None
    ci_high: float | None
    confidence: float = 0.95
    method: str = "exact Poisson (chi-square) interval"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def poisson_rate_ci(count: int, exposure_hours: float | None, confidence: float = 0.95) -> RateEstimate:
    """Events per hour with the exact (Garwood) Poisson interval:
    ``[chi2(a/2, 2k)/2, chi2(1-a/2, 2k+2)/2] / exposure``."""
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    if count < 0:
        raise ValueError("count must be >= 0")
    if exposure_hours is None or not math.isfinite(exposure_hours) or exposure_hours <= 0:
        return RateEstimate(count, exposure_hours, None, None, None, confidence,
                            reason="no decision time: a rate cannot be computed")
    alpha = 1.0 - confidence
    lo = 0.0 if count == 0 else float(chi2.ppf(alpha / 2, 2 * count)) / 2.0
    hi = float(chi2.ppf(1 - alpha / 2, 2 * count + 2)) / 2.0
    return RateEstimate(count, exposure_hours, count / exposure_hours, lo / exposure_hours, hi / exposure_hours,
                        confidence)


@dataclass(frozen=True)
class ProportionEstimate:
    successes: int | None
    trials: int | None
    fraction: float | None
    ci_low: float | None
    ci_high: float | None
    confidence: float = 0.95
    method: str = "Wilson score interval"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def wilson_interval(successes: int, trials: int, confidence: float = 0.95) -> ProportionEstimate:
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("need 0 <= successes <= trials")
    if trials == 0:
        return ProportionEstimate(successes, trials, None, None, None, confidence,
                                  reason="no events: a proportion cannot be computed")
    z = float(norm.ppf(1 - (1 - confidence) / 2))
    n = float(trials)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # At p = 0 or p = 1 the Wilson bound on that side is exactly 0 or 1; pin
    # it so rounding cannot print 0.9999999999999999.
    lo = 0.0 if successes == 0 else max(0.0, center - half)
    hi = 1.0 if successes == trials else min(1.0, center + half)
    return ProportionEstimate(successes, trials, p, lo, hi, confidence)


# ---------------------------------------------------------------------------
# False alarms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FalseAlarmResult:
    onsets: int
    observed: ObservedTime
    rate: RateEstimate
    onset_times_ns: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "onsets": self.onsets,
            "observed": self.observed.to_dict(),
            "rate": self.rate.to_dict(),
            "onset_times_ns": list(self.onset_times_ns),
            "warnings": list(self.warnings),
        }


def false_alarms(timeline: Timeline, empty_intervals: Sequence[Interval] | None = None,
                 *, confidence: float = 0.95) -> FalseAlarmResult:
    """MOTION onsets while the target room is known to be empty, per hour of
    decision time. Without explicit intervals the whole timeline is empty
    time (an S1 run). MOTION active at an interval start counts as an onset."""
    intervals = list(empty_intervals) if empty_intervals is not None else [
        Interval(timeline.start_ns, timeline.end_ns, "EMPTY")
    ]
    onset_times: list[int] = []
    observed: list[ObservedTime] = []
    for iv in intervals:
        part = timeline.clip(iv.start_ns, iv.end_ns)
        onset_times.extend(part.motion_onsets())
        observed.append(observed_time(part))
    obs = _sum_observed(observed) if observed else observed_time(Timeline(timeline.start_ns, timeline.start_ns, ()))
    rate = poisson_rate_ci(len(onset_times), obs.decision_hours if obs.decision_s > 0 else None, confidence)
    warnings: list[str] = []
    if obs.total_s > 0 and obs.decision_s < 0.5 * obs.total_s:
        warnings.append(
            f"less than half of the empty time was decision time ({obs.decision_s:.0f} of {obs.total_s:.0f} s)"
        )
    return FalseAlarmResult(len(onset_times), obs, rate, tuple(onset_times), tuple(warnings))


def combine_false_alarms(results: Sequence[FalseAlarmResult], *, confidence: float = 0.95) -> FalseAlarmResult:
    """Pool several runs: total onsets over total decision time."""
    if not results:
        empty = ObservedTime(0, 0, 0, 0, 0, 0, 0, 0)
        return FalseAlarmResult(0, empty, poisson_rate_ci(0, None, confidence), (), ("no runs",))
    obs = _sum_observed([r.observed for r in results])
    n = sum(r.onsets for r in results)
    rate = poisson_rate_ci(n, obs.decision_hours if obs.decision_s > 0 else None, confidence)
    times = tuple(t for r in results for t in r.onset_times_ns)
    warnings = tuple(w for r in results for w in r.warnings)
    return FalseAlarmResult(n, obs, rate, times, warnings)


# ---------------------------------------------------------------------------
# Events: recall, latency, missed events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventOutcome:
    event_id: str | None
    label: str
    start_ns: int
    end_ns: int
    detected: bool
    latency_s: float | None
    # MOTION was already active when the event started, so this event is not
    # an independent test of the detector and its latency is not measurable.
    motion_at_start: bool
    decision_s: float
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_events(timeline: Timeline, events: Sequence[Interval], *, tolerance_s: float) -> list[EventOutcome]:
    """Detection outcome per event.

    Detection window: ``[event start, event end + tolerance_s)``. Detected if a
    MOTION onset lies in the window; latency = first onset - event start.
    An event with no decision time in its window is *missed* (conservative:
    unobserved is never counted as a success).
    """
    tol = _ns(tolerance_s)
    out: list[EventOutcome] = []
    for ev in events:
        window = timeline.clip(ev.start_ns, ev.end_ns + tol)
        obs = observed_time(window)
        seg = timeline.segment_at(ev.start_ns)
        # A MOTION segment that began before the event (or at the very start
        # of the data, where its true beginning is unknown) is carried over.
        carried = seg is not None and seg.state == MOTION and (
            seg.start_ns < ev.start_ns or seg.start_ns == timeline.start_ns
        )
        onsets = window.motion_onsets()
        if carried:
            out.append(EventOutcome(ev.event_id, ev.label, ev.start_ns, ev.end_ns, True, None, True, obs.decision_s,
                                    "MOTION_DETECTED was already active at the event start; latency not measurable"))
        elif onsets:
            out.append(EventOutcome(ev.event_id, ev.label, ev.start_ns, ev.end_ns, True,
                                    (onsets[0] - ev.start_ns) / _NS, False, obs.decision_s, None))
        else:
            reason = (
                "no decision time in the detection window (UNKNOWN / SENSOR_OFFLINE / no data)"
                if obs.decision_s <= 0
                else "no MOTION_DETECTED onset within the event plus tolerance"
            )
            out.append(EventOutcome(ev.event_id, ev.label, ev.start_ns, ev.end_ns, False, None, False,
                                    obs.decision_s, reason))
    return out


CarriedOverPolicy = Literal["exclude", "count_as_detected"]


def detection_fraction(outcomes: Sequence[EventOutcome], *, carried_over: CarriedOverPolicy = "exclude",
                       confidence: float = 0.95) -> ProportionEstimate:
    """Detected events / events with a Wilson interval.

    ``carried_over`` decides what happens to events that started while MOTION
    was already active: ``"exclude"`` drops them (use for recall, so an
    always-on detector cannot earn recall this way) and ``"count_as_detected"``
    counts them (use for outside-room / interference events, where counting
    them can only make the result look worse).
    """
    if carried_over == "exclude":
        pool = [o for o in outcomes if not o.motion_at_start]
    elif carried_over == "count_as_detected":
        pool = list(outcomes)
    else:
        raise ValueError("carried_over must be 'exclude' or 'count_as_detected'")
    detected = sum(1 for o in pool if o.detected)
    return wilson_interval(detected, len(pool), confidence)


@dataclass(frozen=True)
class LatencySummary:
    n: int
    median_s: float | None
    p90_s: float | None
    max_s: float | None
    method: str = "numpy percentile, linear interpolation"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def latency_summary(outcomes: Sequence[EventOutcome]) -> LatencySummary:
    lat = [o.latency_s for o in outcomes if o.detected and o.latency_s is not None]
    if not lat:
        return LatencySummary(0, None, None, None, reason="no detected event with a measurable latency")
    arr = np.asarray(lat, dtype=float)
    return LatencySummary(len(lat), float(np.median(arr)), float(np.percentile(arr, 90)), float(arr.max()))


# ---------------------------------------------------------------------------
# Time fractions (stationary person, disconnects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeFraction:
    state: str
    state_s: float
    denominator_s: float
    denominator: str  # "decision" or "total"
    fraction: float | None
    intervals_used: int
    intervals_skipped: int
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def time_in_state_fraction(timeline: Timeline, intervals: Sequence[Interval], *, state: str,
                           skip_start_s: float = 0.0,
                           denominator: Literal["decision", "total"] = "decision") -> TimeFraction:
    """Share of time inside ``intervals`` spent in ``state``, ignoring the
    first ``skip_start_s`` of each interval."""
    state = _state_value(state)
    skip = _ns(skip_start_s)
    state_s = denom_s = 0.0
    used = skipped = 0
    for iv in intervals:
        a = iv.start_ns + skip
        if a >= iv.end_ns:
            skipped += 1
            continue
        part = timeline.clip(a, iv.end_ns)
        d = part.durations_s()
        obs = observed_time(part)
        state_s += d[state]
        denom_s += obs.decision_s if denominator == "decision" else obs.total_s
        used += 1
    if used == 0:
        reason = "no intervals" if not intervals else "every interval was shorter than the skipped start time"
        return TimeFraction(state, 0.0, 0.0, denominator, None, 0, skipped, reason)
    if denom_s <= 0:
        return TimeFraction(state, state_s, 0.0, denominator, None, used, skipped,
                            f"no {denominator} time inside the intervals")
    return TimeFraction(state, state_s, denom_s, denominator, state_s / denom_s, used, skipped)


def stationary_detection_fraction(timeline: Timeline, still_intervals: Sequence[Interval], *,
                                  settle_s: float) -> TimeFraction:
    """Share of decision time with MOTION while a person is still.

    Expected to be LOW: amplitude-variance motion detection does not see a
    motionless person (breathing is below what this pipeline resolves). A low
    value is the documented limitation, not a success.
    """
    return time_in_state_fraction(timeline, still_intervals, state=MOTION, skip_start_s=settle_s,
                                  denominator="decision")


def false_all_clear_fraction(timeline: Timeline, disconnect_intervals: Sequence[Interval], *,
                             grace_s: float) -> TimeFraction:
    """Share of total time inside disconnect intervals (after a grace period)
    reported as NO_MOTION_DETECTED. Anything above 0 is a defect: missing data
    must be reported as SENSOR_OFFLINE / UNKNOWN."""
    return time_in_state_fraction(timeline, disconnect_intervals, state=NO_MOTION, skip_start_s=grace_s,
                                  denominator="total")


# ---------------------------------------------------------------------------
# Label pairing and warnings
# ---------------------------------------------------------------------------


@dataclass
class PairingResult:
    intervals: dict[str, list[Interval]] = field(default_factory=dict)
    marks: list[tuple[int, str]] = field(default_factory=list)
    ignored_labels: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervals": {k: [asdict(i) for i in v] for k, v in self.intervals.items()},
            "marks": [{"t_unix_ns": t, "label": lab} for t, lab in self.marks],
            "ignored_labels": dict(self.ignored_labels),
            "warnings": list(self.warnings),
        }


def pair_labelled_events(events: Iterable[Any], labels: Iterable[str], *,
                         window_start_ns: int | None = None, window_end_ns: int | None = None) -> PairingResult:
    """Turn START/END label events into intervals per label.

    ``events`` are objects with ``t_unix_ns``, ``kind`` (MARK/START/END, enum
    or str), ``label`` and optionally ``event_id`` (e.g. ``LabeledEvent``).
    Labels match case-insensitively. Events outside the window are ignored.
    Unmatched STARTs/ENDs and zero-length intervals are dropped with a warning
    rather than guessed.
    """
    wanted = {str(lab).strip().upper() for lab in labels}
    res = PairingResult(intervals={lab: [] for lab in sorted(wanted)})
    items = []
    for order, ev in enumerate(events):
        t = int(ev.t_unix_ns)
        if window_start_ns is not None and t < window_start_ns:
            continue
        if window_end_ns is not None and t > window_end_ns:
            continue
        kind = getattr(ev.kind, "value", ev.kind)
        items.append((t, order, str(kind), str(ev.label).strip().upper(), getattr(ev, "event_id", None)))
    items.sort()
    open_: dict[str, tuple[int, str | None]] = {}
    for t, _order, kind, label, event_id in items:
        if kind == "MARK":
            res.marks.append((t, label))
            continue
        if label not in wanted:
            res.ignored_labels[label] = res.ignored_labels.get(label, 0) + 1
            continue
        if kind == "START":
            if label in open_:
                res.warnings.append(f"{label}: START at {open_[label][0]} had no END before the next START; dropped")
            open_[label] = (t, event_id)
        elif kind == "END":
            if label not in open_:
                res.warnings.append(f"{label}: END at {t} without a matching START; dropped")
                continue
            t0, eid = open_.pop(label)
            if t <= t0:
                res.warnings.append(f"{label}: zero-length interval at {t0}; dropped")
                continue
            res.intervals[label].append(Interval(t0, t, label, eid))
    for label, (t0, _eid) in sorted(open_.items()):
        res.warnings.append(f"{label}: START at {t0} never ended within the run; dropped")
    return res


def sample_size_warnings(*, n_events: int | None = None, min_events: int = 10,
                         observation_hours: float | None = None, min_hours: float | None = None,
                         what: str = "") -> list[str]:
    prefix = f"{what}: " if what else ""
    out: list[str] = []
    if n_events is not None and n_events < min_events:
        out.append(f"{prefix}{INSUFFICIENT_EVENTS} ({n_events} < {min_events})")
    if observation_hours is not None and min_hours is not None and observation_hours < min_hours:
        out.append(f"{prefix}insufficient observation time ({observation_hours:.2f} h < {min_hours:g} h)")
    return out
