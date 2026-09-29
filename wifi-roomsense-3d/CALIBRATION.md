# Calibration

Calibration covers two things:

* a **quiet baseline** for motion detection (capability B)
* an optional, **experimental** zone model (capability C)

Both are tied to the exact hardware, channel, firmware, room geometry and processing
configuration they were recorded under. When any of those change, they are
invalidated automatically.

> Use RoomSense only in spaces you control, and only with every participant's consent.
> Recording is opt-in and stays on this computer.

## 0. Before you start

1. The boards are mounted in their final positions (see `docs/PLACEMENT.md`).
   Moving a board afterwards invalidates the calibration.
2. The live source is connected, and the header shows **LIVE MEASUREMENTS**.
   Simulation can walk you through the steps. A baseline made from simulated data is
   labelled SIMULATION, and it can never enable anything for live use.
3. The measured acquisition rate for each link is stable (Dashboard → link list).

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
2. Calibration → Quiet baseline → tick **"I confirm the target room is empty and quiet"** → Start.
3. Leave it running for at least `detection.baseline_min_duration_s` (default 60 s) and
   `detection.baseline_min_windows` (default 30) windows. Longer is better.
4. Press Stop. The baseline is **rejected** (reason `BASELINE_UNSTABLE`) when any of
   these happen:
   * more than 10 % of its own windows would already count as motion
   * the static channel profile shifted during the recording
   * data quality was too low

   A rejected baseline is never applied. Record it again.

What the baseline records: the median and robust spread of each window feature
(amplitude variation, temporal differences), a per-subcarrier profile, the calibration
ID, the hardware signature, the config version, and the CSI layout.

The detector **never** folds new data into the baseline. When the static channel
changes for longer than `drift_hold_s` (for example because a board moved, furniture
moved, or someone is standing still), the link reports **UNKNOWN** with reason
`STATIC_CHANNEL_CHANGED`. Recalibrate only when the room really is empty.

The calibration is invalidated automatically (state `UNKNOWN`, reason
`CALIBRATION_INVALID`) when any of these change:

* the channel or bandwidth
* the CSI layout
* the firmware identity
* the transmitter MAC
* the set of links
* the room configuration hash
* the processing or detection config version

## 3. Walk test

1. Calibration → Walk test → Start.
2. One person walks through the target room for 20–30 s, crossing each sensing link.
3. Stop. For each link the report shows the maximum score, the fraction of windows in
   motion, and whether motion was detected.

A walk test in the **same room** is not evidence of **through-wall** performance. That
needs the protocol in `VALIDATION_REPORT.md`.

## 4. Zone data collection (capability C, experimental)

Zone estimation stays **DISABLED** until a model passes the predefined criteria in
`configs/zone_enablement.toml`. Those criteria were written before any data existed,
and they must not be lowered after seeing results.

Requirements:

* One transmitter and at least `zone.min_receivers` (default 3) spatially separated
  receivers.
* One moving participant. It is not a people counter, and it does not track stationary people.

For each class, record **separate sessions** (Recordings → consent form → Start):

| Class | What to do |
|---|---|
| each zone id (e.g. `A`, `B`, `C`) | One person moves inside that zone only: walking in small loops, turning, different orientations, arm movements. |
| `EMPTY` | Nobody in the target room, and little nearby activity. |
| `OUTSIDE_TARGET_ROOM` | One person moving **outside** the target room, near the transmitter or receivers. |

Collect at least:

* 3 training sessions per class
* 1 validation session per class
* 1 test session per class

Record the test sessions **last**, preferably on a different day. Vary the
orientation and movement style.

Training (Capabilities & Research → Zone → Train, or `roomsense zone-train`):

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
room hash, the link set and the config version. If any of those change, the model goes
back to DISABLED.

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
