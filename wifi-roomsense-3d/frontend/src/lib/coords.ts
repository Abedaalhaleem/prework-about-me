/**
 * Coordinate mapping between the backend floor frame and three.js.
 *
 * Backend: metres, x east, y north, z up (height above the floor).
 * three.js: y is up. We map (x, y, z) -> (x, z, -y), a proper rotation, so
 * the handedness is preserved and "north" points into the screen (-Z) in the
 * default camera orientation.
 */

import type { Door, RoomGeometry, Vec2, Wall } from '../api/types';

export type Triple = [number, number, number];

export function toThree(p: { x: number; y: number; z?: number }): Triple {
  // `+ 0` normalises -0 to 0 so equality checks and labels stay clean.
  return [p.x, (p.z ?? 0) + 0, -p.y + 0];
}

export function fromThree([x, y, z]: Triple): { x: number; y: number; z: number } {
  return { x, y: -z + 0, z: y };
}

/** Polygon centroid (same maths as backend Zone.display_anchor). Display only. */
export function polygonCentroid(pts: readonly Vec2[]): Vec2 | null {
  const n = pts.length;
  if (n === 0) return null;
  let a = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < n; i++) {
    const p0 = pts[i] as Vec2;
    const p1 = pts[(i + 1) % n] as Vec2;
    const cross = p0.x * p1.y - p1.x * p0.y;
    a += cross;
    cx += (p0.x + p1.x) * cross;
    cy += (p0.y + p1.y) * cross;
  }
  if (Math.abs(a) < 1e-12) {
    return { x: pts.reduce((s, p) => s + p.x, 0) / n, y: pts.reduce((s, p) => s + p.y, 0) / n };
  }
  a *= 0.5;
  return { x: cx / (6 * a), y: cy / (6 * a) };
}

export interface Bounds2 {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

/** Floor-plane bounds covering the declared extent and every drawn element. */
export function roomBounds(room: RoomGeometry): Bounds2 {
  const xs: number[] = [0, room.width_m];
  const ys: number[] = [0, room.depth_m];
  room.walls.forEach((w) => {
    xs.push(w.start.x, w.end.x);
    ys.push(w.start.y, w.end.y);
  });
  room.nodes.forEach((n) => {
    xs.push(n.position.x);
    ys.push(n.position.y);
  });
  room.zones.forEach((z) =>
    z.polygon.forEach((p) => {
      xs.push(p.x);
      ys.push(p.y);
    }),
  );
  room.target_room_polygon.forEach((p) => {
    xs.push(p.x);
    ys.push(p.y);
  });
  const finite = (a: number[]): number[] => a.filter((v) => Number.isFinite(v));
  const fx = finite(xs);
  const fy = finite(ys);
  return { minX: Math.min(...fx), maxX: Math.max(...fx), minY: Math.min(...fy), maxY: Math.max(...fy) };
}

export function wallLength(w: Wall): number {
  return Math.hypot(w.end.x - w.start.x, w.end.y - w.start.y);
}

/** Point at distance `d` metres from the wall start, on the wall line. */
export function pointAlongWall(w: Wall, d: number): Vec2 {
  const len = wallLength(w);
  if (len === 0) return { ...w.start };
  const f = d / len;
  return { x: w.start.x + (w.end.x - w.start.x) * f, y: w.start.y + (w.end.y - w.start.y) * f };
}

export interface WallSpans {
  /** Solid wall intervals [from, to] in metres from the wall start. */
  solid: [number, number][];
  /** Door openings clamped to the wall, merged where they overlap. */
  openings: { from: number; to: number; height: number; doorIds: string[] }[];
}

/**
 * Split a wall into solid pieces around its doors. Doors are clamped to the
 * wall length; overlapping doors are merged (taking the taller opening).
 */
export function wallSpans(wall: Wall, doors: readonly Door[]): WallSpans {
  const len = wallLength(wall);
  const raw = doors
    .filter((d) => d.wall_id === wall.id)
    .map((d) => ({
      from: Math.max(0, Math.min(len, d.offset_m)),
      to: Math.max(0, Math.min(len, d.offset_m + d.width_m)),
      height: Math.min(d.height_m, wall.height_m),
      doorIds: [d.id],
    }))
    .filter((o) => o.to > o.from)
    .sort((a, b) => a.from - b.from);
  const openings: WallSpans['openings'] = [];
  for (const o of raw) {
    const last = openings[openings.length - 1];
    if (last && o.from <= last.to) {
      last.to = Math.max(last.to, o.to);
      last.height = Math.max(last.height, o.height);
      last.doorIds.push(...o.doorIds);
    } else {
      openings.push({ ...o, doorIds: [...o.doorIds] });
    }
  }
  const solid: [number, number][] = [];
  let cursor = 0;
  for (const o of openings) {
    if (o.from > cursor) solid.push([cursor, o.from]);
    cursor = Math.max(cursor, o.to);
  }
  if (cursor < len) solid.push([cursor, len]);
  return { solid, openings };
}
