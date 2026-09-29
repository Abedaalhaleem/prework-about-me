import { describe, expect, it } from 'vitest';
import { fromThree, polygonCentroid, roomBounds, toThree, wallSpans } from './coords';
import { blankRoom } from './roomValidation';
import type { Door, Wall } from '../api/types';

describe('backend -> three.js coordinates', () => {
  it('maps x east / y north / z up to (x, z, -y)', () => {
    expect(toThree({ x: 1, y: 2, z: 3 })).toEqual([1, 3, -2]);
    expect(toThree({ x: 1, y: 0 })).toEqual([1, 0, 0]);
  });
  it('round-trips', () => {
    const p = { x: 1.5, y: -2.25, z: 0.8 };
    expect(fromThree(toThree(p))).toEqual(p);
  });
  it('is a proper rotation (keeps handedness)', () => {
    // east x north = up in the backend frame; the images must satisfy the same.
    const e = toThree({ x: 1, y: 0, z: 0 });
    const n = toThree({ x: 0, y: 1, z: 0 });
    const u = toThree({ x: 0, y: 0, z: 1 });
    const cross = [e[1] * n[2] - e[2] * n[1], e[2] * n[0] - e[0] * n[2], e[0] * n[1] - e[1] * n[0]];
    cross.forEach((c, i) => expect(c).toBeCloseTo(u[i] ?? Number.NaN));
  });
});

describe('polygonCentroid', () => {
  it('matches the backend display anchor for a rectangle', () => {
    expect(polygonCentroid([
      { x: 0, y: 0 },
      { x: 4, y: 0 },
      { x: 4, y: 2 },
      { x: 0, y: 2 },
    ])).toEqual({ x: 2, y: 1 });
  });
  it('falls back to the mean for degenerate polygons', () => {
    expect(polygonCentroid([
      { x: 0, y: 0 },
      { x: 1, y: 1 },
      { x: 2, y: 2 },
    ])).toEqual({ x: 1, y: 1 });
    expect(polygonCentroid([])).toBeNull();
  });
});

describe('wallSpans', () => {
  const wall: Wall = {
    id: 'w1',
    start: { x: 0, y: 0 },
    end: { x: 5, y: 0 },
    height_m: 2.5,
    thickness_m: 0.1,
    material: null,
    is_target_room_boundary: true,
  };
  const door = (id: string, offset: number, width: number): Door => ({ id, wall_id: 'w1', offset_m: offset, width_m: width, height_m: 2 });

  it('cuts a door gap out of the wall', () => {
    const s = wallSpans(wall, [door('d1', 1, 0.9)]);
    expect(s.solid).toEqual([
      [0, 1],
      [1.9, 5],
    ]);
    expect(s.openings[0]).toMatchObject({ from: 1, to: 1.9, height: 2 });
  });
  it('merges overlapping doors and clamps to the wall', () => {
    const s = wallSpans(wall, [door('a', 1, 1), door('b', 1.5, 1), door('c', 4.5, 2)]);
    expect(s.openings.map((o) => [o.from, o.to])).toEqual([
      [1, 2.5],
      [4.5, 5],
    ]);
    expect(s.solid).toEqual([
      [0, 1],
      [2.5, 4.5],
    ]);
  });
  it('ignores doors on other walls', () => {
    expect(wallSpans(wall, [{ ...door('x', 1, 1), wall_id: 'other' }]).solid).toEqual([[0, 5]]);
  });
});

describe('roomBounds', () => {
  it('covers the declared extent and nodes outside it', () => {
    const room = blankRoom(4, 3, 2.5);
    room.nodes.push({
      id: 'rx',
      role: 'RX',
      label: 'rx',
      position: { x: -1, y: 5, z: 1 },
      inside_target_room: false,
      device_mac: null,
    });
    expect(roomBounds(room)).toEqual({ minX: -1, maxX: 4, minY: 0, maxY: 5 });
  });
});
