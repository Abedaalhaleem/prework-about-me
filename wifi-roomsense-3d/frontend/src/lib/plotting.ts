/**
 * Pure helpers for the canvas signal plots.
 *
 * The central rule: a `null` sample, a non-finite value, a backwards time
 * step or a reported gap interval ENDS the current line segment. Lines are
 * never bridged across missing data and nothing is interpolated. Gap
 * intervals are returned separately so they can be shaded.
 */

import type { ActivityState, GapInterval, SignalSnapshot } from '../api/types';

export interface Point {
  x: number;
  y: number;
}

/** A run of consecutive valid samples; draw as one polyline (or a dot if length 1). */
export type Segment = Point[];

export interface SplitOptions {
  /** Reported gaps (same units as t). A gap overlapping (t[i-1], t[i]) splits there. */
  gaps?: readonly GapInterval[];
  /** Split when consecutive samples are further apart than this (same units as t). */
  maxStep?: number;
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/** True when any gap interval overlaps the open interval (a, b). */
export function gapBetween(a: number, b: number, gaps: readonly GapInterval[] | undefined): boolean {
  if (!gaps) return false;
  for (const g of gaps) {
    if (g.start < b && g.end > a) return true;
  }
  return false;
}

export function splitSeries(
  t: readonly number[],
  v: readonly (number | null | undefined)[],
  opts: SplitOptions = {},
): Segment[] {
  const n = Math.min(t.length, v.length);
  const segments: Segment[] = [];
  let current: Segment = [];
  let prevT: number | null = null;
  const flush = (): void => {
    if (current.length > 0) segments.push(current);
    current = [];
  };
  for (let i = 0; i < n; i++) {
    const ti = t[i];
    const vi = v[i];
    if (!isFiniteNumber(ti)) {
      flush();
      prevT = null;
      continue;
    }
    if (!isFiniteNumber(vi)) {
      // Missing sample: the line must stop here.
      flush();
      prevT = ti;
      continue;
    }
    if (current.length > 0 && prevT !== null) {
      const backwards = ti <= prevT;
      const tooFar = opts.maxStep !== undefined && ti - prevT > opts.maxStep;
      if (backwards || tooFar || gapBetween(prevT, ti, opts.gaps)) flush();
    }
    current.push({ x: ti, y: vi });
    prevT = ti;
  }
  flush();
  return segments;
}

export function extentOf(segments: readonly Segment[], extra: readonly (number | null | undefined)[] = []): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const seg of segments) {
    for (const p of seg) {
      if (p.y < lo) lo = p.y;
      if (p.y > hi) hi = p.y;
    }
  }
  for (const e of extra) {
    if (isFiniteNumber(e)) {
      if (e < lo) lo = e;
      if (e > hi) hi = e;
    }
  }
  return Number.isFinite(lo) && Number.isFinite(hi) ? [lo, hi] : null;
}

/** Pad an extent and enforce a minimum span so flat lines stay visible. */
export function paddedExtent(ext: [number, number], padFraction = 0.08, minSpan = 1e-6): [number, number] {
  let [lo, hi] = ext;
  if (hi - lo < minSpan) {
    const mid = (lo + hi) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  const pad = (hi - lo) * padFraction;
  return [lo - pad, hi + pad];
}

export type Scale = (x: number) => number;

export function linearScale(d0: number, d1: number, r0: number, r1: number): Scale {
  const span = d1 - d0;
  if (span === 0 || !Number.isFinite(span)) {
    const mid = (r0 + r1) / 2;
    return () => mid;
  }
  const k = (r1 - r0) / span;
  return (x: number) => r0 + (x - d0) * k;
}

/** "Nice" tick values (1, 2, 5 x 10^n steps) covering [min, max]. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || count < 1) return [];
  if (max < min) [min, max] = [max, min];
  if (max === min) return [min];
  const rough = (max - min) / count;
  const pow = 10 ** Math.floor(Math.log10(rough));
  const unit = rough / pow;
  const step = (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * pow;
  const start = Math.ceil(min / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= max + step * 1e-9 && ticks.length < 50; v += step) {
    // Round away float noise such as 0.30000000000000004.
    ticks.push(Number(v.toFixed(12)));
  }
  return ticks;
}

export function tickStep(ticks: readonly number[]): number {
  return ticks.length >= 2 ? Math.abs((ticks[1] ?? 0) - (ticks[0] ?? 0)) : 1;
}

export function formatTick(v: number, step: number): string {
  const decimals = step >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log10(step)));
  const s = v.toFixed(decimals);
  return s === '-0' ? '0' : s;
}

/** Clamp gap intervals to [t0, t1]; drop those outside. */
export function clipGaps(gaps: readonly GapInterval[], t0: number, t1: number): GapInterval[] {
  const out: GapInterval[] = [];
  for (const g of gaps) {
    if (!isFiniteNumber(g.start) || !isFiniteNumber(g.end) || g.end <= g.start) continue;
    const start = Math.max(t0, g.start);
    const end = Math.min(t1, g.end);
    if (end > start) out.push({ start, end });
  }
  return out;
}

/** Seconds relative to now (negative = in the past). */
export function relativeSeconds(tMs: number, nowMs: number): number {
  return (tMs - nowMs) / 1000;
}

export function toRelativeSeconds(t: readonly number[], nowMs: number): number[] {
  return t.map((x) => relativeSeconds(x, nowMs));
}

export function gapsToRelativeSeconds(gaps: readonly GapInterval[], nowMs: number): GapInterval[] {
  return gaps.map((g) => ({ start: relativeSeconds(g.start, nowMs), end: relativeSeconds(g.end, nowMs) }));
}

export interface SubcarrierSeries {
  k: number;
  t: number[];
  v: (number | null)[];
}

/**
 * Per-subcarrier time series from SignalSnapshot.amplitude.
 *
 * The backend contract (ProcessingEngine.signal_snapshot) is v[timeIndex]
 * [kIndex], where a whole `null` row is a gap marker. A subcarrier-major
 * matrix (v[kIndex][timeIndex]) is also accepted when the dimensions make it
 * unambiguous. Anything else yields no series (never a guess). At most
 * `maxSeries` subcarriers are returned, evenly spaced.
 */
export function amplitudeSeries(amp: SignalSnapshot['amplitude'] | null | undefined, maxSeries = 12): SubcarrierSeries[] {
  if (!amp || !Array.isArray(amp.t) || !Array.isArray(amp.k) || !Array.isArray(amp.v)) return [];
  const nT = amp.t.length;
  const nK = amp.k.length;
  if (nT === 0 || nK === 0) return [];
  const timeMajor =
    amp.v.length === nT && amp.v.every((row) => row === null || (Array.isArray(row) && row.length === nK));
  const kMajor = !timeMajor && amp.v.length === nK && amp.v.every((row) => Array.isArray(row) && row.length === nT);
  if (!timeMajor && !kMajor) return [];

  const pick: number[] = [];
  if (nK <= maxSeries) {
    for (let i = 0; i < nK; i++) pick.push(i);
  } else {
    for (let j = 0; j < maxSeries; j++) pick.push(Math.round((j * (nK - 1)) / (maxSeries - 1)));
  }
  return [...new Set(pick)].map((ki) => {
    const v: (number | null)[] = [];
    for (let ti = 0; ti < nT; ti++) {
      const cell = timeMajor ? amp.v[ti]?.[ki] : amp.v[ki]?.[ti];
      v.push(isFiniteNumber(cell) ? cell : null);
    }
    return { k: amp.k[ki] ?? ki, t: [...amp.t], v };
  });
}

export interface StateBand {
  start: number;
  end: number;
  state: ActivityState;
}

/**
 * Contiguous runs of the same activity state, for the colour band under the
 * score plot. A run extends to the next sample only when no gap lies between
 * them; a null state, a gap or a backwards step ends the run.
 */
export function stateBands(
  t: readonly number[],
  states: readonly (ActivityState | null | undefined)[],
  gaps: readonly GapInterval[] = [],
): StateBand[] {
  const n = Math.min(t.length, states.length);
  const bands: StateBand[] = [];
  let cur: StateBand | null = null;
  for (let i = 0; i < n; i++) {
    const ti = t[i];
    const si = states[i];
    if (!isFiniteNumber(ti) || !si) {
      if (cur) bands.push(cur);
      cur = null;
      continue;
    }
    if (cur && (ti <= cur.end || gapBetween(cur.end, ti, gaps))) {
      bands.push(cur);
      cur = null;
    }
    if (cur && cur.state === si) {
      cur.end = ti;
    } else {
      if (cur) {
        // Close the previous run at the boundary sample (no gap in between).
        cur.end = ti;
        bands.push(cur);
      }
      cur = { start: ti, end: ti, state: si };
    }
  }
  if (cur) bands.push(cur);
  return bands.filter((b) => b.end >= b.start);
}

/**
 * A defensive split threshold: `factor` x the median positive step of `t`.
 * Used so that an unreported hole (e.g. missing subcarriers in a profile, or
 * a long pause between samples) is not bridged either. Returns undefined when
 * there are too few samples to judge.
 */
export function autoMaxStep(t: readonly number[], factor = 4): number | undefined {
  const steps: number[] = [];
  for (let i = 1; i < t.length; i++) {
    const a = t[i - 1];
    const b = t[i];
    if (isFiniteNumber(a) && isFiniteNumber(b) && b > a) steps.push(b - a);
  }
  if (steps.length < 3) return undefined;
  steps.sort((x, y) => x - y);
  const mid = steps[Math.floor(steps.length / 2)] ?? 0;
  return mid > 0 ? mid * factor : undefined;
}

/** Evenly spaced colours from a perceptually ordered (viridis-like) ramp. */
export function rampColor(i: number, n: number): string {
  const stops: [number, number, number][] = [
    [68, 1, 84],
    [65, 68, 135],
    [42, 120, 142],
    [34, 168, 132],
    [122, 209, 81],
    [253, 231, 37],
  ];
  // Skip the darkest stop: it is unreadable on the dark background.
  const f = n <= 1 ? 0.5 : 0.18 + (0.82 * i) / (n - 1);
  const pos = f * (stops.length - 1);
  const lo = Math.floor(pos);
  const hi = Math.min(stops.length - 1, lo + 1);
  const w = pos - lo;
  const a = stops[lo] ?? stops[0]!;
  const b = stops[hi] ?? stops[stops.length - 1]!;
  const c = a.map((v, j) => Math.round(v + ((b[j] ?? v) - v) * w));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}
