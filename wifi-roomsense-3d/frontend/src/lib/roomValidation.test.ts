import { describe, expect, it } from 'vitest';
import { blankRoom, perimeterWalls, validateRoom } from './roomValidation';

describe('validateRoom', () => {
  it('accepts a blank room (with a warning about the missing outline)', () => {
    const issues = validateRoom(blankRoom());
    expect(issues.errors).toEqual([]);
    expect(issues.warnings.some((w) => /target-room outline/.test(w))).toBe(true);
  });

  it('mirrors the backend constraints', () => {
    const r = blankRoom();
    r.width_m = 0;
    r.height_m = 25;
    r.walls = perimeterWalls(4, 3, 2.5);
    r.doors.push({ id: 'd1', wall_id: 'nope', offset_m: 0, width_m: 0.9, height_m: 2 });
    r.nodes.push(
      { id: 'n1', role: 'TX', label: 'a', position: { x: 0, y: 0, z: 1 }, inside_target_room: true, device_mac: null },
      { id: 'n1', role: 'RX', label: 'b', position: { x: 1, y: 1, z: Number.NaN }, inside_target_room: true, device_mac: null },
    );
    r.links.push({ link_id: 'l1', transmitter_id: 'n1', receiver_id: 'ghost' });
    r.zones.push({ id: 'z', label: 'z', kind: 'TARGET_ROOM_ZONE', polygon: [{ x: 0, y: 0 }, { x: 1, y: 1 }] });
    const { errors } = validateRoom(r);
    const all = errors.join('\n');
    expect(all).toMatch(/width/);
    expect(all).toMatch(/height must be ≤ 20/);
    expect(all).toMatch(/unknown wall/);
    expect(all).toMatch(/Duplicate sensor node id "n1"/);
    expect(all).toMatch(/invalid x\/y\/z/);
    expect(all).toMatch(/unknown node/);
    expect(all).toMatch(/at least 3 polygon points/);
  });

  it('generates four closed perimeter walls', () => {
    const walls = perimeterWalls(4, 3, 2.5);
    expect(walls).toHaveLength(4);
    expect(walls[3]?.end).toEqual(walls[0]?.start);
  });
});
