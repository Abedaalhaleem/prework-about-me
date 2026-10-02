/**
 * SIMULATED Wi-Fi coverage for the "Wi-Fi waves (simulated)" page.
 *
 * A textbook indoor "multi-wall" path-loss model: free-space loss over
 * distance plus a fixed loss for every wall the straight line from the
 * transmitter crosses (less where it passes through a door). It uses only the
 * room layout the user entered (or the EXAMPLE room) and assumed values. It
 * measures nothing, ignores reflections and furniture, and cannot show people
 * or objects. Typical wall losses vary widely in practice; these are rough.
 */

import type { Door, RoomGeometry, Vec2, Wall } from '../api/types';

/** Assumed one-wall losses at 2.4 GHz (dB), matched on the user's material text. */
export const WALL_LOSS_RULES: readonly [RegExp, number][] = [
  [/concrete|cement|stone/i, 12],
  [/brick|block|masonry/i, 9],
  [/metal|steel/i, 20],
  [/glass|window/i, 3],
  [/wood|timber/i, 4],
  [/drywall|plaster|gypsum|plasterboard/i, 3],
];
export const UNKNOWN_WALL_LOSS_DB = 5;
export const OPEN_DOOR_LOSS_DB = 1;
/** Extra loss per wall at 5 GHz relative to 2.4 GHz (rough). */
export const FIVE_GHZ_EXTRA_WALL_LOSS_DB = 2;

export interface PropagationParams {
  txPowerDbm: number; // transmit power + antenna gain (assumed)
  freqGhz: 2.4 | 5;
  pathLossExponent: number; // 2 = free space
}

export const DEFAULT_PARAMS: PropagationParams = { txPowerDbm: 20, freqGhz: 2.4, pathLossExponent: 2.0 };

export function wallLossDb(material: string | null | undefined, freqGhz: number = 2.4): number {
  let base = UNKNOWN_WALL_LOSS_DB;
  if (material) {
    for (const [re, db] of WALL_LOSS_RULES) {
      if (re.test(material)) {
        base = db;
        break;
      }
    }
  }
  return base + (freqGhz >= 5 ? FIVE_GHZ_EXTRA_WALL_LOSS_DB : 0);
}

/** Free-space path loss at 1 m in dB: 20*log10(4*pi*f/c). */
export function fsplAt1mDb(freqGhz: number): number {
  return 20 * Math.log10((4 * Math.PI * freqGhz * 1e9) / 299_792_458);
}

/** Parameter t along segment a->b where it crosses c->d, or null if no proper crossing. */
export function crossingT(a: Vec2, b: Vec2, c: Vec2, d: Vec2): number | null {
  const rx = b.x - a.x;
  const ry = b.y - a.y;
  const sx = d.x - c.x;
  const sy = d.y - c.y;
  const den = rx * sy - ry * sx;
  if (Math.abs(den) < 1e-12) return null; // parallel
  const qx = c.x - a.x;
  const qy = c.y - a.y;
  const t = (qx * sy - qy * sx) / den; // along a->b
  const u = (qx * ry - qy * rx) / den; // along c->d
  const eps = 1e-9;
  if (t <= eps || t >= 1 - eps || u < -eps || u > 1 + eps) return null;
  return u;
}

export interface PathResult {
  dbm: number;
  lossDb: number;
  wallsCrossed: number;
}

/** Predicted received power at p from a transmitter at tx (both on the floor plan). */
export function predictDbm(
  tx: Vec2,
  p: Vec2,
  walls: readonly Wall[],
  doors: readonly Door[],
  params: PropagationParams = DEFAULT_PARAMS,
): PathResult {
  const d = Math.max(0.3, Math.hypot(p.x - tx.x, p.y - tx.y));
  let loss = fsplAt1mDb(params.freqGhz) + 10 * params.pathLossExponent * Math.log10(d);
  let crossed = 0;
  for (const w of walls) {
    const u = crossingT(tx, p, w.start, w.end);
    if (u === null) continue;
    crossed += 1;
    const len = Math.hypot(w.end.x - w.start.x, w.end.y - w.start.y);
    const along = u * len;
    const throughDoor = doors.some((dr) => dr.wall_id === w.id && along >= dr.offset_m && along <= dr.offset_m + dr.width_m);
    loss += throughDoor ? OPEN_DOOR_LOSS_DB : wallLossDb(w.material, params.freqGhz);
  }
  return { dbm: params.txPowerDbm - loss, lossDb: loss, wallsCrossed: crossed };
}

export interface CoverageGrid {
  x0: number;
  y0: number;
  cell: number;
  nx: number;
  ny: number;
  dbm: Float32Array; // row-major, index = j * nx + i, cell centres
}

export function predictGrid(
  room: RoomGeometry,
  tx: Vec2,
  params: PropagationParams = DEFAULT_PARAMS,
  cell = 0.2,
  margin = 1.5,
): CoverageGrid {
  const xs = [0, room.width_m, tx.x, ...room.walls.flatMap((w) => [w.start.x, w.end.x])];
  const ys = [0, room.depth_m, tx.y, ...room.walls.flatMap((w) => [w.start.y, w.end.y])];
  const x0 = Math.min(...xs) - margin;
  const y0 = Math.min(...ys) - margin;
  const nx = Math.max(1, Math.ceil((Math.max(...xs) + margin - x0) / cell));
  const ny = Math.max(1, Math.ceil((Math.max(...ys) + margin - y0) / cell));
  const dbm = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const p = { x: x0 + (i + 0.5) * cell, y: y0 + (j + 0.5) * cell };
      dbm[j * nx + i] = predictDbm(tx, p, room.walls, room.doors, params).dbm;
    }
  }
  return { x0, y0, cell, nx, ny, dbm };
}

/** Strongest and weakest predicted level in a grid (for the colour scale and legend). */
export function gridRange(g: CoverageGrid): { lo: number; hi: number } {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of g.dbm) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return Number.isFinite(lo) ? { lo, hi } : { lo: -90, hi: -35 };
}

/** 0..1 position of a level on the colour scale [lo, hi] (clamped). */
export function levelFraction(dbm: number, lo: number, hi: number): number {
  return Math.min(1, Math.max(0, (dbm - lo) / Math.max(1e-6, hi - lo)));
}

/** Blue glow colour: bright cyan at the strongest level, fading to dark and transparent at the weakest. */
export function coverageRgba(dbm: number, lo = -90, hi = -35): [number, number, number, number] {
  const f = levelFraction(dbm, lo, hi);
  const r = Math.round(10 + 90 * f * f);
  const g = Math.round(40 + 190 * f);
  const b = Math.round(110 + 145 * f);
  const a = Math.round(255 * (0.04 + 0.66 * f * f));
  return [r, g, b, a];
}
