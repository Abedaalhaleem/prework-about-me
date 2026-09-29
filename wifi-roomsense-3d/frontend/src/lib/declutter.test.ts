import { describe, expect, it } from 'vitest';
import {
  type LabelBox,
  candidateOffsets,
  declutterLabels,
  overlappingPairs,
  pointInPolygon,
  rectAt,
  rectsCollide,
} from './declutter';

const VIEW = { width: 1000, height: 600, gap: 3 };

const box = (id: string, x: number, y: number, patch: Partial<LabelBox> = {}): LabelBox => ({
  id,
  x,
  y,
  w: 120,
  h: 18,
  priority: 1,
  hideable: false,
  ...patch,
});

describe('declutterLabels', () => {
  it('keeps labels that do not overlap at their home position', () => {
    const labels = [box('a', 100, 100), box('b', 400, 100), box('c', 100, 300)];
    const out = declutterLabels(labels, VIEW);
    expect(out.map((p) => [p.id, p.dx, p.dy, p.hidden])).toEqual([
      ['a', 0, 0, false],
      ['b', 0, 0, false],
      ['c', 0, 0, false],
    ]);
  });

  it('moves a lower-priority label off a higher-priority one, which stays put', () => {
    // The zone label comes first in the input but has the lower priority.
    const labels = [box('zone', 505, 300, { priority: 4, hideable: true }), box('node', 500, 300, { priority: 1 })];
    const out = declutterLabels(labels, VIEW);
    expect(out[1]).toEqual({ id: 'node', dx: 0, dy: 0, hidden: false });
    expect(out[0]?.hidden).toBe(false);
    expect(out[0]?.dy).toBe(-(18 + 3)); // first free candidate: one row up
    expect(overlappingPairs(labels, out)).toEqual([]);
  });

  it('never hides node labels; overlapping nodes are nudged apart', () => {
    const labels = [box('tx', 500, 300), box('rx3', 540, 305)];
    const out = declutterLabels(labels, VIEW);
    expect(out.every((p) => !p.hidden)).toBe(true);
    expect(out[0]).toMatchObject({ dx: 0, dy: 0 });
    expect(overlappingPairs(labels, out)).toEqual([]);
  });

  it('hides a hideable label only when no free position is left; others overlap as little as possible', () => {
    // A view just big enough for one label: everything else is out of view or taken.
    const tiny = { width: 130, height: 22, gap: 2 };
    const labels = [
      box('node', 65, 11),
      box('zone', 65, 11, { priority: 4, hideable: true }),
      box('link', 65, 11, { priority: 2 }),
    ];
    const out = declutterLabels(labels, tiny);
    expect(out[0]).toMatchObject({ hidden: false, dx: 0, dy: 0 });
    expect(out[1]?.hidden).toBe(true);
    expect(out[2]?.hidden).toBe(false); // not hideable: placed anyway
  });

  it('prefers the previous offset so labels do not jump between redraws', () => {
    const prev = { dx: 0, dy: 18 + 3 }; // was one row DOWN last time; "up" is free too
    const labels = [box('node', 500, 300), box('link', 500, 300, { priority: 2, prev })];
    const out = declutterLabels(labels, VIEW);
    expect(out[1]).toMatchObject({ dx: prev.dx, dy: prev.dy, hidden: false });
    // Without a previous offset the first generic candidate (one row up) wins.
    expect(declutterLabels([labels[0] as LabelBox, box('link', 500, 300, { priority: 2 })], VIEW)[1]).toMatchObject({
      dx: 0,
      dy: -21,
    });
  });

  it('returns home as soon as home is free again', () => {
    const out = declutterLabels([box('link', 500, 300, { prev: { dx: 0, dy: -21 } })], VIEW);
    expect(out[0]).toMatchObject({ dx: 0, dy: 0 });
  });

  it('tries caller-supplied offsets (e.g. along the link line) before the generic ring', () => {
    const along = [
      { dx: -80, dy: 40 },
      { dx: 80, dy: -40 },
    ];
    const labels = [box('node', 500, 300), box('link', 500, 300, { priority: 2, extra: along })];
    const out = declutterLabels(labels, VIEW);
    expect(out[1]).toMatchObject({ dx: -80, dy: 40 });
  });

  it('keeps nudged labels inside the view and off overlay obstacles', () => {
    // Home is at the top edge and blocked; "one row up" would leave the view.
    const labels = [box('node', 500, 9), box('zone', 500, 9, { priority: 4, hideable: true })];
    const out = declutterLabels(labels, VIEW);
    const r = rectAt(labels[1] as LabelBox, out[1] as { dx: number; dy: number });
    expect(r.top).toBeGreaterThanOrEqual(0);
    expect(out[1]?.dy).toBeGreaterThan(0);

    // An overlay (e.g. the geometry label) covers the home position: the label moves below it.
    const obstacle = { left: 0, top: 0, right: 300, bottom: 50 };
    const under = declutterLabels([box('zone', 100, 40, { priority: 4, hideable: true })], { ...VIEW, obstacles: [obstacle] });
    const placed = rectAt({ x: 100, y: 40, w: 120, h: 18 }, under[0] as { dx: number; dy: number });
    expect(under[0]?.hidden).toBe(false);
    expect(rectsCollide(placed, obstacle, 3)).toBe(false);
    // Deep under a large overlay a hideable label is hidden; an important one is still placed.
    const big = { left: 0, top: 0, right: 1000, bottom: 200 };
    const deep = declutterLabels(
      [box('zone', 100, 40, { priority: 4, hideable: true }), box('node', 100, 40)],
      { ...VIEW, obstacles: [big] },
    );
    expect(deep[0]?.hidden).toBe(true);
    expect(deep[1]?.hidden).toBe(false);
  });

  it('declutters an EXAMPLE-room-like top-down row: TX label and three zone labels', () => {
    // Top-down view of the shipped EXAMPLE room: the zone centroids and the TX
    // node share one screen row, so every label overlaps its neighbour.
    const labels: LabelBox[] = [
      { id: 'zone-A', x: 395, y: 270, w: 160, h: 16, priority: 4, hideable: true },
      { id: 'zone-B', x: 500, y: 270, w: 165, h: 16, priority: 4, hideable: true },
      { id: 'zone-C', x: 605, y: 270, w: 160, h: 16, priority: 4, hideable: true },
      { id: 'node-tx1', x: 342, y: 252, w: 186, h: 18, priority: 1, hideable: false },
      { id: 'node-rx1', x: 670, y: 343, w: 176, h: 18, priority: 1, hideable: false },
      { id: 'link-tx1->rx1', x: 500, y: 315, w: 150, h: 16, priority: 2, hideable: false },
      { id: 'target', x: 342, y: 408, w: 80, h: 18, priority: 3, hideable: true },
    ];
    // Sanity: the home positions really do overlap.
    expect(overlappingPairs(labels, labels.map((l) => ({ id: l.id, dx: 0, dy: 0, hidden: false }))).length).toBeGreaterThan(0);

    const out = declutterLabels(labels, VIEW);
    expect(overlappingPairs(labels, out)).toEqual([]);
    for (const id of ['node-tx1', 'node-rx1', 'link-tx1->rx1']) {
      expect(out.find((p) => p.id === id)?.hidden).toBe(false);
    }
    expect(out.find((p) => p.id === 'node-tx1')).toMatchObject({ dx: 0, dy: 0 });
    // Nudging, not hiding, is enough here: every zone label stays readable.
    expect(out.filter((p) => p.id.startsWith('zone-') && !p.hidden)).toHaveLength(3);
  });

  it('keeps a moved zone label inside its own zone, or hides it', () => {
    // Zone B is a narrow vertical strip on screen (x 450..550); the node label blocks its home.
    const zoneB = [
      { x: 450, y: 150 },
      { x: 550, y: 150 },
      { x: 550, y: 450 },
      { x: 450, y: 450 },
    ];
    const labels = [box('node', 500, 300), box('zone-B', 500, 300, { priority: 4, hideable: true, region: zoneB })];
    const out = declutterLabels(labels, VIEW);
    expect(out[1]?.hidden).toBe(false);
    expect(out[1]?.dx).toBe(0); // sideways shifts would leave the strip
    expect(pointInPolygon({ x: 500 + (out[1]?.dx ?? 0), y: 300 + (out[1]?.dy ?? 0) }, zoneB)).toBe(true);

    // A zone too small to hold the label anywhere but home: hidden, never moved into a neighbour.
    const tiny = [
      { x: 490, y: 295 },
      { x: 510, y: 295 },
      { x: 510, y: 305 },
      { x: 490, y: 305 },
    ];
    const hidden = declutterLabels([box('node', 500, 300), box('zone', 500, 300, { priority: 4, hideable: true, region: tiny })], VIEW);
    expect(hidden[1]?.hidden).toBe(true);
    // Home is always allowed when it is free, even with an empty region.
    expect(declutterLabels([box('zone', 500, 300, { hideable: true, region: [] })], VIEW)[0]).toMatchObject({ hidden: false, dx: 0, dy: 0 });
  });

  it('retries with a failed label first when the input order was the problem', () => {
    // "b" can only sit at home (its region allows nothing else); "a" could also
    // move up. In input order "a" takes the shared spot first; the repair round
    // places "b" first and moves "a" instead, so nothing overlaps.
    const roomy = [
      { x: 300, y: 200 },
      { x: 700, y: 200 },
      { x: 700, y: 400 },
      { x: 300, y: 400 },
    ];
    const labels = [box('a', 500, 300, { priority: 2, region: roomy }), box('b', 520, 300, { priority: 2, region: [] })];
    const out = declutterLabels(labels, VIEW);
    expect(overlappingPairs(labels, out)).toEqual([]);
    expect(out[1]).toMatchObject({ id: 'b', dx: 0, dy: 0, hidden: false });
    expect(out[0]?.hidden).toBe(false);
    expect(out[0]?.dx !== 0 || out[0]?.dy !== 0).toBe(true);
  });

  it('returns placements in input order and is deterministic', () => {
    const labels = [box('c', 500, 300, { priority: 3 }), box('a', 500, 300, { priority: 1 }), box('b', 500, 300, { priority: 2 })];
    const a = declutterLabels(labels, VIEW);
    expect(a.map((p) => p.id)).toEqual(['c', 'a', 'b']);
    expect(declutterLabels(labels, VIEW)).toEqual(a);
    expect(overlappingPairs(labels, a)).toEqual([]);
  });
});

describe('pointInPolygon', () => {
  it('handles convex and concave polygons', () => {
    const square = [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 10, y: 10 },
      { x: 0, y: 10 },
    ];
    expect(pointInPolygon({ x: 5, y: 5 }, square)).toBe(true);
    expect(pointInPolygon({ x: 15, y: 5 }, square)).toBe(false);
    const ell = [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 10, y: 4 },
      { x: 4, y: 4 },
      { x: 4, y: 10 },
      { x: 0, y: 10 },
    ];
    expect(pointInPolygon({ x: 2, y: 8 }, ell)).toBe(true);
    expect(pointInPolygon({ x: 8, y: 8 }, ell)).toBe(false);
    expect(pointInPolygon({ x: 1, y: 1 }, [])).toBe(false);
  });
});

describe('candidateOffsets', () => {
  it('starts with one row up and down and never repeats home', () => {
    const c = candidateOffsets(100, 20, 2);
    expect(c[0]).toEqual({ dx: 0, dy: -22 });
    expect(c[1]).toEqual({ dx: 0, dy: 22 });
    expect(c.some((o) => o.dx === 0 && o.dy === 0)).toBe(false);
  });
});
