# Where to put the transmitter and receivers

This guide shows where the transmitter (TX) and receivers (RX) go for each
experiment. Read it before you mount anything.

> **Status: not hardware-tested.** This project has not yet run any of these
> layouts on real boards. The advice comes from basic radio geometry and from
> Espressif's esp-csi notes (pinned commit `8633d67`). Only your own
> measurements, using the validation protocol, show whether a layout works
> behind your wall.
>
> Use RoomSense only in spaces you control, and only when everyone present
> knows about it and agrees.

## Conventions

* Coordinates use the same convention as `backend/roomsense/schemas.py`:
  metres, **x = east, y = north, z = height above the floor**.
* Enter every board in the room model (Calibration wizard, step "Sensor
  nodes"). Give its role (TX, RX or ROUTER), its position `x, y, z`, and
  `inside_target_room` (true or false).
* In the diagrams, `#` marks a wall, `[TX1]`, `[RX1]`, … are boards, and
  `[PC]` is the computer running RoomSense. Receivers connect to the PC by
  USB. A transmitter only needs power.
* The **target room** is the room you want to sense. An **adjacent space** is
  a room or corridor next to it that you also control.

## The one rule: the link has to cross the space you want to sense

A TX–RX pair senses best along the straight line between them. The sensitive
region around that line is roughly the first Fresnel zone, which is widest
halfway between the two boards. Its radius there is about ½·√(λ·d), where
λ ≈ 0.123 m at 2.4 GHz and d is the TX–RX distance:

| TX–RX distance d | Fresnel radius at mid-point (computed, not measured) |
|---|---|
| 2 m | 0.25 m |
| 4 m | 0.35 m |
| 6 m | 0.43 m |
| 8 m | 0.50 m |

Movement outside this zone still changes the CSI through reflections
(multipath), but usually much less. Two things follow:

1. If the TX–RX line never enters the target room, a person moving in the
   target room mostly affects the signal through weak reflections. Someone
   moving near the boards on your side of the wall affects it strongly.
2. **Do not assume everything can stay on one side of a wall.** Most useful
   through-wall layouts need a board on the far side of the target room, or a
   board inside it.

### Do the boards have to be inside the target room?

| Layout | Board inside the target room? | Spaces you need access to | What it can show |
|---|---|---|---|
| (a) First capture | No target room involved | One room | Only that acquisition works |
| (b1) Through-wall, opposite sides | **No**, but TX and RX sit in two different adjacent spaces on opposite sides of the target room | Two adjacent spaces | The link crosses the target room |
| (b2) Through-wall, one board inside | **Yes**, one board (preferably the TX) | The target room and one adjacent space | The link crosses the target room |
| (c) Both boards on one side of one wall | No | One adjacent space | Reflections only. Expected to be weak and dominated by movement on your side. It must be tested with scenario S3. |
| (d) Zone experiment | Usually boards on two or three sides, or inside | Several adjacent spaces | Experimental. Subject to the zone enablement criteria. |

## (a) First capture experiment: router + one receiver, same room

Purpose: check that acquisition works. You should see an RSHELLO line, a steady
packet rate, RSSI and drop counters (capability A). This is **not** a sensing
test and not a through-wall test.

```
 y (north)
 ^
 |  +------------------------------------------------------+
 |  |  ONE ROOM YOU CONTROL (no target room yet)           |
 |  |                                                      |
 |  |   [ROUTER]   stays where it is; settings unchanged   |
 |  |       \                                              |
 |  |        \                                             |
 |  |         \   direct path, at least 1 m                |
 |  |          \                                           |
 |  |          [RX1]  z ~ 1.0 m ======USB====== [PC]       |
 |  |                                                      |
 |  +------------------------------------------------------+
 +----------------------------------------------------------> x (east)
```

* Put RX1 **more than 1 m** from the router. The esp-csi get-started README
  says: "The distance between the two devices should be greater than 1 meter."
  That note is about two dev boards. Using the same spacing for a router is
  our conservative choice.
* Keep a clear line of sight between the router and RX1 for this first test.
* With a dedicated transmitter (ESP-NOW mode), use the same layout with
  `[TX1]` in place of the router.
* Try it like this: record 2 minutes with nobody moving, then walk across the
  line a few times. If the score changes, the pipeline responds. That is not
  validation.

## (b) Through-wall, single link: the link must pass through the target room

### (b1) TX in adjacent space A, RX in adjacent space B on the far side

No board goes inside the target room. You need access to **both** A and B.

```
        SPACE A                      TARGET ROOM                      SPACE B
  +-----------------+##+------------------------------------+##+-----------------+
  |                 |##|                                    |##|                 |
  |                 |##|       ..--''''''''''''''--..       |##|                 |
  |  [TX1] ---------|##|------(  TX-RX sensing line  )------|##|--------- [RX1]  |
  |  z ~ 1.0 m      |##|       ''--..............--''       |##|       z ~ 1.0 m |
  |  (power only)   |##|  Fresnel zone, widest mid-way      |##|        |USB     |
  |                 |##|                                    |##|       [PC]      |
  +-----------------+##+------------------------------------+##+-----------------+
                   wall 1                                  wall 2
```

* Line the two boards up so that the straight TX–RX line crosses the part of
  the target room where people actually move, not a corner.
* The signal crosses two walls. Whether enough of it gets through depends on
  the wall material. That has not been measured, so check the per-link rate and
  RSSI in the Dashboard before calibrating.

### (b2) One board inside the target room

If you cannot reach a space on the far side, one board has to go **inside**
the target room. The TX is the better choice there: it only needs power, so no
USB data cable has to run into the room.

```
          SPACE A (near side)                       TARGET ROOM
  +--------------------------------+##+-------------------------------------+
  |                                |##|                                     |
  |  [PC]===USB===[RX1] -----------|##|------- sensing line -------- [TX1]  |
  |               z ~ 1.0 m        |##|                          z ~ 1.0 m  |
  |                                |##|         (USB power supply only)     |
  |                                |##|                                     |
  +--------------------------------+##+-------------------------------------+
```

* Put TX1 against the far wall of the target room so the link spans most of
  the room.
* Set `inside_target_room = true` for TX1 in the room model.

## (c) Both boards on one side of one wall: reflection only

```
             NEAR SIDE (you, TX, RX, PC)                     TARGET ROOM
  +--------------------------------------------+##+----------------------------+
  |                                            |##|                            |
  |   [TX1] ========= direct path ======= [RX1]|##|                            |
  |      \                               /   | |##|         (person)           |
  |       \       near-side reflections /  USB |##|           ^   |            |
  |        \_______ (strong, dominant) /   [PC]|##|           |   v            |
  |                                            |##|                            |
  |          weak path: crosses the wall ------|##|--->  scatters off the      |
  |          twice and must scatter back <-----|##|----  person                |
  +--------------------------------------------+##+----------------------------+
```

Why this is expected to be weak:

* The direct path and the strongest reflections stay on your side of the
  wall. None of them enters the target room.
* A signal that reaches a person in the target room crosses the wall twice,
  losing strength each time. It also has to scatter back off the person toward
  the receiver. What arrives is a small part of the total.
* Anyone moving on your side (you, the operator, a pet, a door) changes the
  dominant paths directly. Their effect is expected to be much larger than that
  of a person behind the wall.

So a detection in this layout **cannot be attributed to the target room**
unless your own validation shows it. Run scenario
`S3_MOTION_NEAR_SENSORS_OUTSIDE` from the validation protocol
(`backend/roomsense/validation/protocol.py`): one person moves near the
boards while the target room is empty. If near-side movement is detected at a
rate comparable to real target-room movement (S2), the through-wall status is
**NOT_DISTINGUISHABLE**, whatever S1 and S2 show. Treat this layout as a
baseline to compare against, not as the setup to aim for.

## (d) Zone experiment: 1 TX + 3–4 RX whose links cross different zones

Capability C (experimental single-person zone estimation) needs at least three
receivers (`min_receivers = 3` in `configs/zone_enablement.toml`). The receivers
must be placed so that **different links cross different zones**. A zone is
then recognisable by which links change. This example uses a 4 m × 4 m target
room with four zones (quadrants). Boards sit in three different adjacent
spaces: west, east and north.

```
                  [RX3]                     SPACE N (adjacent)
                 ///
            #################################
            #///     NW     :         NE    # ~~~~[RX2]
 SPACE W   /#/              :        ~~~~~~~#~~
 (adj.)   //#               :~~~~~~~~~      #         SPACE E (adjacent)
        /// #.......~~~~~~~~~~..............#
      ///   #~~~~~~~~       :               #
  [TX1]=====#===============================#=====[RX1]
            #      SW       :         SE    #
            #################################
           x=0             x=2             x=4      (y=0 at the bottom wall, y=4 at the top)
```

Links: `=` is TX1→RX1, `~` is TX1→RX2, `/` is TX1→RX3. The drawing was
rasterised from the coordinates below. It is schematic: the vertical scale is
compressed.

| Board | Space | x | y | z |
|---|---|---|---|---|
| TX1 | W (adjacent, west) | −1.0 | 0.8 | 1.0 |
| RX1 | E (adjacent, east) | 5.0 | 0.8 | 1.0 |
| RX2 | E (adjacent, east) | 5.0 | 3.6 | 1.0 |
| RX3 | N (adjacent, north) | 1.0 | 5.0 | 1.0 |

The zones each straight TX–RX line passes through were computed by sampling
the lines. This is geometry, not a measurement.

| Link | Length | Zones crossed (length inside each) |
|---|---|---|
| TX1→RX1 | 6.00 m | SW 2.00 m, SE 2.00 m |
| TX1→RX2 | 6.62 m | SW 1.73 m, NW 0.47 m, NE 2.21 m |
| TX1→RX3 | 4.65 m | NW 1.22 m |

Each zone gets a different "link signature": SW {RX1, RX2}, SE {RX1}, NW
{RX3, a little RX2}, NE {RX2}. That is the property the layout is after.

A 4th receiver is optional. Place it so that its line crosses a *different*
combination of zones, typically to strengthen the zone with the weakest
coverage (SE here, which only RX1 crosses). Draw the lines on your own floor
plan before mounting anything.

Honest limits of this layout:

* Every link starts at TX1, so all links overlap near the TX. Zones next to
  the TX side are harder to tell apart.
* Independent receivers are **not a synchronised antenna array**. They have
  separate clocks and no shared phase reference. RoomSense only lines up their
  windows by host arrival time, within `acquisition.alignment_tolerance_s`
  (default 0.25 s). Nothing here measures angle of arrival or position.
* A zone output stays **DISABLED** until a model passes the predefined
  criteria in `configs/zone_enablement.toml` on held-out sessions, with
  `EMPTY` and `OUTSIDE_TARGET_ROOM` classes and one moving participant. Even
  then the zone centre shown in the 3-D view is a display anchor, not a
  measured position.
* This layout needs access to three adjacent spaces. If you only control one
  side of the target room, a zone experiment is unlikely to work. Layout (c)
  explains why.

## Mounting, spacing and cables (all layouts)

* **Height:** start at about **1.0 m** above the floor (`z ≈ 1.0`) for both TX
  and RX, so the link is level and passes through the torso of a standing or
  walking person. This is a starting point, not a measured optimum.
* **Away from metal:** keep the antennas clear of metal shelves, radiators,
  appliances, mirrors, metal door frames and the PC case. We have not measured
  how much clearance is enough, so keep as much as is practical and write it
  down in the run's placement notes.
* **Fixed mounts:** use a stand, bracket or tape so that nothing can be bumped.
  Never hand-hold a board, and don't mount one on a door or on furniture that
  people move.
* **Antenna orientation:** point external antennas the same way on every board
  (for example vertical) and don't change it after calibration. Boards with a
  PCB antenna are directional ("PCB antenna has directivity", esp-csi README),
  so their orientation matters too.
* **Spacing:** keep TX and RX **at least 1 m** apart (esp-csi get-started
  README). Keeping receivers 1 m or more from each other is our own
  conservative choice, not an upstream rule.
* **Recalibrate after any move.** Moving, turning or re-mounting a board
  changes the channel. Record a new quiet baseline (CALIBRATION.md). If you
  also update the position in the room model, the room configuration hash
  changes, and old calibrations and zone models tied to it are invalidated
  automatically.
* **Cable routing:**
  * Receivers need a data-capable USB cable to the PC. Transmitters need only
    USB power.
  * Run cables along walls or floor edges and tape them down. Don't run them
    across walkways or through doors you will open during tests (door
    movement is scenario S4).
  * Don't coil cables around or next to an antenna. A cable that moves near an
    antenna can change the signal.
  * If receivers are far from the PC, use a powered USB hub near them rather
    than long passive cables. Check any extension or active cable before
    relying on it (Dashboard: rate, drops).
* **Operators are near-side motion:** during empty-room calibration and S1
  runs, stay away from the boards and the link line, or leave the area.
* **Write it down:** each validation run needs a placement description
  (heights, distance to the wall, which side of the wall each board is on). See
  `REQUIRED_METADATA` in `backend/roomsense/validation/protocol.py`.
