import { describe, expect, it } from 'vitest';
import { RIBBON, dbmToHeight, ribbonSegments, strengthColor, timeToX } from './ribbon';

describe('3-D signal ribbon helpers', () => {
  it('maps dBm to height and clamps outside the range', () => {
    expect(dbmToHeight(RIBBON.dbmFloor)).toBe(0);
    expect(dbmToHeight(RIBBON.dbmTop)).toBe(RIBBON.height);
    expect(dbmToHeight(-200)).toBe(0);
    expect(dbmToHeight(0)).toBe(RIBBON.height);
  });

  it('maps the time window onto the x axis', () => {
    expect(timeToX(-120, 120)).toBe(RIBBON.xMin);
    expect(timeToX(0, 120)).toBe(RIBBON.xMax);
  });

  it('breaks at missing readings and long gaps; never interpolates', () => {
    const t = [-10, -9.5, -9, -8.5, -3, -2.5];
    const v = [-60, -61, null, -62, -63, -64];
    const segs = ribbonSegments(t, v, 120, 1.5);
    expect(segs.map((s) => s.map((p) => p.dbm))).toEqual([[-60, -61], [-62], [-63, -64]]);
    const total = segs.reduce((n, s) => n + s.length, 0);
    expect(total).toBe(5); // exactly the real readings, no extra points
  });

  it('drops samples outside the window', () => {
    expect(ribbonSegments([-200, -1], [-50, -51], 120, 5).flat().map((p) => p.dbm)).toEqual([-51]);
  });

  it('colours weak signals red and strong ones green', () => {
    const weak = strengthColor(-90);
    const strong = strengthColor(-45);
    expect(weak[0]).toBeGreaterThan(weak[1]);
    expect(strong[1]).toBeGreaterThan(strong[0]);
  });
});
