/**
 * Pure helpers for the 3-D signal ribbon on the "My Wi-Fi signal" page.
 *
 * The ribbon is a CHART: x = time, height = signal strength in dBm. It is not a
 * picture of the room. Missing readings break the ribbon; nothing is
 * interpolated across them.
 */

export const RIBBON = {
  xMin: -6,
  xMax: 6,
  /** dBm drawn at height 0 and at full height. */
  dbmFloor: -100,
  dbmTop: -30,
  height: 4,
  depth: 0.9,
} as const;

export interface RibbonPoint {
  x: number;
  y: number;
  dbm: number;
}

const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

export function dbmToHeight(dbm: number): number {
  return clamp01((dbm - RIBBON.dbmFloor) / (RIBBON.dbmTop - RIBBON.dbmFloor)) * RIBBON.height;
}

/** tRelS is seconds relative to now (-windowS .. 0). */
export function timeToX(tRelS: number, windowS: number): number {
  return RIBBON.xMin + clamp01((tRelS + windowS) / windowS) * (RIBBON.xMax - RIBBON.xMin);
}

/**
 * Split samples into drawable segments. A segment ends at a null reading or
 * when two readings are more than maxGapS apart. Samples outside the window
 * are dropped. Single isolated points are kept (drawn as a short stub).
 */
export function ribbonSegments(
  tRelS: readonly number[],
  dbm: readonly (number | null)[],
  windowS: number,
  maxGapS: number,
): RibbonPoint[][] {
  const out: RibbonPoint[][] = [];
  let cur: RibbonPoint[] = [];
  let lastT: number | null = null;
  const n = Math.min(tRelS.length, dbm.length);
  for (let i = 0; i < n; i++) {
    const t = tRelS[i];
    const v = dbm[i];
    if (t === undefined || !Number.isFinite(t) || t < -windowS || t > 0) continue;
    if (v === null || v === undefined || !Number.isFinite(v)) {
      if (cur.length) out.push(cur);
      cur = [];
      lastT = null;
      continue;
    }
    if (lastT !== null && t - lastT > maxGapS && cur.length) {
      out.push(cur);
      cur = [];
    }
    cur.push({ x: timeToX(t, windowS), y: dbmToHeight(v), dbm: v });
    lastT = t;
  }
  if (cur.length) out.push(cur);
  return out;
}

/** Colour by strength: red (weak, <= -85 dBm) -> yellow (-70) -> green (>= -50). RGB 0..1. */
export function strengthColor(dbm: number): [number, number, number] {
  const red: [number, number, number] = [0.94, 0.33, 0.31];
  const yellow: [number, number, number] = [0.98, 0.8, 0.28];
  const green: [number, number, number] = [0.32, 0.81, 0.4];
  const mix = (a: [number, number, number], b: [number, number, number], f: number): [number, number, number] => [
    a[0] + (b[0] - a[0]) * f,
    a[1] + (b[1] - a[1]) * f,
    a[2] + (b[2] - a[2]) * f,
  ];
  if (dbm <= -70) return mix(red, yellow, clamp01((dbm + 85) / 15));
  return mix(yellow, green, clamp01((dbm + 70) / 20));
}
