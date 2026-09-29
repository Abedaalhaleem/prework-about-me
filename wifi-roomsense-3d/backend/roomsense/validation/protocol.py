"""The through-wall validation protocol, as data.

The protocol answers one narrow question: *with the operator's own boards,
placement and wall, does the motion detector respond to one person moving in
the target room behind the wall, stay quiet when that room is empty, and can
its detections be told apart from movement near the sensors on the near side
of the wall?*

Every scenario is run with LIVE hardware only, in a space the operator
controls, with every person present informed and in agreement. Simulation and
replays are useful for software testing but never count as evidence.

Ground truth comes only from labelled events the operator enters while the run
is in progress (``START``/``END`` pairs with the labels listed per scenario,
or ``MARK`` for single moments). Nothing is inferred from the Wi-Fi data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "PROTOCOL_VERSION",
    "REQUIRED_METADATA",
    "ProtocolScenario",
    "SCENARIOS",
    "SCENARIO_IDS",
    "GENERAL_RULES",
    "get_scenario",
    "protocol_as_dict",
    "missing_run_metadata",
    "LABEL_MOVING",
    "LABEL_STILL",
    "LABEL_OUTSIDE_MOTION",
    "LABEL_DOOR",
    "LABEL_INTERFERENCE",
    "LABEL_DISCONNECT",
    "LABEL_DEGRADED",
]

PROTOCOL_VERSION = "through-wall-protocol-v1"

# Interval labels (used with START/END events). Matching is case-insensitive.
LABEL_MOVING = "MOVING"
LABEL_STILL = "STILL"
LABEL_OUTSIDE_MOTION = "OUTSIDE_MOTION"
LABEL_DOOR = "DOOR"
LABEL_INTERFERENCE = "INTERFERENCE"
LABEL_DISCONNECT = "DISCONNECT"
LABEL_DEGRADED = "DEGRADED"

# Metadata every run must carry. "duration" comes from the run's start/end
# times and "labelled_events" from the events table; the rest are fields of
# ValidationRun that the operator fills in.
REQUIRED_METADATA: tuple[str, ...] = (
    "placement",
    "wall_description",
    "channel",
    "duration",
    "labelled_events",
    "conditions",
)

REQUIRED_METADATA_HELP: dict[str, str] = {
    "placement": "Where each TX and RX is mounted (height, distance to the wall) and which side of the wall it is on.",
    "wall_description": "Wall material and thickness as you know or measured them (e.g. 'drywall on studs, ~12 cm'). "
    "Say 'unknown' rather than guessing.",
    "channel": "The Wi-Fi channel the boards used (from RSHELLO or your firmware configuration).",
    "duration": "Recorded automatically from the run's start and stop times.",
    "labelled_events": "START/END label events entered while the run is in progress (labels listed per scenario).",
    "conditions": "Anything that could matter: other people or pets nearby, fans, open windows, other Wi-Fi load, "
    "time of day.",
}

GENERAL_RULES: tuple[str, ...] = (
    "Run only in a space you control, with every person present informed and agreeing. No names are recorded.",
    "Use LIVE hardware. SIMULATION runs and replays are listed in the report but never count as evidence.",
    "Do not change placement, wall, channel, firmware or detection thresholds between runs you want to combine; "
    "the report groups runs by placement, wall description and channel.",
    "Record a fresh quiet baseline (calibration) before the runs and keep it for the whole series.",
    "Enter label events at the moment things happen. Label from what you observe, never from the RoomSense display.",
    "Decide the criteria (configs/through_wall_criteria.toml) before collecting data and do not lower them "
    "after seeing results. The report records the criteria hash (criteria_version).",
    "UNKNOWN and SENSOR_OFFLINE time is reported separately and is not counted as decision time.",
)


@dataclass(frozen=True)
class ProtocolScenario:
    scenario_id: str
    title: str
    purpose: str
    instructions: tuple[str, ...]
    required_metadata: tuple[str, ...]
    min_duration_s: float
    interval_labels: tuple[str, ...]  # START/END labels this scenario uses
    measured: tuple[str, ...]
    pass_fail_meaning: str
    counts_toward_status: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, tuple):
                d[k] = list(v)
        return d


SCENARIOS: tuple[ProtocolScenario, ...] = (
    ProtocolScenario(
        scenario_id="S1_EMPTY_TARGET_ROOM",
        title="Empty target room, no nearby activity",
        purpose="Estimate the false-alarm rate when nobody is in the target room and nobody moves near the sensors.",
        instructions=(
            "Make sure the target room behind the wall is empty (people and pets) and its door is closed.",
            "Nobody moves near the transmitter or receivers. Leave the area or stay still well away from them.",
            "Start the run, wait at least the minimum duration, then stop it.",
            "If someone enters the target room or walks near the sensors, stop the run and start a new one.",
            "Collect several runs at different times of day until the total empty observation time reaches the "
            "criteria minimum.",
        ),
        required_metadata=REQUIRED_METADATA,
        min_duration_s=1800.0,
        interval_labels=(),
        measured=(
            "Decision time (NO_MOTION_DETECTED + MOTION_DETECTED), with UNKNOWN / SENSOR_OFFLINE time reported "
            "separately.",
            "False alarms: MOTION_DETECTED onsets during the run, per hour of decision time, with an exact "
            "Poisson 95% confidence interval.",
        ),
        pass_fail_meaning="Passes when the total decision time reaches the minimum hours and the upper 95% bound of "
        "the false-alarm rate is at or below the criteria maximum. Failing means the detector alarms too often "
        "in an empty room for through-wall use in this setup.",
        counts_toward_status=True,
    ),
    ProtocolScenario(
        scenario_id="S2_PERSON_MOVING_BEHIND_WALL",
        title="One person moving inside the target room, behind the wall",
        purpose="Measure event recall and detection latency for one moving person on the far side of the wall.",
        instructions=(
            "Exactly one consenting person is in the target room. Everyone else stays away from the sensors.",
            "Between events the person stands completely still (or leaves the room) for at least 20 s so the "
            "state can settle.",
            f"Enter START '{LABEL_MOVING}' when the person starts walking and END '{LABEL_MOVING}' when they stop. "
            "Vary paths, speed and distance to the wall across events.",
            "Aim for at least the minimum number of events in the criteria, spread over several runs.",
        ),
        required_metadata=REQUIRED_METADATA,
        min_duration_s=600.0,
        interval_labels=(LABEL_MOVING,),
        measured=(
            "Event recall: events with a MOTION_DETECTED onset between the event start and the event end plus the "
            "tolerance, with a Wilson 95% confidence interval.",
            "Detection latency per event (median and 90th percentile).",
            "Missed events, including events with no decision time (counted as missed).",
        ),
        pass_fail_meaning="Passes when there are at least the minimum number of events and the lower 95% bound of "
        "recall is at or above the criteria minimum. Failing means moving people behind this wall are missed too "
        "often.",
        counts_toward_status=True,
    ),
    ProtocolScenario(
        scenario_id="S3_MOTION_NEAR_SENSORS_OUTSIDE",
        title="Movement only near TX/RX, outside the target room",
        purpose="Check whether detections could come from the near side of the wall instead of the target room.",
        instructions=(
            "The target room is empty. One consenting person moves near the transmitter and receivers on the near "
            "side of the wall (outside the target room).",
            f"Enter START '{LABEL_OUTSIDE_MOTION}' / END '{LABEL_OUTSIDE_MOTION}' around each movement, with at "
            "least 20 s of stillness between events.",
            "Use movements similar in size and speed to the S2 events.",
        ),
        required_metadata=REQUIRED_METADATA,
        min_duration_s=600.0,
        interval_labels=(LABEL_OUTSIDE_MOTION,),
        measured=(
            "Outside-room detection fraction: events with a MOTION_DETECTED onset (same rule as S2), with a Wilson "
            "95% confidence interval.",
        ),
        pass_fail_meaning="If outside movement is detected at a rate comparable to S2 (detection fraction at or "
        "above the criteria threshold), detections cannot be attributed to the target room and the through-wall "
        "status is NOT_DISTINGUISHABLE, regardless of S1/S2.",
        counts_toward_status=True,
    ),
    ProtocolScenario(
        scenario_id="S4_DOOR_AND_INTERFERENCE",
        title="Door movement and ordinary environmental interference",
        purpose="Show how the detector reacts to doors, appliances and other everyday disturbances with no person "
        "moving in the target room.",
        instructions=(
            "Nobody moves inside the target room.",
            f"Open/close a door and enter START/END '{LABEL_DOOR}' around each movement.",
            f"Switch ordinary interference on/off (fan, microwave, a large file transfer on another device you own) "
            f"and enter START/END '{LABEL_INTERFERENCE}' around it.",
        ),
        required_metadata=REQUIRED_METADATA,
        min_duration_s=600.0,
        interval_labels=(LABEL_DOOR, LABEL_INTERFERENCE),
        measured=(
            "Detection fraction for door events and for interference intervals (Wilson 95% CI).",
            "MOTION_DETECTED onsets per hour over the whole run.",
        ),
        pass_fail_meaning="Informational. High detection fractions mean door or interference events will look "
        "like motion in this setup; the report states this next to the status.",
        counts_toward_status=False,
    ),
    ProtocolScenario(
        scenario_id="S5_STILL_AFTER_MOVING",
        title="Person stands still after moving",
        purpose="Document the known limitation that a motionless person is usually not detected.",
        instructions=(
            "One consenting person in the target room walks, then stands or sits completely still.",
            f"Enter START/END '{LABEL_MOVING}' around the walking and START/END '{LABEL_STILL}' around the still "
            "period (at least 60 s).",
        ),
        required_metadata=REQUIRED_METADATA,
        min_duration_s=600.0,
        interval_labels=(LABEL_MOVING, LABEL_STILL),
        measured=(
            "Stationary-person detection fraction: share of decision time inside STILL intervals (after a settle "
            "time) that is MOTION_DETECTED. Expected to be low.",
            "Recall for the MOVING intervals (same rule as S2).",
        ),
        pass_fail_meaning="Informational. A low stationary detection fraction is the expected limitation, not a "
        "failure: NO_MOTION_DETECTED never means the room is empty.",
        counts_toward_status=False,
    ),
    ProtocolScenario(
        scenario_id="S6_DISCONNECT_AND_DEGRADED",
        title="Receiver unplugged and degraded signal",
        purpose="Check that lost or degraded data is reported as SENSOR_OFFLINE / UNKNOWN and never as "
        "NO_MOTION_DETECTED.",
        instructions=(
            f"Unplug one receiver's USB cable, wait at least 30 s, plug it back in. Enter START/END "
            f"'{LABEL_DISCONNECT}' around it.",
            f"Degrade the signal (for example cover an antenna with your hand or move a receiver far away) and "
            f"enter START/END '{LABEL_DEGRADED}' around it.",
        ),
        required_metadata=REQUIRED_METADATA,
        min_duration_s=300.0,
        interval_labels=(LABEL_DISCONNECT, LABEL_DEGRADED),
        measured=(
            "Time per state inside DISCONNECT and DEGRADED intervals, per link and combined.",
            "False all-clear fraction: share of DISCONNECT time (after the grace period) that the any-link "
            "combination reports as NO_MOTION_DETECTED. Must be 0.",
        ),
        pass_fail_meaning="Informational safety check. Any false all-clear is reported as a defect.",
        counts_toward_status=False,
    ),
)

SCENARIO_IDS: tuple[str, ...] = tuple(s.scenario_id for s in SCENARIOS)
_BY_ID = {s.scenario_id: s for s in SCENARIOS}


def get_scenario(scenario_id: str) -> ProtocolScenario | None:
    return _BY_ID.get(scenario_id)


def protocol_as_dict() -> dict[str, Any]:
    """JSON-ready protocol for ``GET /api/validation/protocol``."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "general_rules": list(GENERAL_RULES),
        "required_metadata": [{"field": f, "help": REQUIRED_METADATA_HELP[f]} for f in REQUIRED_METADATA],
        "scenarios": [s.to_dict() for s in SCENARIOS],
    }


def missing_run_metadata(run: Any, n_labelled_events: int | None = None) -> list[str]:
    """Names of required metadata a validation run lacks.

    ``run`` is a :class:`~roomsense.storage.models.ValidationRun` (duck-typed to
    keep this module free of storage imports). ``labelled_events`` is only
    checked for scenarios that use interval labels and when a count is given.
    """
    missing: list[str] = []
    if not str(getattr(run, "placement", "") or "").strip():
        missing.append("placement")
    if not str(getattr(run, "wall_description", "") or "").strip():
        missing.append("wall_description")
    if getattr(run, "channel", None) is None:
        missing.append("channel")
    if getattr(run, "ended_at_unix_ns", None) is None:
        missing.append("duration")
    if not str(getattr(run, "conditions", "") or "").strip():
        missing.append("conditions")
    scenario = get_scenario(str(getattr(run, "scenario_id", "")))
    if scenario is not None and scenario.interval_labels and n_labelled_events is not None and n_labelled_events == 0:
        missing.append("labelled_events")
    return missing
