# Calibration

Calibration covers two things:

* a **quiet baseline** for motion detection (capability B)
* an optional, **experimental** zone model (capability C)

Both are tied to the exact hardware, channel, firmware, room geometry and processing
configuration they were recorded under. When RoomSense notices a change in any of
those, they are invalidated automatically.

A quiet baseline also applies **only to the source session in which it was
recorded**. Stopping or switching the source, or restarting the server, starts a
new session without a baseline (`NO_BASELINE`), and you record a new one. Earlier
baselines stay in the calibration history but are never re-applied automatically.

> Use RoomSense only in spaces you control, and only with every participant's consent.
> Recording is opt-in and stays on this computer.

## 0. Before you start

1. The boards are mounted in their final positions (see `docs/PLACEMENT.md`).
   Moving a board afterwards makes the calibration meaningless, so record a new
   baseline. RoomSense cannot see a move directly: it notices one only when you
   update the board's position in the room model, or indirectly when the static
   channel changes (`STATIC_CHANNEL_CHANGED`, below).
2. The live source is running (Dashboard → Source → Live), and the header shows
   **LIVE MEASUREMENTS**. Simulation can walk you through the steps. A baseline made
   from simulated data is labelled SIMULATION, and it can never enable anything for
   live use.
3. The measured acquisition rate for each link is stable (Dashboard → Links).

## 1. Room geometry (wizard: Calibration → steps 1–6)

You enter the room in metres. The coordinates are x = east, y = north, and z = height
above the floor.

* Room width, depth and height; walls (start, end, thickness, material as *you*
  describe it); doors.
* Sensor nodes: role (TX / RX / ROUTER), position x, y, z (mounting height), and whether
  each one is inside the target room.
* Links: which transmitter each receiver listens to (e.g. `tx1->rx1`).
* Zones: floor polygons inside the target room (for capability C only).

This geometry is always labelled **USER PROVIDED**. RoomSense never reconstructs
geometry from Wi-Fi. The sample room is labelled **EXAMPLE**.

When you save, RoomSense computes a room configuration hash. If it differs from the
previous one, all calibrations and zone models tied to the old hash become invalid, and
the UI lists what was invalidated.

## 2. Quiet baseline (required for motion detection)

1. Make sure nobody is in the target room, and that there is as little activity as
   possible nearby. Close doors you will not be using.
2. Calibration → step 7 *Quiet baseline* → tick **"I confirm the target room is empty
   and quiet"** → *Start quiet baseline*. The start is refused without that
   confirmation (API: `POST /api/calibration/baseline/start` needs
   `"confirm_room_empty": true`, otherwise 422 `ROOM_NOT_CONFIRMED_EMPTY`) and
   without a running source.
3. Leave it running for at least `detection.baseline_min_duration_s` (default 60 s,
   from the first window start to the last window end) and
   `detection.baseline_min_windows` (default 30) usable windows. Longer is better.
4. Press *Stop & evaluate*. The baseline is **rejected**, with one or more reasons, when:
   * it is too short (`INSUFFICIENT_DURATION`) or has too few usable windows
     (`INSUFFICIENT_WINDOWS`, or `NO_USABLE_WINDOWS`)
   * more than half of its windows were skipped for low quality
     (`LOW_QUALITY_DURING_CALIBRATION`)
   * more than 10 % of its windows deviate from a baseline built from the same
     windows by at least `detection.enter_threshold`, in either direction
     (`BASELINE_UNSTABLE`: movement during calibration is likely)
   * the static channel profile shifted during the recording (`BASELINE_UNSTABLE`)
   * the CSI layout or the link identity changed during the recording
     (`LAYOUT_CHANGED`, `HARDWARE_SIGNATURE_CHANGED`)

   A rejected baseline is never applied. Record it again.

What the baseline records: the median and robust spread of each window feature
(amplitude variation, temporal differences), a per-subcarrier profile, the calibration
ID, the hardware signature, the config version, and the CSI layout.

The detector **never** folds new data into the baseline. When the static channel
changes for longer than `detection.drift_hold_s` (default 30 s; for example because a
board moved, furniture moved, or someone is standing still), the link reports
**UNKNOWN** with reason `STATIC_CHANNEL_CHANGED`. Recalibrate only when the room
really is empty.

Within a session, a link's baseline is invalidated automatically (state `UNKNOWN`,
reason `CALIBRATION_INVALID`) when the link reports a different:

* channel or secondary channel (bandwidth)
* CSI layout
* chip, firmware name or firmware version
* transmitter MAC

It is also invalidated when you save room geometry with a different room
configuration hash. Changing `[processing]` or `[detection]` in the config changes
the config version; that needs a server restart, which starts a new session without
a baseline anyway.

## 3. Walk test

1. Record a valid quiet baseline first, in the same session. Without one, every
   window is undecided and the report says `NO_DECISION_WINDOWS`.
2. Calibration → step 8 *Walk test* → *Start walk test*.
3. One person walks slowly through the target room for 30–60 s, crossing each
   sensing link.
4. Press *Stop & show results*. For each link the report shows the maximum score
   (a heuristic, not a probability), the fraction of decided windows in motion, the
   window counts (motion / undecided / offline), and whether `MOTION_DETECTED` was
   reported at least once.

A walk test in the **same room** is not evidence of **through-wall** performance. That
needs the protocol in `VALIDATION_REPORT.md`.

## 4. Zone data collection (capability C, experimental)

Zone estimation stays **DISABLED** until a model passes the predefined criteria in
`configs/zone_enablement.toml`. Those criteria were written before any data existed,
and they must not be lowered after seeing results.

Requirements:

* One transmitter and at least 3 spatially separated receivers (the larger of
  `min_receivers` in `configs/zone_enablement.toml` and `zone.min_receivers` in the
  config; both are 3). The same links must be present at runtime.
* A saved **USER PROVIDED** room geometry with at least 2 target-room zones
  (step 5). A model trained with the EXAMPLE room can never be enabled.
* One moving participant. It is not a people counter, and it does not track stationary people.
* Real measurements only. A simulated session, or a recording of a replay of
  simulated data, makes the model ineligible whatever its scores.

For each class, record **separate sessions** with the Live source running (Recordings
& Replay → Record: complete the consent form → *Start recording*). Give each
recording a label you can map back to its class.

| Class | What to do |
|---|---|
| each zone id (e.g. `A`, `B`, `C`) | One person moves inside that zone only: walking in small loops, turning, different orientations, arm movements. |
| `EMPTY` | Nobody in the target room, and little nearby activity. |
| `OUTSIDE_TARGET_ROOM` | One person moving **outside** the target room, near the transmitter or receivers. |

Collect at least:

* 3 training sessions per class
* 1 validation session per class
* 1 test session per class, with at least 30 usable test windows per class

The first and last 5 s of every session are trimmed. Record the test sessions
**last**: the criteria require every test session to start after every training
and validation session (of every class) has ended. A different day is better
still. Vary the orientation and movement style.

Training is **not** in the UI. Use one of:

* while the server runs: `POST /api/zone/train` with
  `{"sessions": [{"recording_id": "...", "label": "A", "split": "train"}, ...]}`
* with the server stopped: `cd backend && uv run roomsense zone-train --sessions sessions.json`
  (`--sessions` takes a JSON file or the JSON text itself: a list of the same
  objects, or `{"sessions": [...]}`)

`label` is a zone id, `EMPTY` or `OUTSIDE_TARGET_ROOM`. `split` (`train`,
`validation` or `test`) is optional, but either every session names it or none
does. Without it, each class's sessions are ordered by start time: the earliest
train, the next validate, the latest test. The status and the last report appear
under Capabilities & Research → *C · Zone estimation*, and at `GET /api/zone/status`.

* The data is split **by whole session**. A session is never split across
  train, validation and test, because overlapping windows would leak between them.
* Normalisation and feature selection are fitted on the **training** sessions only.
* The abstention threshold is tuned on **validation** sessions and then frozen.
* The **test** sessions are evaluated once.

The report lists:

* sample (window) counts and session counts per class
* class balance
* the confusion matrix
* per-zone errors
* the abstention rate
* EMPTY- and OUTSIDE-predicted-as-zone rates
* per-test-session accuracy (later sessions)
* the criteria version

If the criteria pass, the model is enabled. It is bound to the hardware signature, the
room hash, the link set, the config version and the criteria version. If any of those
change, the model goes back to DISABLED. Deleting a recording also deletes every zone
model trained on it.

When the zone model is enabled, the UI highlights the **estimated floor zone**. The
zone centre is only a display anchor, not a measured coordinate, and there is no
vertical coordinate. The model **abstains** in these cases:

* it is ambiguous
* the data is out of calibration
* a required receiver is missing or stale
* the windows are misaligned
* it predicts EMPTY or OUTSIDE

RoomSense never estimates a position by averaging receiver coordinates or by choosing
the receiver with the strongest RSSI.
