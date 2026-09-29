/**
 * Client-side checks for a RoomGeometry draft before PUT /api/room.
 *
 * Errors mirror the pydantic constraints in backend/roomsense/schemas.py so
 * the user gets immediate feedback; the backend remains the authority and its
 * 422 detail is shown as well. Warnings flag things that are allowed but
 * probably mistakes.
 */

import type { NodeRole, RoomGeometry, Vec2, Wall } from '../api/types';
import { wallLength } from './coords';

export interface RoomIssues {
  errors: string[];
  warnings: string[];
}

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

function checkRange(errors: string[], what: string, v: number, lo: number, hi: number, loInclusive = false): void {
  if (!finite(v)) errors.push(`${what} must be a number`);
  else if (loInclusive ? v < lo : v <= lo) errors.push(`${what} must be ${loInclusive ? '≥' : '>'} ${lo}`);
  else if (v > hi) errors.push(`${what} must be ≤ ${hi}`);
}

function checkVec2(errors: string[], what: string, p: Vec2): void {
  if (!finite(p.x) || !finite(p.y)) errors.push(`${what} has a missing or invalid coordinate`);
}

function duplicates(ids: string[]): string[] {
  const seen = new Set<string>();
  const dup = new Set<string>();
  ids.forEach((id) => (seen.has(id) ? dup.add(id) : seen.add(id)));
  return [...dup];
}

export function validateRoom(room: RoomGeometry): RoomIssues {
  const errors: string[] = [];
  const warnings: string[] = [];

  if (!room.name.trim()) warnings.push('The room has no name.');
  checkRange(errors, 'Room width (x extent)', room.width_m, 0, 100);
  checkRange(errors, 'Room depth (y extent)', room.depth_m, 0, 100);
  checkRange(errors, 'Room height', room.height_m, 0, 20);

  // Walls
  const wallIds = room.walls.map((w) => w.id);
  if (wallIds.some((id) => !id.trim())) errors.push('Every wall needs an id.');
  duplicates(wallIds).forEach((id) => errors.push(`Duplicate wall id "${id}".`));
  const wallById = new Map<string, Wall>();
  room.walls.forEach((w) => {
    wallById.set(w.id, w);
    checkVec2(errors, `Wall ${w.id} start`, w.start);
    checkVec2(errors, `Wall ${w.id} end`, w.end);
    checkRange(errors, `Wall ${w.id} height`, w.height_m, 0, 20);
    checkRange(errors, `Wall ${w.id} thickness`, w.thickness_m, 0, 2);
    if (finite(w.start.x) && finite(w.end.x) && wallLength(w) < 0.01) warnings.push(`Wall ${w.id} has (almost) zero length.`);
  });

  // Doors
  duplicates(room.doors.map((d) => d.id)).forEach((id) => errors.push(`Duplicate door id "${id}".`));
  room.doors.forEach((d) => {
    const w = wallById.get(d.wall_id);
    if (!w) errors.push(`Door ${d.id} references unknown wall "${d.wall_id}".`);
    checkRange(errors, `Door ${d.id} offset`, d.offset_m, 0, Number.MAX_VALUE, true);
    checkRange(errors, `Door ${d.id} width`, d.width_m, 0, 5);
    checkRange(errors, `Door ${d.id} height`, d.height_m, 0, 5);
    if (w && finite(d.offset_m) && finite(d.width_m) && d.offset_m + d.width_m > wallLength(w) + 1e-6) {
      warnings.push(`Door ${d.id} extends past the end of wall ${w.id}; it will be clipped in the view.`);
    }
  });

  // Nodes
  const nodeIds = room.nodes.map((n) => n.id);
  if (nodeIds.some((id) => !id.trim())) errors.push('Every sensor node needs an id.');
  duplicates(nodeIds).forEach((id) => errors.push(`Duplicate sensor node id "${id}".`));
  const roleById = new Map<string, NodeRole>();
  room.nodes.forEach((n) => {
    roleById.set(n.id, n.role);
    if (!finite(n.position.x) || !finite(n.position.y) || !finite(n.position.z)) {
      errors.push(`Node ${n.id} has a missing or invalid x/y/z position.`);
    } else if (n.position.z < 0 || n.position.z > room.height_m) {
      warnings.push(`Node ${n.id} height z=${n.position.z} m is outside 0..${room.height_m} m.`);
    }
    if (n.inside_target_room === null) warnings.push(`Node ${n.id}: "inside target room" is not set.`);
  });

  // Links
  duplicates(room.links.map((l) => l.link_id)).forEach((id) => errors.push(`Duplicate link id "${id}".`));
  room.links.forEach((l) => {
    if (!l.link_id.trim()) errors.push('Every link needs an id.');
    const txRole = roleById.get(l.transmitter_id);
    const rxRole = roleById.get(l.receiver_id);
    if (txRole === undefined || rxRole === undefined) {
      errors.push(`Link ${l.link_id} references an unknown node.`);
      return;
    }
    if (l.transmitter_id === l.receiver_id) errors.push(`Link ${l.link_id} uses the same node as transmitter and receiver.`);
    if (txRole === 'RX') warnings.push(`Link ${l.link_id}: transmitter "${l.transmitter_id}" has role RX.`);
    if (rxRole !== 'RX') warnings.push(`Link ${l.link_id}: receiver "${l.receiver_id}" does not have role RX.`);
  });

  // Zones
  const zoneIds = room.zones.map((z) => z.id);
  if (zoneIds.some((id) => !id.trim())) errors.push('Every zone needs an id.');
  duplicates(zoneIds).forEach((id) => errors.push(`Duplicate zone id "${id}".`));
  room.zones.forEach((z) => {
    if (z.polygon.length < 3) errors.push(`Zone ${z.id} needs at least 3 polygon points.`);
    z.polygon.forEach((p, i) => checkVec2(errors, `Zone ${z.id} point ${i + 1}`, p));
  });

  if (room.target_room_polygon.length > 0 && room.target_room_polygon.length < 3) {
    errors.push('The target-room outline needs at least 3 points (or none).');
  }
  room.target_room_polygon.forEach((p, i) => checkVec2(errors, `Target-room point ${i + 1}`, p));
  if (room.target_room_polygon.length === 0) warnings.push('No target-room outline: the view cannot show which room is being sensed.');

  return { errors, warnings };
}

/** An empty user-provided room of the given size (no walls, nodes or zones). */
export function blankRoom(width = 4, depth = 3, height = 2.5): RoomGeometry {
  return {
    geometry_id: 'user-room',
    provenance: 'USER_PROVIDED',
    name: 'My room',
    width_m: width,
    depth_m: depth,
    height_m: height,
    walls: [],
    doors: [],
    nodes: [],
    links: [],
    zones: [],
    target_room_polygon: [],
    notes: null,
  };
}

/** Four perimeter walls for the declared rectangle (a user-triggered convenience). */
export function perimeterWalls(width: number, depth: number, height: number): Wall[] {
  const corners: Vec2[] = [
    { x: 0, y: 0 },
    { x: width, y: 0 },
    { x: width, y: depth },
    { x: 0, y: depth },
  ];
  const names = ['south', 'east', 'north', 'west'];
  return corners.map((c, i) => ({
    id: `w_${names[i] ?? i}`,
    start: { ...c },
    end: { ...(corners[(i + 1) % 4] as Vec2) },
    height_m: height,
    thickness_m: 0.12,
    material: null,
    is_target_room_boundary: true,
  }));
}

export function defaultLinkId(transmitterId: string, receiverId: string): string {
  return `${transmitterId}->${receiverId}`;
}

export function cloneRoom(room: RoomGeometry): RoomGeometry {
  return JSON.parse(JSON.stringify(room)) as RoomGeometry;
}
