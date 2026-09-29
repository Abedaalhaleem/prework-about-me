# Validation report

> **Through-wall detection: UNVERIFIED.** No real measurement of any kind has
> been made yet. There is no ESP32 hardware in the build environment, so every
> hardware-dependent result below is **NOT MEASURED**. Nothing in this
> repository shows that RoomSense detects motion behind a wall, or even in the
> same room, with real signals.

## What has actually been verified

| Level | Status | Evidence |
|---|---|---|
| Software tested | **Yes, on synthetic and fixture data only** | Automated tests executed in the build container: backend `pytest`, frontend `vitest` plus typecheck and build, and firmware host tests (`make -C firmware/esp32/host_tests test`). See `PROGRESS_LOG.md` for the exact counts. These tests show that code paths behave as specified: parsing, gap handling, state machine, gates, API rules. They say **nothing** about sensing accuracy. |
| Firmware compiled | **No** | The ESP-IDF toolchain could not be downloaded in the build container. Only host unit tests of the firmware core and a header-level syntax check were run. See `firmware/esp32/README.md`. |
| Hardware tested | **No** | No ESP32 board was available. |
| Through-wall validated | **No** | Requires the protocol below, run with real hardware in your own layout. |

A same-room walk test is **not** evidence of behind-wall performance.
Simulated runs, replays and synthetic recordings are listed by the report but
**never** count as evidence (this is enforced in code:
`backend/roomsense/validation/report.py`).

## Protocol for your layout

The protocol is served by the app (`GET /api/validation/protocol`, the
**Validation** page), which also shows the step-by-step instructions. Each run
records:

* the placement (where the TX and RX are, and which side of the wall each is on)
* the wall description (material and thickness, as you describe them)
* the Wi-Fi channel, conditions and notes
* the labelled events (START/END/MARK) you enter while the run is going

Duration and per-window decisions come from the activity log.

| Scenario | What you do | Minimum run | What it measures | Counts toward status |
|---|---|---|---|---|
| S1 Empty target room | Nobody in the target room, nobody moving near the sensors | 30 min per run; total ≥ 2 h decision time, and in practice ≥ ~3.7 h to pass | False alarms per hour of decision time, with an exact Poisson 95% CI | yes |
| S2 Person moving behind the wall | One consenting person walks in the target room. You label each `MOVING` START/END | 10 min; ≥ 20 events in total | Event recall (Wilson 95% CI), detection latency, missed events | yes |
| S3 Motion near the sensors, outside the room | Target room empty. One person moves near TX/RX on the near side. You label `OUTSIDE_MOTION` | 10 min; ≥ 10 events | Outside-room detection fraction. If it is ≥ 0.5, the status is **NOT_DISTINGUISHABLE** | yes |
| S4 Door and interference | Door movements (`DOOR`) and everyday interference (`INTERFERENCE`), with nobody moving in the room | 10 min | Detection fraction per disturbance; motion onsets per hour | informational |
| S5 Still after moving | Walk (`MOVING`), then stand still for ≥ 60 s (`STILL`) | 10 min | Stationary detection fraction. It is **expected to be low**: a motionless person can go undetected | informational |
| S6 Disconnect and degraded | Unplug a receiver (`DISCONNECT`); cover or move an antenna (`DEGRADED`) | 5 min | Time per state. The false all-clear fraction must be 0 | informational |

The criteria were written before any data existed and must not be lowered
after seeing results (`configs/through_wall_criteria.toml`; their hash is the
`criteria_version`). The status can take three values:

* **VALIDATED**, only when S1, S2 and S3 meet the criteria on LIVE runs of the same setup.
* **NOT_DISTINGUISHABLE**, when motion outside the target room triggers
  detections as readily as motion inside it. The UI then says that target-room
  and outside-room motion cannot be distinguished.
* **UNVERIFIED** otherwise.

## Regenerating this report

The app computes the report from your own LIVE runs:

```bash
cd backend && uv run roomsense validate-report --markdown > ../VALIDATION_REPORT.local.md
```

The **Validation** page shows the same report, with a markdown download.

---

## Report as generated from an empty database (2026-09-29, build container)

### RoomSense through-wall validation report

Generated: 2026-09-29 04:39:38 UTC  
Protocol: through-wall-protocol-v1  
Criteria: `through_wall_criteria.toml` (criteria_version `e76147a9ab732095`)  
Schema version: 1.0.0

#### Through-wall status: UNVERIFIED

No LIVE validation run counts as evidence yet, so through-wall detection is UNVERIFIED. Simulation, replays and incomplete runs never count.

Unmet requirements:

- S1 (empty room): NOT MEASURED
- S2 (person moving behind wall): NOT MEASURED
- S3 (movement near sensors outside the room): NOT MEASURED

#### Evidence levels (kept separate)

| Level | Value | Basis |
|---|---|---|
| software_tested | NOT MEASURED | Automated tests on synthetic and fixture data. They show that code paths run, not that sensing works. This report does not run them. |
| hardware_tested | no | True only if at least one LIVE validation run logged motion decisions from real receivers. |
| through_wall_validated | no | Predefined criteria e76147a9ab732095 evaluated on LIVE runs of the current setup. |

#### Setup the status refers to

NOT MEASURED: no counted LIVE run.

#### Scenario results

##### S1_EMPTY_TARGET_ROOM: Empty target room, no nearby activity

**NOT MEASURED** (no counted LIVE runs).

| Metric | Value |
|---|---|
| Counted runs | 0 |
| Run duration (h) | NOT MEASURED |
| Decision time (h) | NOT MEASURED |
| UNKNOWN / CALIBRATING / OFFLINE / no-data time (h, excluded from rates) | NOT MEASURED |
| False alarms (count) | NOT MEASURED |
| False alarms per hour of decision time | NOT MEASURED |
| Exact Poisson 95% CI (per hour) | NOT MEASURED |

##### S2_PERSON_MOVING_BEHIND_WALL: One person moving inside the target room, behind the wall

**NOT MEASURED** (no counted LIVE runs).

| Metric | Value |
|---|---|
| Counted runs | 0 |
| Run duration (h) | NOT MEASURED |
| Decision time (h) | NOT MEASURED |
| UNKNOWN / CALIBRATING / OFFLINE / no-data time (h, excluded from rates) | NOT MEASURED |
| Recall: events (total / carried over) | NOT MEASURED |
| Recall: detected / usable | NOT MEASURED |
| Recall: detection fraction | NOT MEASURED |
| Recall: Wilson 95% CI | NOT MEASURED |
| Recall: latency median / p90 (s) | NOT MEASURED |
| Recall: missed events | NOT MEASURED |

##### S3_MOTION_NEAR_SENSORS_OUTSIDE: Movement only near TX/RX, outside the target room

**NOT MEASURED** (no counted LIVE runs).

| Metric | Value |
|---|---|
| Counted runs | 0 |
| Run duration (h) | NOT MEASURED |
| Decision time (h) | NOT MEASURED |
| UNKNOWN / CALIBRATING / OFFLINE / no-data time (h, excluded from rates) | NOT MEASURED |
| Outside-room detection: events (total / carried over) | NOT MEASURED |
| Outside-room detection: detected / usable | NOT MEASURED |
| Outside-room detection: detection fraction | NOT MEASURED |
| Outside-room detection: Wilson 95% CI | NOT MEASURED |
| Outside-room detection: latency median / p90 (s) | NOT MEASURED |
| Outside-room detection: missed events | NOT MEASURED |

##### S4_DOOR_AND_INTERFERENCE: Door movement and ordinary environmental interference

**NOT MEASURED** (no counted LIVE runs).

| Metric | Value |
|---|---|
| Counted runs | 0 |
| Run duration (h) | NOT MEASURED |
| Decision time (h) | NOT MEASURED |
| UNKNOWN / CALIBRATING / OFFLINE / no-data time (h, excluded from rates) | NOT MEASURED |
| Motion onsets (count) | NOT MEASURED |
| Motion onsets per hour of decision time | NOT MEASURED |
| Exact Poisson 95% CI (per hour) | NOT MEASURED |
| Door: events (total / carried over) | NOT MEASURED |
| Door: detected / usable | NOT MEASURED |
| Door: detection fraction | NOT MEASURED |
| Door: Wilson 95% CI | NOT MEASURED |
| Door: latency median / p90 (s) | NOT MEASURED |
| Door: missed events | NOT MEASURED |
| Interference: events (total / carried over) | NOT MEASURED |
| Interference: detected / usable | NOT MEASURED |
| Interference: detection fraction | NOT MEASURED |
| Interference: Wilson 95% CI | NOT MEASURED |
| Interference: latency median / p90 (s) | NOT MEASURED |
| Interference: missed events | NOT MEASURED |

##### S5_STILL_AFTER_MOVING: Person stands still after moving

**NOT MEASURED** (no counted LIVE runs).

| Metric | Value |
|---|---|
| Counted runs | 0 |
| Run duration (h) | NOT MEASURED |
| Decision time (h) | NOT MEASURED |
| UNKNOWN / CALIBRATING / OFFLINE / no-data time (h, excluded from rates) | NOT MEASURED |
| Moving recall: events (total / carried over) | NOT MEASURED |
| Moving recall: detected / usable | NOT MEASURED |
| Moving recall: detection fraction | NOT MEASURED |
| Moving recall: Wilson 95% CI | NOT MEASURED |
| Moving recall: latency median / p90 (s) | NOT MEASURED |
| Moving recall: missed events | NOT MEASURED |
| Stationary detection fraction (expected low) | NOT MEASURED |

_Limitation:_ A motionless person is usually NOT detected. A low stationary detection fraction is expected; NO_MOTION_DETECTED never means the room is empty.

##### S6_DISCONNECT_AND_DEGRADED: Receiver unplugged and degraded signal

**NOT MEASURED** (no counted LIVE runs).

| Metric | Value |
|---|---|
| Counted runs | 0 |
| Run duration (h) | NOT MEASURED |
| Decision time (h) | NOT MEASURED |
| UNKNOWN / CALIBRATING / OFFLINE / no-data time (h, excluded from rates) | NOT MEASURED |
| False all-clear fraction during disconnect (must be 0) | NOT MEASURED |

#### Runs

NOT MEASURED: no validation runs recorded.

#### Predefined criteria

Written before any data existed; they must not be lowered after seeing results.

- S1: at least 2 h of empty-room decision time; false alarms upper 95% bound <= 1 per hour
- S2: at least 20 events; recall lower 95% bound >= 0.8
- S3: at least 10 events; detection fraction >= 0.5 means NOT_DISTINGUISHABLE
- Detection tolerance after event end: 3 s; decision hold: 5 s

#### Notes

- Only LIVE runs count. SIMULATION and REPLAY runs, synthetic recordings and incomplete runs are listed but excluded from evidence.
- Decision time is MOTION_DETECTED + NO_MOTION_DETECTED under the any-link OR combination; UNKNOWN, SENSOR_OFFLINE and missing data are reported separately and excluded from every rate.
- Activity scores are heuristic deviations from a quiet baseline, not probabilities.
- A motionless person can remain undetected. This is not a people counter and performs no identification.
