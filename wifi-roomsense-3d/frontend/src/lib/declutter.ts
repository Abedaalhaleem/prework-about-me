/**
 * Screen-space label decluttering for the 3-D room view.
 *
 * Pure geometry, no DOM: RoomScene projects each label anchor to the screen,
 * measures the label, and calls declutterLabels() after every redraw.
 *
 * Labels are placed greedily in priority order (lower number first; the
 * scene uses estimate/disclaimer < node < link < target < zone < compass).
 * Each label tries, in order:
 *   1. its home position;
 *   2. the offset it had in the previous pass (so labels do not jump around
 *      while a text or the camera changes a little);
 *   3. caller-supplied offsets (e.g. other points along a link's own line);
 *   4. a fixed ring of nearby offsets: one row up/down, half a label left or
 *      right, then two rows, a full label width sideways, then three rows.
 * Any position but home must lie fully inside the view and, when the label
 * has a `region` (a zone's outline on screen), keep its centre inside that
 * region: a zone label moved over another zone would mislabel the floor, so
 * it is hidden instead. A label that finds no free position is hidden when it
 * is hideable; otherwise it is placed where it overlaps least (important
 * labels are never hidden).
 *
 * Greedy placement depends on the order within a priority class, so when a
 * label had to overlap or be hidden, placement is repeated (a few times at
 * most) with that label first in its class, and the better result is kept.
 */

export interface Offset {
  dx: number;
  dy: number;
}

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface LabelBox {
  id: string;
  /** Centre of the label at its home position, CSS px in view coordinates. */
  x: number;
  y: number;
  /** Measured size, CSS px. */
  w: number;
  h: number;
  /** Lower = more important = placed first. */
  priority: number;
  /** May be hidden when no free position exists. */
  hideable: boolean;
  /** Offset chosen in the previous pass, if the label was shown. */
  prev?: Offset | null;
  /** Preferred alternative offsets, tried before the generic ring. */
  extra?: readonly Offset[];
  /**
   * Screen polygon the label's centre must stay in when moved (home is always
   * allowed). An empty polygon allows home only.
   */
  region?: readonly Point[];
}

export interface LabelPlacement {
  id: string;
  dx: number;
  dy: number;
  hidden: boolean;
}

export interface DeclutterOptions {
  /** View size, CSS px. */
  width: number;
  height: number;
  /** Minimum free space between two labels, px (default 2). */
  gap?: number;
  /** Areas that labels must not cover (overlays drawn on top of the view). */
  obstacles?: readonly Rect[];
}

export function rectAt(l: Pick<LabelBox, 'x' | 'y' | 'w' | 'h'>, o: Offset): Rect {
  const cx = l.x + o.dx;
  const cy = l.y + o.dy;
  return { left: cx - l.w / 2, top: cy - l.h / 2, right: cx + l.w / 2, bottom: cy + l.h / 2 };
}

/** True when the two rectangles are closer than `gap` (touching at exactly `gap` is fine). */
export function rectsCollide(a: Rect, b: Rect, gap = 0): boolean {
  return a.left < b.right + gap && b.left < a.right + gap && a.top < b.bottom + gap && b.top < a.bottom + gap;
}

function overlapArea(a: Rect, b: Rect): number {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return w > 0 && h > 0 ? w * h : 0;
}

function outsideArea(r: Rect, width: number, height: number): number {
  const total = (r.right - r.left) * (r.bottom - r.top);
  return total - overlapArea(r, { left: 0, top: 0, right: width, bottom: height });
}

function insideView(r: Rect, width: number, height: number): boolean {
  return r.left >= 0 && r.top >= 0 && r.right <= width && r.bottom <= height;
}

/** Even-odd point-in-polygon test (boundary counts as inside or outside arbitrarily). */
export function pointInPolygon(p: Point, poly: readonly Point[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i] as Point;
    const b = poly[j] as Point;
    if (a.y > p.y !== b.y > p.y && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

function allowedByRegion(l: LabelBox, o: Offset): boolean {
  if (!l.region) return true;
  if (o.dx === 0 && o.dy === 0) return true;
  return pointInPolygon({ x: l.x + o.dx, y: l.y + o.dy }, l.region);
}

/** Generic nearby positions for a w×h label, nearest first (home excluded). */
export function candidateOffsets(w: number, h: number, gap: number): Offset[] {
  const row = h + gap;
  const half = w / 2 + gap; // the label's near edge sits right next to the anchor
  const full = w + gap; // clears a same-sized label centred on the same anchor
  return [
    { dx: 0, dy: -row },
    { dx: 0, dy: row },
    { dx: half, dy: 0 },
    { dx: -half, dy: 0 },
    { dx: half, dy: -row },
    { dx: -half, dy: -row },
    { dx: half, dy: row },
    { dx: -half, dy: row },
    { dx: 0, dy: -2 * row },
    { dx: 0, dy: 2 * row },
    { dx: full, dy: 0 },
    { dx: -full, dy: 0 },
    { dx: half, dy: -2 * row },
    { dx: -half, dy: -2 * row },
    { dx: half, dy: 2 * row },
    { dx: -half, dy: 2 * row },
    { dx: 0, dy: -3 * row },
    { dx: 0, dy: 3 * row },
  ];
}

function sameOffset(a: Offset, b: Offset): boolean {
  return Math.abs(a.dx - b.dx) < 0.5 && Math.abs(a.dy - b.dy) < 0.5;
}

function orderedCandidates(l: LabelBox, gap: number): Offset[] {
  const out: Offset[] = [{ dx: 0, dy: 0 }];
  const push = (o: Offset): void => {
    if (Number.isFinite(o.dx) && Number.isFinite(o.dy) && !out.some((c) => sameOffset(c, o))) out.push(o);
  };
  if (l.prev) push(l.prev);
  (l.extra ?? []).forEach(push);
  candidateOffsets(l.w, l.h, gap).forEach(push);
  return out;
}

interface PassResult {
  placements: LabelPlacement[];
  /** Indices of labels that were hidden or had to overlap. */
  failed: number[];
  overlapArea: number;
}

function placeOnce(labels: readonly LabelBox[], rank: readonly number[], opts: DeclutterOptions): PassResult {
  const gap = opts.gap ?? 2;
  const taken: Rect[] = [...(opts.obstacles ?? [])];
  const order = labels
    .map((l, i) => ({ l, i }))
    .sort((a, b) => a.l.priority - b.l.priority || (rank[a.i] ?? a.i) - (rank[b.i] ?? b.i) || a.i - b.i);
  const placements: LabelPlacement[] = labels.map((l) => ({ id: l.id, dx: 0, dy: 0, hidden: true }));
  const failed: number[] = [];
  let overlapTotal = 0;

  for (const { l, i } of order) {
    const cands = orderedCandidates(l, gap).filter((c) => allowedByRegion(l, c));
    let chosen: Offset | null = null;
    for (let k = 0; k < cands.length && chosen === null; k++) {
      const c = cands[k] as Offset;
      const r = rectAt(l, c);
      if (k > 0 && !insideView(r, opts.width, opts.height)) continue;
      if (!taken.some((t) => rectsCollide(t, r, gap))) chosen = c;
    }
    if (chosen === null) {
      failed.push(i);
      if (!l.hideable) {
        // Never hidden: take the position that covers the least of what is
        // already placed (and of the area outside the view).
        let best = Infinity;
        for (const c of cands) {
          const r = rectAt(l, c);
          const covered = taken.reduce((sum, t) => sum + overlapArea(t, r), 0);
          const score = covered + outsideArea(r, opts.width, opts.height);
          if (score < best - 1e-6) {
            best = score;
            chosen = c;
          }
        }
        overlapTotal += Number.isFinite(best) ? best : 0;
      }
    }
    if (chosen !== null) {
      taken.push(rectAt(l, chosen));
      placements[i] = { id: l.id, dx: chosen.dx, dy: chosen.dy, hidden: false };
    }
  }
  return { placements, failed, overlapArea: overlapTotal };
}

/** Lower is better: fewer overlapping important labels, then fewer hidden ones. */
function badness(r: PassResult, labels: readonly LabelBox[]): [number, number, number] {
  const overlapping = r.failed.filter((i) => !labels[i]?.hideable).length;
  return [overlapping, r.failed.length - overlapping, r.overlapArea];
}

function better(a: [number, number, number], b: [number, number, number]): boolean {
  return a[0] !== b[0] ? a[0] < b[0] : a[1] !== b[1] ? a[1] < b[1] : a[2] < b[2] - 1e-6;
}

const REPAIR_ROUNDS = 3;

/** Placements in the same order as `labels`. */
export function declutterLabels(labels: readonly LabelBox[], opts: DeclutterOptions): LabelPlacement[] {
  const rank = labels.map((_, i) => i);
  let best = placeOnce(labels, rank, opts);
  let bestScore = badness(best, labels);
  let current = best;
  for (let round = 0; round < REPAIR_ROUNDS && current.failed.length > 0; round++) {
    // Place the labels that failed first within their priority class (ranks
    // below every earlier rank, so the latest failures go first).
    let front = -(round + 1) * labels.length;
    for (const i of current.failed) rank[i] = front++;
    current = placeOnce(labels, rank, opts);
    const score = badness(current, labels);
    if (better(score, bestScore)) {
      best = current;
      bestScore = score;
    }
  }
  return best.placements;
}

/** Pairs of shown labels that still overlap (for tests and diagnostics). */
export function overlappingPairs(labels: readonly LabelBox[], placements: readonly LabelPlacement[]): [string, string][] {
  const shown = labels
    .map((l, i) => ({ l, p: placements[i] }))
    .filter((e): e is { l: LabelBox; p: LabelPlacement } => !!e.p && !e.p.hidden)
    .map((e) => ({ id: e.l.id, r: rectAt(e.l, e.p) }));
  const pairs: [string, string][] = [];
  for (let a = 0; a < shown.length; a++) {
    for (let b = a + 1; b < shown.length; b++) {
      const A = shown[a];
      const B = shown[b];
      if (A && B && rectsCollide(A.r, B.r)) pairs.push([A.id, B.id]);
    }
  }
  return pairs;
}
