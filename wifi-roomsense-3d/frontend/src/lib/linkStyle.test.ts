import { describe, expect, it } from 'vitest';
import { LEGEND_ORDER, linkStyle } from './linkStyle';

describe('linkStyle', () => {
  it('uses a warm colour for motion and a cool one for no motion', () => {
    const motion = linkStyle('MOTION_DETECTED');
    const quiet = linkStyle('NO_MOTION_DETECTED');
    expect(motion.color).not.toBe(quiet.color);
    // warm: red channel dominates; cool: blue channel dominates
    expect(motion.color >> 16).toBeGreaterThan(motion.color & 0xff);
    expect(quiet.color & 0xff).toBeGreaterThan(quiet.color >> 16);
    expect(motion.dashed).toBe(false);
  });

  it('draws SENSOR_OFFLINE dashed dark red and stale/no-data grey dashed', () => {
    const off = linkStyle('SENSOR_OFFLINE');
    expect(off.dashed).toBe(true);
    expect(off.color >> 16).toBeGreaterThan((off.color >> 8) & 0xff);
    for (const s of ['STALE', 'NO_DATA'] as const) {
      const st = linkStyle(s);
      expect(st.dashed).toBe(true);
      const r = st.color >> 16;
      const b = st.color & 0xff;
      expect(Math.abs(r - b)).toBeLessThan(40); // greyish
    }
  });

  it('UNKNOWN and CALIBRATING are grey, not "no motion" blue', () => {
    expect(linkStyle('UNKNOWN').color).not.toBe(linkStyle('NO_MOTION_DETECTED').color);
    expect(linkStyle('CALIBRATING').color).not.toBe(linkStyle('NO_MOTION_DETECTED').color);
  });

  it('never describes no-motion as an empty room', () => {
    for (const s of LEGEND_ORDER) {
      // Strip the explicit disclaimer, then nothing may claim the room is empty.
      const text = linkStyle(s).description.toLowerCase().replace('does not mean the room is empty', '');
      expect(text).not.toMatch(/room is empty|empty room|nobody|no one/);
    }
    expect(linkStyle('NO_MOTION_DETECTED').description).toMatch(/does not mean the room is empty/);
  });
});
