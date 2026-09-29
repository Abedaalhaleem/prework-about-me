"""Builders for storage/validation tests.

Everything here is hand-constructed test data. Frames flagged LIVE below are
*not* measurements: they only exercise bookkeeping code paths (the mode value
is what the storage layer filters on). Nothing produced here says anything
about sensing accuracy.
"""

from __future__ import annotations

from typing import Sequence

from roomsense.config import StorageConfig
from roomsense.schemas import CsiFrame, DeviceIdentity, InputFormat, QualityFlag, SourceMode
from roomsense.storage.db import Database
from roomsense.storage.models import (
    ActivityLogEntry,
    ConsentRecord,
    EventKind,
    LabeledEvent,
    ValidationRun,
    ValidationRunStatus,
)

NS = 1_000_000_000
T0 = 1_780_000_000 * NS  # fixed epoch for deterministic tests
LAYOUT_ID = "classic.lltf_only.sec_none.total128.LLTF64"


def make_consent(**overrides: object) -> ConsentRecord:
    data: dict[str, object] = {
        "all_participants_consented": True,
        "participant_count": 1,
        "purpose": "software test of the recorder",
    }
    data.update(overrides)
    return ConsentRecord(**data)  # type: ignore[arg-type]


def storage_cfg(**overrides: object) -> StorageConfig:
    return StorageConfig(**overrides)  # type: ignore[arg-type]


def make_frame(
    *,
    session_id: str = "sess_live",
    source_mode: SourceMode = SourceMode.LIVE,
    link_id: str = "tx1->rx1",
    counter: int = 0,
    t_ns: int | None = None,
    synthetic: bool = False,
    raw_line: str | None = None,
) -> CsiFrame:
    """A structurally valid CsiFrame with deterministic int8 values."""
    tx, rx = link_id.split("->")
    flags: tuple[str, ...] = (QualityFlag.SYNTHETIC.value,) if synthetic else ()
    raw = tuple(((i * 7 + counter) % 256) - 128 for i in range(128))
    host = (T0 + counter * NS // 25) if t_ns is None else t_ns
    return CsiFrame(
        source_mode=source_mode,
        session_id=session_id,
        receiver_id=rx,
        transmitter_id=tx,
        link_id=link_id,
        input_format=InputFormat.SYNTHETIC_V1 if synthetic else InputFormat.ROOMSENSE_RSCSI_V1,
        device=DeviceIdentity(receiver_id=rx, chip="esp32s3",
                              identity_source="synthetic" if synthetic else "firmware_hello"),
        frame_counter=counter,
        frame_counter_unwrapped=counter,
        transmitter_counter=None,
        device_timestamp_us=counter * 40_000,
        device_timestamp_unwrapped_us=counter * 40_000,
        host_arrival_monotonic_ns=counter * NS // 25,
        host_arrival_unix_ns=host,
        transmitter_mac="1a:00:00:00:00:01",
        channel=6,
        secondary_channel=0,
        bandwidth_mhz=20,
        sig_mode=0,
        bb_format=None,
        mcs=0,
        rate=11,
        stbc=0,
        rssi_dbm=-55,
        noise_floor_dbm=None,
        agc_gain=None,
        fft_gain=None,
        sig_len=100,
        rx_state=0,
        antenna=0,
        first_word_invalid=False,
        csi_len=128,
        raw_csi=raw,
        values_are_gain_compensated=False,
        layout_id=LAYOUT_ID,
        valid_subcarriers=tuple(k for k in range(-26, 27) if k != 0),
        quality_flags=flags,
        raw_line=raw_line,
    )


# ---------------------------------------------------------------------------
# Validation fixtures: decision timelines, runs and label events
# ---------------------------------------------------------------------------

MOTION = "MOTION_DETECTED"
QUIET = "NO_MOTION_DETECTED"
UNKNOWN = "UNKNOWN"
OFFLINE = "SENSOR_OFFLINE"


def add_timeline(
    db: Database,
    *,
    session_id: str,
    link_id: str,
    start_ns: int,
    pieces: Sequence[tuple[float, str]],
    step_s: float = 0.5,
    source_mode: SourceMode = SourceMode.LIVE,
    calibration_id: str | None = "cal_test",
) -> int:
    """Log one activity row every ``step_s`` following ``pieces`` of
    ``(duration_s, state)``. Returns the end time in ns."""
    rows: list[ActivityLogEntry] = []
    t = start_ns
    step = int(step_s * NS)
    for dur, state in pieces:
        end = t + int(dur * NS)
        while t < end:
            rows.append(ActivityLogEntry(session_id=session_id, link_id=link_id, t_end_unix_ns=t, state=state,
                                         score=None, quality_level="GOOD", calibration_id=calibration_id,
                                         source_mode=source_mode))
            t += step
    db.add_activity_many(rows)
    return t


def add_run(
    db: Database,
    *,
    scenario_id: str,
    session_id: str,
    start_ns: int,
    end_ns: int | None,
    source_mode: SourceMode = SourceMode.LIVE,
    placement: str = "TX in hallway 1.0 m from wall; RX in hallway 1.5 m from wall; target room behind wall",
    wall_description: str = "operator: plasterboard on studs, about 12 cm",
    channel: int | None = 6,
    conditions: str = "evening, no pets, window closed",
    status: ValidationRunStatus = ValidationRunStatus.COMPLETE,
    recording_id: str | None = None,
    link_ids: Sequence[str] = (),
    run_id: str | None = None,
) -> ValidationRun:
    kwargs: dict[str, object] = {}
    if run_id is not None:
        kwargs["run_id"] = run_id
    run = ValidationRun(
        scenario_id=scenario_id, session_id=session_id, recording_id=recording_id, source_mode=source_mode,
        started_at_unix_ns=start_ns, ended_at_unix_ns=end_ns, placement=placement,
        wall_description=wall_description, channel=channel, conditions=conditions, status=status,
        link_ids=list(link_ids), **kwargs,  # type: ignore[arg-type]
    )
    db.ensure_session(session_id, source_mode)
    return db.add_validation_run(run)


def add_interval(db: Database, *, session_id: str, label: str, start_ns: int, end_ns: int) -> None:
    db.add_event(LabeledEvent(session_id=session_id, t_unix_ns=start_ns, kind=EventKind.START, label=label))
    db.add_event(LabeledEvent(session_id=session_id, t_unix_ns=end_ns, kind=EventKind.END, label=label))


def add_event_series(
    db: Database,
    *,
    session_id: str,
    link_id: str,
    start_ns: int,
    label: str,
    n_events: int,
    detected: Sequence[bool],
    event_s: float = 10.0,
    gap_s: float = 20.0,
    latency_s: float = 2.0,
    source_mode: SourceMode = SourceMode.LIVE,
) -> int:
    """``n_events`` labelled intervals separated by quiet gaps. Detected events
    switch the link to MOTION ``latency_s`` after the START label. Returns the
    end time (ns) of the series."""
    t = start_ns
    for i in range(n_events):
        # quiet gap before each event
        t = add_timeline(db, session_id=session_id, link_id=link_id, start_ns=t, pieces=[(gap_s, QUIET)],
                         source_mode=source_mode)
        ev_start = t
        if detected[i]:
            pieces = [(latency_s, QUIET), (event_s - latency_s, MOTION)]
        else:
            pieces = [(event_s, QUIET)]
        t = add_timeline(db, session_id=session_id, link_id=link_id, start_ns=t, pieces=pieces,
                         source_mode=source_mode)
        add_interval(db, session_id=session_id, label=label, start_ns=ev_start, end_ns=t)
    return add_timeline(db, session_id=session_id, link_id=link_id, start_ns=t, pieces=[(gap_s, QUIET)],
                        source_mode=source_mode)
