import { describe, expect, it } from 'vitest';
import type { Door, RoomGeometry, Wall } from '../api/types';
import {
  DEFAULT_PARAMS,
  coverageRgba,
  crossingT,
  fsplAt1mDb,
  gridRange,
  levelFraction,
  predictDbm,
  predictGrid,
  wallLossDb,
} from './propagation';

const wall = (id: string, x: number, material: string | null = 'drywall'): Wall => ({
  id,
  start: { x, y: -5 },
  end: { x, y: 5 },
  height_m: 2.5,
  thickness_m: 0.12,
  material,
  is_target_room_boundary: false,
});

const ROOM_FOR_COLOURS = {
  width_m: 4,
  depth_m: 3,
  walls: [wall('w', 2)],
  doors: [],
} as unknown as RoomGeometry;

describe('simulated propagation model', () => {
  it('free-space loss at 1 m is about 40 dB at 2.4 GHz', () => {
    expect(fsplAt1mDb(2.4)).toBeCloseTo(40.05, 1);
  });

  it('maps wall materials to assumed losses', () => {
    expect(wallLossDb('Drywall')).toBe(3);
    expect(wallLossDb('brick')).toBe(9);
    expect(wallLossDb('reinforced concrete')).toBe(12);
    expect(wallLossDb(null)).toBe(5);
    expect(wallLossDb('drywall', 5)).toBe(5);
  });

  it('detects crossings, not near misses', () => {
    expect(crossingT({ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 1, y: -1 }, { x: 1, y: 1 })).toBeCloseTo(0.5);
    expect(crossingT({ x: 0, y: 0 }, { x: 0.5, y: 0 }, { x: 1, y: -1 }, { x: 1, y: 1 })).toBeNull();
  });

  it('gets weaker with distance and with each wall crossed', () => {
    const tx = { x: 0, y: 0 };
    const near = predictDbm(tx, { x: 1, y: 0 }, [], [], DEFAULT_PARAMS);
    const far = predictDbm(tx, { x: 4, y: 0 }, [], [], DEFAULT_PARAMS);
    expect(far.dbm).toBeLessThan(near.dbm);
    const walls = [wall('a', 2), wall('b', 3, 'concrete')];
    const behind = predictDbm(tx, { x: 4, y: 0 }, walls, [], DEFAULT_PARAMS);
    expect(behind.wallsCrossed).toBe(2);
    expect(far.dbm - behind.dbm).toBeCloseTo(3 + 12, 5);
  });

  it('loses less through a door opening', () => {
    const tx = { x: 0, y: 0 };
    const w = wall('a', 2, 'brick');
    const door: Door = { id: 'd', wall_id: 'a', offset_m: 4.5, width_m: 1, height_m: 2 }; // covers y in [-0.5, 0.5]
    const viaDoor = predictDbm(tx, { x: 4, y: 0 }, [w], [door], DEFAULT_PARAMS);
    const viaWall = predictDbm(tx, { x: 4, y: 3 }, [w], [door], DEFAULT_PARAMS);
    expect(viaDoor.lossDb).toBeLessThan(viaWall.lossDb);
  });

  it('scales colours to the grid range: strongest bright and opaque, weakest faint', () => {
    const g = predictGrid(ROOM_FOR_COLOURS, { x: 1, y: 1 });
    const { lo, hi } = gridRange(g);
    expect(hi).toBeGreaterThan(lo);
    const strong = coverageRgba(hi, lo, hi);
    const weak = coverageRgba(lo, lo, hi);
    expect(strong[3]).toBeGreaterThan(weak[3]);
    expect(strong[1]).toBeGreaterThan(weak[1]);
    expect(levelFraction(lo - 10, lo, hi)).toBe(0);
    expect(levelFraction(hi + 10, lo, hi)).toBe(1);
  });
});

