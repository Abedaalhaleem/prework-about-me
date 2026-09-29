import { describe, expect, it } from 'vitest';
import {
  amplitudeSeries,
  clipGaps,
  extentOf,
  gapBetween,
  linearScale,
  niceTicks,
  paddedExtent,
  splitSeries,
  stateBands,
  formatTick,
  autoMaxStep,
} from './plotting';

describe('splitSeries', () => {
  it('splits the series at null samples (never bridges them)', () => {
    const segs = splitSeries([0, 1, 2, 3, 4, 5], [1, 2, null, 4, 5, 6]);
    expect(segs).toHaveLength(2);
    expect(segs[0]).toEqual([
      { x: 0, y: 1 },
      { x: 1, y: 2 },
    ]);
    expect(segs[1]?.map((p) => p.x)).toEqual([3, 4, 5]);
    // No segment contains a point on both sides of the null sample.
    for (const s of segs) expect(s.some((p) => p.x < 2) && s.some((p) => p.x > 2)).toBe(false);
  });

  it('treats consecutive nulls, NaN and undefined as missing', () => {
    const segs = splitSeries([0, 1, 2, 3, 4, 5], [1, null, null, Number.NaN, undefined, 6]);
    expect(segs.map((s) => s.length)).toEqual([1, 1]);
  });

  it('returns no segments for an all-null series', () => {
    expect(splitSeries([0, 1, 2], [null, null, null])).toEqual([]);
  });

  it('splits across a reported gap interval even without a null sample', () => {
    const segs = splitSeries([0, 1, 5, 6], [1, 1, 1, 1], { gaps: [{ start: 1.5, end: 4.5 }] });
    expect(segs.map((s) => s.map((p) => p.x))).toEqual([
      [0, 1],
      [5, 6],
    ]);
  });

  it('splits on a too-large time step and on backwards time', () => {
    expect(splitSeries([0, 1, 10, 11], [1, 2, 3, 4], { maxStep: 2 })).toHaveLength(2);
    expect(splitSeries([0, 2, 1, 3], [1, 2, 3, 4])).toHaveLength(2);
  });

  it('uses the shorter of t and v', () => {
    expect(splitSeries([0, 1, 2], [1, 2])[0]).toHaveLength(2);
  });
});

describe('gapBetween / clipGaps', () => {
  it('detects overlap with the open interval only', () => {
    expect(gapBetween(0, 10, [{ start: 10, end: 12 }])).toBe(false);
    expect(gapBetween(0, 10, [{ start: 9, end: 12 }])).toBe(true);
  });
  it('clips gaps to the plotted window and drops invalid ones', () => {
    expect(
      clipGaps(
        [
          { start: -5, end: 2 },
          { start: 20, end: 30 },
          { start: 5, end: 4 },
        ],
        0,
        10,
      ),
    ).toEqual([{ start: 0, end: 2 }]);
  });
});

describe('scales and ticks', () => {
  it('maps linearly and handles a zero-span domain', () => {
    const s = linearScale(0, 10, 0, 100);
    expect(s(5)).toBe(50);
    expect(linearScale(3, 3, 0, 100)(3)).toBe(50);
  });
  it('produces nice ticks', () => {
    expect(niceTicks(0, 10, 5)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(niceTicks(-60, 0, 6)).toEqual([-60, -50, -40, -30, -20, -10, 0]);
    expect(niceTicks(0.1, 0.35, 5)).toEqual([0.1, 0.15, 0.2, 0.25, 0.3, 0.35]);
    expect(formatTick(0.15, 0.05)).toBe('0.15');
  });
  it('computes extents including thresholds and pads flat lines', () => {
    const ext = extentOf([[{ x: 0, y: 1 }]], [4, null, 2.5]);
    expect(ext).toEqual([1, 4]);
    const [lo, hi] = paddedExtent([2, 2], 0.1, 1);
    expect(hi - lo).toBeCloseTo(1.2);
    expect(extentOf([], [])).toBeNull();
  });
});

describe('amplitudeSeries', () => {
  it('reads a frames x subcarriers matrix', () => {
    const series = amplitudeSeries({ t: [0, 1, 2], k: [-2, 2], v: [[1, 10], [null, 11], [3, 12]] });
    expect(series.map((s) => s.k)).toEqual([-2, 2]);
    expect(series[0]?.v).toEqual([1, null, 3]);
    expect(series[1]?.v).toEqual([10, 11, 12]);
  });
  it('reads a subcarriers x frames matrix when unambiguous', () => {
    const series = amplitudeSeries({ t: [0, 1, 2], k: [5, 6], v: [[1, 2, 3], [4, null, 6]] });
    expect(series[1]?.v).toEqual([4, null, 6]);
  });
  it('treats a null row as a gap marker (backend contract) and splits there', () => {
    const series = amplitudeSeries({ t: [0, 1, 2, 3], k: [7, 8], v: [[1, 2], null, [3, 4], [5, 6]] });
    expect(series[0]?.v).toEqual([1, null, 3, 5]);
    const segs = splitSeries(series[0]?.t ?? [], series[0]?.v ?? []);
    expect(segs.map((s) => s.map((p) => p.x))).toEqual([[0], [2, 3]]);
  });
  it('refuses a malformed matrix instead of guessing', () => {
    expect(amplitudeSeries({ t: [0, 1, 2], k: [5, 6], v: [[1, 2], [3]] })).toEqual([]);
  });
  it('limits the number of subcarriers, evenly spaced', () => {
    const k = Array.from({ length: 52 }, (_, i) => i);
    const v = [k.map((x) => x)];
    const series = amplitudeSeries({ t: [0], k, v }, 4);
    expect(series.map((s) => s.k)).toEqual([0, 17, 34, 51]);
  });
});

describe('stateBands', () => {
  it('groups runs and breaks at null states and gaps', () => {
    const bands = stateBands(
      [0, 1, 2, 3, 4, 10, 11],
      ['NO_MOTION_DETECTED', 'NO_MOTION_DETECTED', 'MOTION_DETECTED', null, 'UNKNOWN', 'UNKNOWN', 'UNKNOWN'],
      [{ start: 5, end: 9 }],
    );
    expect(bands).toEqual([
      { start: 0, end: 2, state: 'NO_MOTION_DETECTED' },
      { start: 2, end: 2, state: 'MOTION_DETECTED' },
      { start: 4, end: 4, state: 'UNKNOWN' },
      { start: 10, end: 11, state: 'UNKNOWN' },
    ]);
  });
});

describe('autoMaxStep', () => {
  it('uses a multiple of the median step and ignores bad steps', () => {
    expect(autoMaxStep([0, 1, 2, 3, 10], 4)).toBe(4);
    expect(autoMaxStep([0, 1], 4)).toBeUndefined();
  });
  it('splits a profile at missing subcarriers', () => {
    const k = [-3, -2, -1, 1, 2, 3];
    // Default factor 4 would not split a 2-step hole; the plot uses 1.5.
    const segs = splitSeries(k, [1, 1, 1, 1, 1, 1], { maxStep: autoMaxStep(k, 1.5) });
    expect(segs).toHaveLength(2);
  });
});
