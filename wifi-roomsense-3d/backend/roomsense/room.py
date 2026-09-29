"""Room geometry: the shipped EXAMPLE room and the user's own room file.

Room geometry is always either typed in by the user (``USER_PROVIDED``) or
the sample below (``EXAMPLE``). It is never inferred from Wi-Fi data, and the
3-D view drawing it is not a reconstruction of anything.

* :func:`example_room` returns the EXAMPLE geometry: a 4.0 x 3.5 m target
  room with one door, one transmitter and three receivers mounted on the
  outside of its walls, three links and three zones (A/B/C). The numbers are
  made up; they describe no real room.
* :func:`load_room` returns the user's ``data/room.json`` (validated, with the
  provenance forced to ``USER_PROVIDED``) or the EXAMPLE if there is none.
* :func:`save_room` validates, forces ``USER_PROVIDED`` and writes the file
  atomically (temporary file + ``os.replace``). It returns the config hash,
  which calibrations and zone models are bound to.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .config import AppConfig
from .schemas import (
    Door,
    GeometryProvenance,
    LinkDef,
    NodeRole,
    RoomGeometry,
    SensorNode,
    Vec2,
    Vec3,
    Wall,
    Zone,
)

__all__ = [
    "EXAMPLE_GEOMETRY_ID",
    "MAX_ROOM_FILE_BYTES",
    "RoomLoadResult",
    "example_room",
    "room_path",
    "load_room",
    "load_room_detailed",
    "save_room",
]

log = logging.getLogger(__name__)

EXAMPLE_GEOMETRY_ID = "example-4x3.5m-3rx"
MAX_ROOM_FILE_BYTES = 1_000_000

_EXAMPLE_NOTE = (
    "EXAMPLE geometry shipped with RoomSense. It does not describe your room. Measure your own room, "
    "sensor positions and zones and save them; calibrations and zone models are bound to the saved geometry."
)


def example_room() -> RoomGeometry:
    """The EXAMPLE room (provenance ``EXAMPLE``). A fresh copy on every call."""
    w, d, h = 4.0, 3.5, 2.5
    third = w / 3.0
    corners = [Vec2(x=0, y=0), Vec2(x=w, y=0), Vec2(x=w, y=d), Vec2(x=0, y=d)]
    walls = [
        Wall(id="wall_south", start=corners[0], end=corners[1], height_m=h, material="drywall (example)",
             is_target_room_boundary=True),
        Wall(id="wall_east", start=corners[1], end=corners[2], height_m=h, material="drywall (example)",
             is_target_room_boundary=True),
        Wall(id="wall_north", start=corners[2], end=corners[3], height_m=h, material="drywall (example)",
             is_target_room_boundary=True),
        Wall(id="wall_west", start=corners[3], end=corners[0], height_m=h, material="drywall (example)",
             is_target_room_boundary=True),
    ]
    # Sensors sit just outside the target room (the "behind a wall" setup);
    # 0.15 m is an arbitrary example mounting offset.
    nodes = [
        SensorNode(id="tx1", role=NodeRole.TX, label="Transmitter (example)", position=Vec3(x=-0.15, y=1.75, z=1.0),
                   inside_target_room=False),
        SensorNode(id="rx1", role=NodeRole.RX, label="Receiver 1 (example)", position=Vec3(x=4.15, y=0.6, z=1.0),
                   inside_target_room=False),
        SensorNode(id="rx2", role=NodeRole.RX, label="Receiver 2 (example)", position=Vec3(x=4.15, y=2.9, z=1.0),
                   inside_target_room=False),
        SensorNode(id="rx3", role=NodeRole.RX, label="Receiver 3 (example)", position=Vec3(x=2.0, y=3.65, z=1.0),
                   inside_target_room=False),
    ]
    links = [LinkDef(link_id=f"tx1->{rx}", transmitter_id="tx1", receiver_id=rx) for rx in ("rx1", "rx2", "rx3")]
    zones = [
        Zone(id="A", label="Zone A (example, west third)",
             polygon=[Vec2(x=0, y=0), Vec2(x=third, y=0), Vec2(x=third, y=d), Vec2(x=0, y=d)]),
        Zone(id="B", label="Zone B (example, middle third)",
             polygon=[Vec2(x=third, y=0), Vec2(x=2 * third, y=0), Vec2(x=2 * third, y=d), Vec2(x=third, y=d)]),
        Zone(id="C", label="Zone C (example, east third)",
             polygon=[Vec2(x=2 * third, y=0), Vec2(x=w, y=0), Vec2(x=w, y=d), Vec2(x=2 * third, y=d)]),
    ]
    return RoomGeometry(
        geometry_id=EXAMPLE_GEOMETRY_ID,
        provenance=GeometryProvenance.EXAMPLE,
        name="EXAMPLE room - not your room",
        width_m=w,
        depth_m=d,
        height_m=h,
        walls=walls,
        doors=[Door(id="door_south", wall_id="wall_south", offset_m=0.4, width_m=0.9, height_m=2.0)],
        nodes=nodes,
        links=links,
        zones=zones,
        target_room_polygon=list(corners),
        notes=_EXAMPLE_NOTE,
    )


def room_path(cfg: AppConfig) -> Path:
    return cfg.resolve_path(cfg.room.geometry_file)


@dataclass(frozen=True)
class RoomLoadResult:
    room: RoomGeometry
    source: str  # "user_file" or "example"
    error: str | None = None  # why the user's file was not used, if it exists but is unusable


def _as_user_provided(room: RoomGeometry) -> RoomGeometry:
    # The provenance is not the client's to choose: anything saved or loaded
    # from the user's file was entered by the user, never measured.
    return room.model_copy(update={"provenance": GeometryProvenance.USER_PROVIDED})


def load_room_detailed(cfg: AppConfig) -> RoomLoadResult:
    """Load the user's room, or fall back to the EXAMPLE with the reason.

    A present but unreadable/invalid file is *reported* (``error``) instead of
    being silently replaced: the UI then shows the EXAMPLE label plus the
    error so the user knows their geometry was not used.
    """
    path = room_path(cfg)
    if not path.exists() and not path.is_symlink():
        return RoomLoadResult(example_room(), "example")
    try:
        if path.is_symlink():
            raise ValueError("room file is a symlink; refusing to follow it")
        size = path.stat().st_size
        if size > MAX_ROOM_FILE_BYTES:
            raise ValueError(f"room file is {size} bytes (limit {MAX_ROOM_FILE_BYTES})")
        data = json.loads(path.read_text(encoding="utf-8"))
        room = RoomGeometry.model_validate(data)
    except (OSError, ValueError, ValidationError) as exc:
        detail = f"ROOM_FILE_INVALID: {path.name} could not be used ({type(exc).__name__}: {str(exc)[:300]})"
        log.warning("room file unusable; using the EXAMPLE room", extra={"reason": detail})
        return RoomLoadResult(example_room(), "example", detail)
    return RoomLoadResult(_as_user_provided(room), "user_file")


def load_room(cfg: AppConfig) -> RoomGeometry:
    """The user's room (``USER_PROVIDED``) or the EXAMPLE room."""
    return load_room_detailed(cfg).room


def save_room(cfg: AppConfig, geometry: RoomGeometry) -> str:
    """Validate, force ``USER_PROVIDED``, write atomically; return the config hash."""
    room = _as_user_provided(RoomGeometry.model_validate(geometry.model_dump()))
    path = room_path(cfg)
    if path.is_symlink():
        raise ValueError("room file path is a symlink; refusing to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(room.model_dump(mode="json"), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_ROOM_FILE_BYTES:
        raise ValueError(f"room geometry is larger than {MAX_ROOM_FILE_BYTES} bytes")
    fd, tmp = tempfile.mkstemp(prefix=".room-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return room.config_hash()
