"""Through-wall validation report (JSON-ready dict + Markdown).

The report is computed from what is stored, never from what is displayed:

* decisions come from the ``activity_log`` table,
* ground truth comes from operator label events (``events`` table),
* setup metadata comes from ``validation_runs``,
* pass/fail thresholds come from ``configs/through_wall_criteria.toml``,
  whose hash (``criteria_version``) is recorded in the report.

Only COMPLETE, LIVE runs with the required metadata and an all-LIVE activity
log count as evidence. Everything else (simulation, replays, synthetic
recordings, incomplete runs) is listed with the reason it was excluded.

Runs are grouped by setup (placement + wall description + channel) because a
validation result only holds for the setup it was measured in. The headline
``through_wall_status`` refers to the setup of the most recent eligible run.
"""

from __future__ import annotations

import hashlib
import re
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from .. import SCHEMA_VERSION
from ..schemas import SourceMode
from ..storage.db import Database
from ..storage.models import ValidationRun, ValidationRunStatus
from .metrics import (
    MOTION,
    CarriedOverPolicy,
    NO_MOTION,
    EventOutcome,
    FalseAlarmResult,
    Interval,
    ObservedTime,
    TimeFraction,
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
    sample_size_warnings,
    stationary_detection_fraction,
)
from .protocol import (
    LABEL_DEGRADED,
    LABEL_DISCONNECT,
    LABEL_DOOR,
    LABEL_INTERFERENCE,
    LABEL_MOVING,
    LABEL_OUTSIDE_MOTION,
    LABEL_STILL,
    PROTOCOL_VERSION,
    SCENARIOS,
    get_scenario,
    missing_run_metadata,
)

__all__ = [
    "ThroughWallStatus",
    "Criteria",
    "CriteriaError",
    "load_criteria",
    "build_validation_report",
    "render_markdown",
    "NOT_MEASURED",
]

ThroughWallStatus = Literal["UNVERIFIED", "VALIDATED", "NOT_DISTINGUISHABLE"]
NOT_MEASURED = "NOT MEASURED"
_MAX_LISTED_MISSED = 200

S1, S2, S3, S4, S5, S6 = (s.scenario_id for s in SCENARIOS)


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


class CriteriaError(ValueError):
    """The criteria file is missing, malformed or incomplete."""


@dataclass(frozen=True)
class Criteria:
    file_name: str
    criteria_version: str
    name: str
    confidence: float
    s1_min_hours: float
    s1_max_fa_upper: float
    s2_min_events: int
    s2_min_recall_lower: float
    s3_min_events: int
    s3_not_distinguishable_fraction: float
    tolerance_s: float
    max_hold_s: float
    still_settle_s: float
    disconnect_grace_s: float
    min_events_warning: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file_name,
            "criteria_version": self.criteria_version,
            "name": self.name,
            "confidence_level": self.confidence,
            "s1_empty": {"min_observation_hours": self.s1_min_hours,
                         "max_false_alarms_per_hour_upper95": self.s1_max_fa_upper},
            "s2_moving": {"min_events": self.s2_min_events, "min_recall_lower95": self.s2_min_recall_lower},
            "s3_outside": {"min_events": self.s3_min_events,
                           "not_distinguishable_detection_fraction": self.s3_not_distinguishable_fraction},
            "metrics": {"detection_tolerance_s": self.tolerance_s, "max_sample_hold_s": self.max_hold_s,
                        "still_settle_s": self.still_settle_s, "disconnect_grace_s": self.disconnect_grace_s,
                        "min_events_warning": self.min_events_warning},
            "only_live_sessions_count": True,
        }


def load_criteria(path: Path | str) -> Criteria:
    """Parse the criteria TOML. Every threshold must be present: a missing
    value is an error, never a silent default."""
    p = Path(path)
    try:
        raw_bytes = p.read_bytes()
    except OSError as exc:
        raise CriteriaError(f"criteria file not readable: {p.name}: {exc}") from exc
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CriteriaError(f"criteria file is not valid TOML: {exc}") from exc

    def num(section: str, key: str, lo: float, hi: float) -> float:
        try:
            v = data[section][key]
        except (KeyError, TypeError) as exc:
            raise CriteriaError(f"criteria file lacks [{section}] {key}") from exc
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
            raise CriteriaError(f"[{section}] {key} must be a number in [{lo}, {hi}]")
        return float(v)

    def integer(section: str, key: str, lo: int, hi: int) -> int:
        v = num(section, key, lo, hi)
        if v != int(v):
            raise CriteriaError(f"[{section}] {key} must be an integer")
        return int(v)

    meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
    return Criteria(
        file_name=p.name,
        criteria_version=hashlib.sha256(raw_bytes).hexdigest()[:16],
        name=str(meta.get("name", "unnamed")),
        confidence=num("meta", "confidence_level", 0.5, 0.999),
        s1_min_hours=num("s1_empty", "min_observation_hours", 0.01, 10_000),
        s1_max_fa_upper=num("s1_empty", "max_false_alarms_per_hour_upper95", 0.0, 10_000),
        s2_min_events=integer("s2_moving", "min_events", 1, 1_000_000),
        s2_min_recall_lower=num("s2_moving", "min_recall_lower95", 0.0, 1.0),
        s3_min_events=integer("s3_outside", "min_events", 1, 1_000_000),
        s3_not_distinguishable_fraction=num("s3_outside", "not_distinguishable_detection_fraction", 0.0, 1.0),
        tolerance_s=num("metrics", "detection_tolerance_s", 0.0, 600.0),
        max_hold_s=num("metrics", "max_sample_hold_s", 0.1, 600.0),
        still_settle_s=num("metrics", "still_settle_s", 0.0, 600.0),
        disconnect_grace_s=num("metrics", "disconnect_grace_s", 0.0, 600.0),
        min_events_warning=integer("metrics", "min_events_warning", 1, 1_000_000),
    )


# ---------------------------------------------------------------------------
# Per-run analysis
# ---------------------------------------------------------------------------


@dataclass
class _RunData:
    run: ValidationRun
    eligible: bool
    exclusion_reasons: list[str]
    links: list[str] = field(default_factory=list)
    activity_rows: int = 0
    non_live_rows: int = 0
    calibration_ids: list[str] = field(default_factory=list)
    observed: ObservedTime | None = None
    per_link_observed: dict[str, ObservedTime] = field(default_factory=dict)
    fa: FalseAlarmResult | None = None
    fa_per_link: dict[str, FalseAlarmResult] = field(default_factory=dict)
    outcomes: dict[str, list[EventOutcome]] = field(default_factory=dict)
    outcomes_per_link: dict[str, dict[str, list[EventOutcome]]] = field(default_factory=dict)
    fractions: dict[str, TimeFraction] = field(default_factory=dict)
    state_time: dict[str, dict[str, float]] = field(default_factory=dict)
    label_events: int = 0
    pairing_warnings: list[str] = field(default_factory=list)
    missing_metadata: list[str] = field(default_factory=list)

    def setup_key(self) -> str:
        return _setup_key(self.run)


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).casefold()


def _setup_key(run: ValidationRun) -> str:
    raw = "\x1f".join([_norm_text(run.placement), _norm_text(run.wall_description), str(run.channel)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _analyse_run(db: Database, run: ValidationRun, crit: Criteria) -> _RunData:
    reasons: list[str] = []
    scenario = get_scenario(run.scenario_id)
    if scenario is None:
        reasons.append(f"unknown scenario {run.scenario_id!r}")
    if run.source_mode == SourceMode.SIMULATION:
        reasons.append("SIMULATION run: synthetic data is never validation evidence")
    elif run.source_mode == SourceMode.REPLAY:
        reasons.append("REPLAY run: a replay is not an independent live observation")
    if run.status != ValidationRunStatus.COMPLETE or run.ended_at_unix_ns is None:
        reasons.append(f"run is {run.status.value}, not COMPLETE")
    if run.recording_id is not None:
        rec = db.get_recording(run.recording_id)
        if rec is not None and rec.synthetic:
            reasons.append("the linked recording contains synthetic data")
        elif rec is not None and not rec.usable_as_validation_evidence:
            reasons.append("the linked recording is not a LIVE capture")
    data = _RunData(run=run, eligible=False, exclusion_reasons=reasons)
    if run.ended_at_unix_ns is None or scenario is None:
        return data  # nothing to measure yet

    start, end = run.started_at_unix_ns, run.ended_at_unix_ns
    hold_ns = int(crit.max_hold_s * 1e9)
    tol_ns = int(crit.tolerance_s * 1e9)
    span_end = end + tol_ns  # event windows may extend past the run end by the tolerance
    rows = db.list_activity(session_id=run.session_id, start_unix_ns=start - hold_ns, end_unix_ns=span_end)
    data.activity_rows = len(rows)
    data.non_live_rows = sum(1 for r in rows if r.source_mode != SourceMode.LIVE)
    if data.non_live_rows:
        reasons.append(f"activity log contains {data.non_live_rows} non-LIVE rows for this run")
    data.calibration_ids = sorted({r.calibration_id for r in rows if r.calibration_id})
    # The link set is what the run declared plus what logged decisions during
    # the run itself. Rows fetched only for the hold/tolerance margins must not
    # add links: a receiver of the *next* run would otherwise count as missing
    # for this whole run and turn it UNKNOWN.
    links = sorted(set(run.link_ids) | {r.link_id for r in rows if start <= r.t_end_unix_ns < end})
    data.links = links
    by_link: dict[str, list[tuple[int, str]]] = {lk: [] for lk in links}
    for r in rows:
        if r.link_id in by_link:
            by_link[r.link_id].append((r.t_end_unix_ns, r.state))
    per_link = {
        lk: Timeline.from_samples(s, start_ns=start, end_ns=span_end, max_hold_s=crit.max_hold_s)
        for lk, s in by_link.items()
    }
    combined = (
        combine_any_link(list(per_link.values()))
        if per_link
        else Timeline.from_samples([], start_ns=start, end_ns=span_end, max_hold_s=crit.max_hold_s)
    )
    run_view = combined.clip(start, end)
    data.observed = observed_time(run_view)
    data.per_link_observed = {lk: observed_time(tl.clip(start, end)) for lk, tl in per_link.items()}

    events = db.list_events(session_id=run.session_id, start_unix_ns=start, end_unix_ns=end, limit=None)
    data.label_events = sum(1 for e in events if e.kind.value in ("START", "END"))
    pairing = pair_labelled_events(events, scenario.interval_labels, window_start_ns=start, window_end_ns=end)
    data.pairing_warnings = list(pairing.warnings)
    if pairing.ignored_labels:
        data.pairing_warnings.append(
            "labels not used by this scenario were ignored: "
            + ", ".join(f"{k} ({v})" for k, v in sorted(pairing.ignored_labels.items()))
        )
    data.missing_metadata = missing_run_metadata(run, n_labelled_events=data.label_events)
    if data.missing_metadata:
        reasons.append("missing required metadata: " + ", ".join(data.missing_metadata))

    sid = scenario.scenario_id
    if sid in (S1, S4):
        # S1: the whole run is empty time. S4: onsets per hour over the run
        # (informational; door/interference intervals are part of it).
        data.fa = false_alarms(run_view, confidence=crit.confidence)
        data.fa_per_link = {lk: false_alarms(tl.clip(start, end), confidence=crit.confidence)
                            for lk, tl in per_link.items()}
    for label, intervals in pairing.intervals.items():
        if label == LABEL_STILL:
            data.fractions["stationary"] = stationary_detection_fraction(combined, intervals,
                                                                         settle_s=crit.still_settle_s)
            continue
        if label in (LABEL_DISCONNECT, LABEL_DEGRADED):
            data.state_time[label] = _state_time(combined, intervals)
            if label == LABEL_DISCONNECT:
                data.fractions["false_all_clear"] = false_all_clear_fraction(
                    combined, intervals, grace_s=crit.disconnect_grace_s)
            continue
        data.outcomes[label] = evaluate_events(combined, intervals, tolerance_s=crit.tolerance_s)
        for lk, tl in per_link.items():
            data.outcomes_per_link.setdefault(lk, {})[label] = evaluate_events(
                tl, intervals, tolerance_s=crit.tolerance_s)
    data.eligible = not reasons
    return data


def _exclude_overlaps(runs: Sequence[_RunData]) -> None:
    """Overlapping runs in one session would count the same decisions and
    labels twice (e.g. doubling S1 empty-room hours), so only the earliest of
    an overlapping group stays evidence."""
    last_end: dict[str, tuple[int, str]] = {}
    for r in sorted((r for r in runs if r.eligible), key=lambda r: (r.run.started_at_unix_ns, r.run.run_id)):
        sid = r.run.session_id
        end = r.run.ended_at_unix_ns
        assert end is not None  # eligible runs are COMPLETE
        prev = last_end.get(sid)
        if prev is not None and r.run.started_at_unix_ns < prev[0]:
            r.eligible = False
            r.exclusion_reasons.append(
                f"overlaps run {prev[1]} in the same session; its time would be counted twice"
            )
            continue
        last_end[sid] = (end, r.run.run_id)


def _state_time(tl: Timeline, intervals: Sequence[Interval]) -> dict[str, float]:
    total: dict[str, float] = {}
    for iv in intervals:
        for state, secs in tl.clip(iv.start_ns, iv.end_ns).durations_s().items():
            total[state] = total.get(state, 0.0) + secs
    return total


# ---------------------------------------------------------------------------
# Aggregation per setup
# ---------------------------------------------------------------------------


def _pool_fraction(items: Sequence[TimeFraction], state: str, denominator: str) -> dict[str, Any]:
    if not items:
        return {"state": state, "fraction": None, "state_s": None, "denominator_s": None,
                "denominator": denominator, "reason": "no labelled intervals"}
    st = sum(i.state_s for i in items)
    den = sum(i.denominator_s for i in items)
    return {
        "state": state,
        "fraction": (st / den) if den > 0 else None,
        "state_s": st,
        "denominator_s": den,
        "denominator": denominator,
        "intervals_used": sum(i.intervals_used for i in items),
        "intervals_skipped": sum(i.intervals_skipped for i in items),
        "reason": None if den > 0 else f"no {denominator} time inside the labelled intervals",
    }


def _sum_observed(items: Sequence[ObservedTime]) -> dict[str, Any] | None:
    if not items:
        return None
    keys = ObservedTime.__dataclass_fields__.keys()
    return {k: float(sum(getattr(o, k) for o in items)) for k in keys}


def _outcomes(runs: Sequence[_RunData], label: str) -> list[EventOutcome]:
    return [o for r in runs for o in r.outcomes.get(label, [])]


def _event_block(outcomes: list[EventOutcome], *, policy: CarriedOverPolicy, crit: Criteria,
                 what: str) -> dict[str, Any]:
    frac = detection_fraction(outcomes, carried_over=policy, confidence=crit.confidence)
    missed = [o.to_dict() for o in outcomes if not o.detected]
    carried = sum(1 for o in outcomes if o.motion_at_start)
    warnings = sample_size_warnings(n_events=frac.trials, min_events=crit.min_events_warning, what=what)
    if carried:
        warnings.append(
            f"{what}: {carried} event(s) started while MOTION was already active "
            + ("and were excluded" if policy == "exclude" else "and were counted as detected")
        )
    return {
        "events_total": len(outcomes),
        "events_carried_over": carried,
        "carried_over_policy": policy,
        "detection": frac.to_dict(),
        "latency": latency_summary(outcomes).to_dict(),
        "missed_events": missed[:_MAX_LISTED_MISSED],
        "missed_events_truncated": len(missed) > _MAX_LISTED_MISSED,
        "warnings": warnings,
    }


def _per_link_detection(runs: Sequence[_RunData], label: str, policy: CarriedOverPolicy,
                        crit: Criteria) -> dict[str, Any]:
    links = sorted({lk for r in runs for lk in r.outcomes_per_link})
    out: dict[str, Any] = {}
    for lk in links:
        outs = [o for r in runs for o in r.outcomes_per_link.get(lk, {}).get(label, [])]
        out[lk] = detection_fraction(outs, carried_over=policy, confidence=crit.confidence).to_dict()
    return out


def _scenario_block(sid: str, runs: Sequence[_RunData], crit: Criteria) -> dict[str, Any]:
    sc = get_scenario(sid)
    assert sc is not None
    measured_runs = [r for r in runs if r.observed is not None]
    block: dict[str, Any] = {
        "scenario_id": sid,
        "title": sc.title,
        "counts_toward_status": sc.counts_toward_status,
        "runs": len(runs),
        "run_ids": [r.run.run_id for r in runs],
        "duration_s": sum(r.run.duration_s or 0.0 for r in runs) if runs else None,
        "observed": _sum_observed([r.observed for r in measured_runs if r.observed is not None]),
        "measured": bool(measured_runs),
        "warnings": [w for r in runs for w in r.pairing_warnings],
    }
    if sid in (S1, S4):
        pooled = combine_false_alarms([r.fa for r in runs if r.fa is not None], confidence=crit.confidence)
        per_link: dict[str, Any] = {}
        for lk in sorted({lk for r in runs for lk in r.fa_per_link}):
            per_link[lk] = combine_false_alarms(
                [r.fa_per_link[lk] for r in runs if lk in r.fa_per_link], confidence=crit.confidence
            ).rate.to_dict()
        key = "false_alarms" if sid == S1 else "motion_onsets"
        block[key] = {"onsets": pooled.onsets if runs else None, "rate": pooled.rate.to_dict(),
                      "per_link": per_link}
        if sid == S1:
            hours = pooled.observed.decision_hours
            block["decision_hours"] = hours if runs else None
            block["warnings"] += sample_size_warnings(observation_hours=hours, min_hours=crit.s1_min_hours,
                                                      what="S1") if runs else []
            block["warnings"] += list(pooled.warnings) if runs else []
    if sid == S2:
        block["recall"] = _event_block(_outcomes(runs, LABEL_MOVING), policy="exclude", crit=crit, what="S2")
        block["per_link_recall"] = _per_link_detection(runs, LABEL_MOVING, "exclude", crit)
    if sid == S3:
        block["outside_detection"] = _event_block(_outcomes(runs, LABEL_OUTSIDE_MOTION), policy="count_as_detected",
                                                  crit=crit, what="S3")
        block["per_link_outside_detection"] = _per_link_detection(runs, LABEL_OUTSIDE_MOTION, "count_as_detected",
                                                                  crit)
    if sid == S4:
        block["door_detection"] = _event_block(_outcomes(runs, LABEL_DOOR), policy="count_as_detected",
                                               crit=crit, what="S4 door")
        block["interference_detection"] = _event_block(_outcomes(runs, LABEL_INTERFERENCE),
                                                       policy="count_as_detected", crit=crit, what="S4 interference")
    if sid == S5:
        block["moving_recall"] = _event_block(_outcomes(runs, LABEL_MOVING), policy="exclude", crit=crit,
                                              what="S5 moving")
        block["stationary_detection"] = _pool_fraction(
            [r.fractions["stationary"] for r in runs if "stationary" in r.fractions], MOTION, "decision")
        block["limitation"] = (
            "A motionless person is usually NOT detected. A low stationary detection fraction is expected; "
            "NO_MOTION_DETECTED never means the room is empty."
        )
    if sid == S6:
        block["false_all_clear"] = _pool_fraction(
            [r.fractions["false_all_clear"] for r in runs if "false_all_clear" in r.fractions], NO_MOTION, "total")
        for label in (LABEL_DISCONNECT, LABEL_DEGRADED):
            parts = [r.state_time[label] for r in runs if label in r.state_time]
            block[f"state_time_{label.lower()}_s"] = (
                {k: float(sum(p.get(k, 0.0) for p in parts)) for k in sorted({k for p in parts for k in p})}
                if parts else None
            )
        fac = block["false_all_clear"]
        if fac.get("fraction") is not None and fac["fraction"] > 0:
            block["warnings"].append(
                "DEFECT: NO_MOTION_DETECTED was reported while a receiver was disconnected "
                f"({fac['state_s']:.1f} s after the grace period)"
            )
    return block


def _setup_status(scen: dict[str, dict[str, Any]], crit: Criteria) -> tuple[ThroughWallStatus, str, list[str]]:
    unmet: list[str] = []
    s1, s2, s3 = scen[S1], scen[S2], scen[S3]

    # S3 first: if outside movement is detected at a comparable rate, nothing
    # else can make the detections attributable to the target room.
    s3_det = s3["outside_detection"]["detection"]
    s3_n = s3_det["trials"] or 0
    s3_frac = s3_det["fraction"]
    if s3_n >= crit.s3_min_events and s3_frac is not None and s3_frac >= crit.s3_not_distinguishable_fraction:
        expl = (
            f"Movement near the sensors OUTSIDE the target room was detected in {s3_det['successes']} of {s3_n} "
            f"events ({s3_frac:.0%}), at or above the predefined threshold of "
            f"{crit.s3_not_distinguishable_fraction:.0%}. Detections cannot be attributed to the room behind the "
            "wall in this setup."
        )
        return "NOT_DISTINGUISHABLE", expl, [
            f"S3: outside-room detection fraction {s3_frac:.2f} >= {crit.s3_not_distinguishable_fraction:g}"
        ]

    hours = s1.get("decision_hours")
    rate = s1["false_alarms"]["rate"]
    if not s1["runs"] or hours is None or hours <= 0:
        unmet.append(f"S1 (empty room): {NOT_MEASURED}")
    elif hours < crit.s1_min_hours:
        unmet.append(f"S1: {hours:.2f} h of decision time; at least {crit.s1_min_hours:g} h required")
    elif rate["ci_high"] is None or rate["ci_high"] > crit.s1_max_fa_upper:
        high = "n/a" if rate["ci_high"] is None else f"{rate['ci_high']:.2f}"
        unmet.append(f"S1: false-alarm upper 95% bound {high}/h exceeds {crit.s1_max_fa_upper:g}/h")

    rec = s2["recall"]["detection"]
    n2 = rec["trials"] or 0
    if not s2["runs"] or n2 == 0:
        unmet.append(f"S2 (person moving behind wall): {NOT_MEASURED}")
    elif n2 < crit.s2_min_events:
        unmet.append(f"S2: {n2} usable events; at least {crit.s2_min_events} required")
    elif rec["ci_low"] is None or rec["ci_low"] < crit.s2_min_recall_lower:
        unmet.append(f"S2: recall lower 95% bound {rec['ci_low']:.2f} is below {crit.s2_min_recall_lower:g}")

    if not s3["runs"] or s3_n == 0:
        unmet.append(f"S3 (movement near sensors outside the room): {NOT_MEASURED}")
    elif s3_n < crit.s3_min_events:
        unmet.append(f"S3: {s3_n} events; at least {crit.s3_min_events} required to check distinguishability")

    if unmet:
        return "UNVERIFIED", "Through-wall motion detection is not validated for this setup: " + "; ".join(unmet), unmet
    expl = (
        f"LIVE runs in this setup met every predefined criterion: S1 false alarms <= "
        f"{crit.s1_max_fa_upper:g}/h (upper 95% bound {rate['ci_high']:.2f}/h over {hours:.2f} h), S2 recall "
        f"lower 95% bound {rec['ci_low']:.2f} over {n2} events, S3 outside-room detection "
        f"{s3_frac:.0%} of {s3_n} events. This applies to motion detection of one moving person in this setup "
        "only; it says nothing about a motionless person, zones or pose."
    )
    return "VALIDATED", expl, []


def _setup_meta(runs: Sequence[_RunData]) -> dict[str, Any]:
    latest = max(runs, key=lambda r: r.run.started_at_unix_ns).run
    return {
        "setup_key": _setup_key(latest),
        "placement": latest.placement,
        "wall_description": latest.wall_description,
        "channel": latest.channel,
        "runs": len(runs),
        "total_duration_s": sum(r.run.duration_s or 0.0 for r in runs),
        "links": sorted({lk for r in runs for lk in r.links}),
        "calibration_ids": sorted({c for r in runs for c in r.calibration_ids}),
        "latest_run_started_at_unix_ns": latest.started_at_unix_ns,
    }


def _run_summary(d: _RunData) -> dict[str, Any]:
    r = d.run
    return {
        "run_id": r.run_id,
        "scenario_id": r.scenario_id,
        "session_id": r.session_id,
        "recording_id": r.recording_id,
        "source_mode": r.source_mode.value,
        "status": r.status.value,
        "started_at_unix_ns": r.started_at_unix_ns,
        "ended_at_unix_ns": r.ended_at_unix_ns,
        "duration_s": r.duration_s,
        "placement": r.placement,
        "wall_description": r.wall_description,
        "channel": r.channel,
        "conditions": r.conditions,
        "notes": r.notes,
        "setup_key": d.setup_key(),
        "links": d.links,
        "activity_rows": d.activity_rows,
        "label_events": d.label_events,
        "observed": None if d.observed is None else d.observed.to_dict(),
        "counted_as_evidence": d.eligible,
        "exclusion_reasons": d.exclusion_reasons,
        "missing_metadata": d.missing_metadata,
        "warnings": d.pairing_warnings,
    }


def build_validation_report(db: Database, criteria_path: Path | str, *, now_ns: int | None = None,
                            software_tested: bool | None = None) -> dict[str, Any]:
    """Build the through-wall validation report from the database.

    ``software_tested`` may be passed by a caller that knows the automated
    test result; the report itself never runs tests and leaves it ``None``
    (unknown) otherwise.
    """
    crit = load_criteria(criteria_path)
    runs = [_analyse_run(db, r, crit) for r in db.list_validation_runs(limit=None)]
    _exclude_overlaps(runs)
    eligible = [r for r in runs if r.eligible]

    groups: dict[str, list[_RunData]] = {}
    for r in eligible:
        groups.setdefault(r.setup_key(), []).append(r)

    setups: list[dict[str, Any]] = []
    for key, members in groups.items():
        scen = {sid: _scenario_block(sid, [m for m in members if m.run.scenario_id == sid], crit)
                for sid in (s.scenario_id for s in SCENARIOS)}
        status, expl, unmet = _setup_status(scen, crit)
        setups.append({**_setup_meta(members), "status": status, "explanation": expl,
                       "unmet_requirements": unmet, "scenarios": scen})
    setups.sort(key=lambda s: s["latest_run_started_at_unix_ns"], reverse=True)

    if setups:
        current = setups[0]
        status: ThroughWallStatus = current["status"]
        explanation = current["explanation"]
        unmet = current["unmet_requirements"]
        scenarios = current["scenarios"]
        current_setup = {k: v for k, v in current.items() if k != "scenarios"}
    else:
        scenarios = {sid: _scenario_block(sid, [], crit) for sid in (s.scenario_id for s in SCENARIOS)}
        # With no data _setup_status can only list what is missing.
        _, _, unmet = _setup_status(scenarios, crit)
        status = "UNVERIFIED"
        explanation = (
            "No LIVE validation run counts as evidence yet, so through-wall detection is UNVERIFIED. "
            "Simulation, replays and incomplete runs never count."
        )
        current_setup = None

    hardware_runs = [r for r in runs if r.run.source_mode == SourceMode.LIVE and r.non_live_rows == 0
                     and r.observed is not None and r.observed.decision_s > 0]
    excluded = [r for r in runs if not r.eligible]
    report: dict[str, Any] = {
        "report_type": "through_wall_validation",
        "generated_at_unix_ns": time.time_ns() if now_ns is None else int(now_ns),
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "criteria_version": crit.criteria_version,
        "criteria": crit.to_dict(),
        "through_wall_status": status,
        "explanation": explanation,
        "unmet_requirements": unmet,
        "current_setup": current_setup,
        "scenarios": scenarios,
        "setups": [{k: v for k, v in s.items()} for s in setups[1:]],
        "evidence_levels": {
            "software_tested": {
                "value": software_tested,
                "basis": "Automated tests on synthetic and fixture data. They show that code paths run, not that "
                "sensing works. This report does not run them.",
            },
            "hardware_tested": {
                "value": bool(hardware_runs),
                "basis": "True only if at least one LIVE validation run logged motion decisions from real "
                "receivers" + (f" ({len(hardware_runs)} run(s))." if hardware_runs else "."),
            },
            "through_wall_validated": {
                "value": status == "VALIDATED",
                "basis": f"Predefined criteria {crit.criteria_version} evaluated on LIVE runs of the current setup.",
            },
        },
        "runs": [_run_summary(r) for r in runs],
        "excluded_runs": [
            {"run_id": r.run.run_id, "scenario_id": r.run.scenario_id, "source_mode": r.run.source_mode.value,
             "reasons": r.exclusion_reasons}
            for r in excluded
        ],
        "notes": [
            "Only LIVE runs count. SIMULATION and REPLAY runs, synthetic recordings and incomplete runs are listed "
            "but excluded from evidence.",
            "Decision time is MOTION_DETECTED + NO_MOTION_DETECTED under the any-link OR combination; UNKNOWN, "
            "SENSOR_OFFLINE and missing data are reported separately and excluded from every rate.",
            "Activity scores are heuristic deviations from a quiet baseline, not probabilities.",
            "A motionless person can remain undetected. This is not a people counter and performs no "
            "identification.",
        ],
    }
    return report


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _v(value: Any, fmt: str = "{}") -> str:
    if value is None:
        return NOT_MEASURED
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return str(value)


def _ci(est: dict[str, Any] | None, fmt: str = "{:.3f}") -> str:
    if not est or est.get("ci_low") is None or est.get("ci_high") is None:
        return NOT_MEASURED
    return f"[{fmt.format(est['ci_low'])}, {fmt.format(est['ci_high'])}]"


def _ts(ns: int | None) -> str:
    if ns is None:
        return NOT_MEASURED
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _event_rows(block: dict[str, Any], name: str, measured: bool) -> list[str]:
    det = block["detection"]
    lat = block["latency"]
    if not measured:
        return [f"| {name}: {m} | {NOT_MEASURED} |" for m in (
            "events (total / carried over)", "detected / usable", "detection fraction", "Wilson 95% CI",
            "latency median / p90 (s)", "missed events")]
    detected = f"{det['successes']} / {det['trials']}" if det["trials"] else NOT_MEASURED
    return [
        f"| {name}: events (total / carried over) | {block['events_total']} / {block['events_carried_over']} |",
        f"| {name}: detected / usable | {detected} |",
        f"| {name}: detection fraction | {_v(det['fraction'], '{:.3f}')} |",
        f"| {name}: Wilson 95% CI | {_ci(det)} |",
        f"| {name}: latency median / p90 (s) | {_v(lat['median_s'], '{:.2f}')} / {_v(lat['p90_s'], '{:.2f}')} |",
        f"| {name}: missed events | {len(block['missed_events']) if det['trials'] else NOT_MEASURED} |",
    ]


def _observed_rows(obs: dict[str, Any] | None) -> list[str]:
    decision_label = "Decision time (h)"
    other_label = "UNKNOWN / CALIBRATING / OFFLINE / no-data time (h, excluded from rates)"
    if obs is None:
        return [f"| {decision_label} | {NOT_MEASURED} |", f"| {other_label} | {NOT_MEASURED} |"]
    other = obs["unknown_s"] + obs["calibrating_s"] + obs["offline_s"] + obs["no_data_s"]
    return [f"| {decision_label} | {obs['decision_s'] / 3600:.3f} |", f"| {other_label} | {other / 3600:.3f} |"]


def _scenario_md(block: dict[str, Any]) -> list[str]:
    sid = block["scenario_id"]
    measured = bool(block["measured"])
    out = [f"### {sid}: {block['title']}", ""]
    if not block["runs"]:
        out += [f"**{NOT_MEASURED}** (no counted LIVE runs).", ""]
    duration_h = None if block["duration_s"] is None else block["duration_s"] / 3600
    out += ["| Metric | Value |", "|---|---|", f"| Counted runs | {block['runs']} |",
            f"| Run duration (h) | {_v(duration_h, '{:.3f}')} |"]
    out += _observed_rows(block["observed"])
    if sid in (S1, S4):
        fa = block["false_alarms" if sid == S1 else "motion_onsets"]
        name = "False alarms" if sid == S1 else "Motion onsets"
        out += [
            f"| {name} (count) | {_v(fa['onsets'])} |",
            f"| {name} per hour of decision time | {_v(fa['rate']['rate_per_hour'], '{:.3f}')} |",
            f"| Exact Poisson 95% CI (per hour) | {_ci(fa['rate'])} |",
        ]
    if sid == S2:
        out += _event_rows(block["recall"], "Recall", measured)
    if sid == S3:
        out += _event_rows(block["outside_detection"], "Outside-room detection", measured)
    if sid == S4:
        out += _event_rows(block["door_detection"], "Door", measured)
        out += _event_rows(block["interference_detection"], "Interference", measured)
    if sid == S5:
        out += _event_rows(block["moving_recall"], "Moving recall", measured)
        st = block["stationary_detection"]
        out += [f"| Stationary detection fraction (expected low) | {_v(st['fraction'], '{:.3f}')} |"]
    if sid == S6:
        fac = block["false_all_clear"]
        out += [f"| False all-clear fraction during disconnect (must be 0) | {_v(fac['fraction'], '{:.3f}')} |"]
    out.append("")
    if block.get("limitation"):
        out += [f"_Limitation:_ {block['limitation']}", ""]
    for w in block["warnings"]:
        out.append(f"- Warning: {_cell(w)}")
    if block["warnings"]:
        out.append("")
    return out


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable VALIDATION_REPORT document. Every value without data is
    written as NOT MEASURED."""
    lines = [
        "# RoomSense through-wall validation report",
        "",
        f"Generated: {_ts(report.get('generated_at_unix_ns'))}  ",
        f"Protocol: {report.get('protocol_version')}  ",
        f"Criteria: `{report['criteria']['file']}` (criteria_version `{report['criteria_version']}`)  ",
        f"Schema version: {report.get('schema_version')}",
        "",
        f"## Through-wall status: {report['through_wall_status']}",
        "",
        _cell(report["explanation"]),
        "",
    ]
    if report["unmet_requirements"]:
        lines += ["Unmet requirements:", ""] + [f"- {_cell(u)}" for u in report["unmet_requirements"]] + [""]

    lines += ["## Evidence levels (kept separate)", "", "| Level | Value | Basis |", "|---|---|---|"]
    for name, ev in report["evidence_levels"].items():
        val = ev["value"]
        shown = NOT_MEASURED if val is None else ("yes" if val else "no")
        lines.append(f"| {name} | {shown} | {_cell(ev['basis'])} |")
    lines.append("")

    cs = report.get("current_setup")
    lines += ["## Setup the status refers to", ""]
    if cs is None:
        lines += [f"{NOT_MEASURED}: no counted LIVE run.", ""]
    else:
        lines += [
            "| Field | Value |", "|---|---|",
            f"| Placement | {_cell(cs['placement']) or NOT_MEASURED} |",
            f"| Wall (as described by the operator) | {_cell(cs['wall_description']) or NOT_MEASURED} |",
            f"| Channel | {_v(cs['channel'])} |",
            f"| Counted runs | {cs['runs']} |",
            f"| Total run duration (h) | {cs['total_duration_s'] / 3600:.3f} |",
            f"| Links | {_cell(', '.join(cs['links'])) or NOT_MEASURED} |",
            f"| Calibration IDs | {_cell(', '.join(cs['calibration_ids'])) or NOT_MEASURED} |",
            "",
        ]

    lines += ["## Scenario results", ""]
    for sid in (s.scenario_id for s in SCENARIOS):
        lines += _scenario_md(report["scenarios"][sid])

    if report.get("setups"):
        lines += ["## Other setups (not the current one)", "", "| Setup | Placement | Wall | Channel | Runs | Status |",
                  "|---|---|---|---|---|---|"]
        for s in report["setups"]:
            lines.append(f"| {s['setup_key']} | {_cell(s['placement'])} | {_cell(s['wall_description'])} | "
                         f"{_v(s['channel'])} | {s['runs']} | {s['status']} |")
        lines.append("")

    lines += ["## Runs", ""]
    if not report["runs"]:
        lines += [f"{NOT_MEASURED}: no validation runs recorded.", ""]
    else:
        lines += ["| Run | Scenario | Mode | Status | Duration (s) | Channel | Counted | Why not |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in report["runs"]:
            why = "; ".join(r["exclusion_reasons"]) if r["exclusion_reasons"] else ""
            lines.append(
                f"| {r['run_id']} | {r['scenario_id']} | {r['source_mode']} | {r['status']} | "
                f"{_v(r['duration_s'], '{:.0f}')} | {_v(r['channel'])} | "
                f"{'yes' if r['counted_as_evidence'] else 'no'} | "
                f"{_cell(why)} |"
            )
        lines.append("")

    crit = report["criteria"]
    lines += [
        "## Predefined criteria",
        "",
        "Written before any data existed; they must not be lowered after seeing results.",
        "",
        f"- S1: at least {crit['s1_empty']['min_observation_hours']:g} h of empty-room decision time; false alarms "
        f"upper 95% bound <= {crit['s1_empty']['max_false_alarms_per_hour_upper95']:g} per hour",
        f"- S2: at least {crit['s2_moving']['min_events']} events; recall lower 95% bound >= "
        f"{crit['s2_moving']['min_recall_lower95']:g}",
        f"- S3: at least {crit['s3_outside']['min_events']} events; detection fraction >= "
        f"{crit['s3_outside']['not_distinguishable_detection_fraction']:g} means NOT_DISTINGUISHABLE",
        f"- Detection tolerance after event end: {crit['metrics']['detection_tolerance_s']:g} s; decision hold: "
        f"{crit['metrics']['max_sample_hold_s']:g} s",
        "",
        "## Notes",
        "",
    ]
    lines += [f"- {_cell(n)}" for n in report["notes"]]
    lines.append("")
    return "\n".join(lines)
