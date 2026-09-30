// Automatic navigation graph: sample walkable surfaces on a grid (multi-level,
// so stairs and second floors work), connect neighbours with a walk simulation,
// then run A* for the bots.
import * as THREE from 'three';

const R = 0.4; // clearance radius
const H = 1.7; // clearance height
const STEP = 0.5; // obstacles lower than this are stepped over

export class NavGraph {
  constructor(world, bounds, spacing = 1) {
    this.world = world;
    this.nodes = [];
    this.cols = new Map();
    this.spacing = spacing;
    this._build(bounds);
  }

  _key(ix, iz) { return ix * 1000 + iz; }

  _build(bounds) {
    const w = this.world, sp = this.spacing;
    const raw = [];
    const colMap = new Map();
    const list = [];
    for (let x = -bounds.x + 1; x <= bounds.x - 1 + 1e-6; x += sp) {
      for (let z = -bounds.z + 1; z <= bounds.z - 1 + 1e-6; z += sp) {
        const ix = Math.round(x / sp), iz = Math.round(z / sp);
        w.query(x - 0.01, -5, z - 0.01, x + 0.01, 12, z + 0.01, list);
        const ys = new Set();
        for (const b of list) if (b.max.y < 11) ys.add(Math.round(b.max.y * 1000) / 1000);
        for (const y of ys) {
          // surface must not be buried inside another solid, and needs headroom
          if (w.overlaps(x - 0.02, y + 0.01, z - 0.02, x + 0.02, y + 0.3, z + 0.02)) continue;
          if (w.overlaps(x - R, y + STEP, z - R, x + R, y + H, z + R)) continue;
          const n = { x, y, z, ix, iz, edges: [], cost: [], id: raw.length };
          raw.push(n);
          const k = this._key(ix, iz);
          if (!colMap.has(k)) colMap.set(k, []);
          colMap.get(k).push(n);
        }
      }
    }
    // Edges
    const dirs = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];
    const a = new THREE.Vector3(), b = new THREE.Vector3();
    for (const n of raw) {
      for (const [dx, dz] of dirs) {
        const col = colMap.get(this._key(n.ix + dx, n.iz + dz));
        if (!col) continue;
        for (const m of col) {
          if (Math.abs(m.y - n.y) > 1.2) continue;
          a.set(n.x, n.y, n.z);
          b.set(m.x, m.y, m.z);
          if (this.walkable(a, b)) {
            n.edges.push(m.id);
            n.cost.push(Math.hypot(m.x - n.x, m.y - n.y, m.z - n.z));
          }
        }
      }
    }
    // Keep the largest strongly-connected region reachable from the street
    let seed = null, bestD = Infinity;
    for (const n of raw) {
      const d = Math.hypot(n.x - 0, n.z - 8) + Math.abs(n.y - 0.12) * 5;
      if (d < bestD) { bestD = d; seed = n; }
    }
    const fwd = new Uint8Array(raw.length), bwd = new Uint8Array(raw.length);
    const rev = raw.map(() => []);
    raw.forEach((n) => n.edges.forEach((e) => rev[e].push(n.id)));
    const flood = (start, mark, adj) => {
      const st = [start.id];
      mark[start.id] = 1;
      while (st.length) {
        const i = st.pop();
        for (const j of adj(i)) if (!mark[j]) { mark[j] = 1; st.push(j); }
      }
    };
    flood(seed, fwd, (i) => raw[i].edges);
    flood(seed, bwd, (i) => rev[i]);
    const keep = raw.filter((n) => fwd[n.id] && bwd[n.id]);
    const remap = new Map();
    keep.forEach((n, i) => remap.set(n.id, i));
    this.nodes = keep.map((n, i) => {
      const edges = [], cost = [];
      n.edges.forEach((e, k) => { if (remap.has(e)) { edges.push(remap.get(e)); cost.push(n.cost[k]); } });
      return { x: n.x, y: n.y, z: n.z, ix: n.ix, iz: n.iz, edges, cost, id: i };
    });
    this.cols.clear();
    for (const n of this.nodes) {
      const k = this._key(n.ix, n.iz);
      if (!this.cols.has(k)) this.cols.set(k, []);
      this.cols.get(k).push(n);
    }
    this.pos = this.nodes.map((n) => new THREE.Vector3(n.x, n.y, n.z));
  }

  // Simulated walk between two points (feet positions)
  walkable(a, b) {
    const w = this.world;
    const dx = b.x - a.x, dz = b.z - a.z;
    const len = Math.hypot(dx, dz);
    const steps = Math.max(1, Math.ceil(len / 0.25));
    let y = a.y;
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const px = a.x + dx * t, pz = a.z + dz * t;
      const g = w.groundHeight(px, pz, y + 0.52, 0.05);
      if (g === -Infinity || g < y - 0.6) return false;
      y = g;
      if (w.overlaps(px - R + 0.05, y + STEP, pz - R + 0.05, px + R - 0.05, y + H, pz + R - 0.05)) return false;
    }
    return Math.abs(y - b.y) < 0.15;
  }

  nearest(p) {
    const sp = this.spacing;
    const ix = Math.round(p.x / sp), iz = Math.round(p.z / sp);
    let best = null, bd = Infinity;
    for (let r = 0; r <= 4 && !best; r++) {
      for (let x = ix - r; x <= ix + r; x++) {
        for (let z = iz - r; z <= iz + r; z++) {
          if (Math.max(Math.abs(x - ix), Math.abs(z - iz)) !== r) continue;
          const col = this.cols.get(this._key(x, z));
          if (!col) continue;
          for (const n of col) {
            if (n.y > p.y + 0.7) continue;
            const d = Math.hypot(n.x - p.x, n.z - p.z) + Math.abs(n.y - p.y) * 2;
            if (d < bd) { bd = d; best = n; }
          }
        }
      }
    }
    if (!best) {
      for (const n of this.nodes) {
        const d = Math.hypot(n.x - p.x, n.z - p.z) + Math.abs(n.y - p.y) * 2;
        if (d < bd) { bd = d; best = n; }
      }
    }
    return best;
  }

  randomNode(filter) {
    for (let i = 0; i < 40; i++) {
      const n = this.nodes[Math.floor(Math.random() * this.nodes.length)];
      if (!filter || filter(n)) return n;
    }
    return this.nodes[Math.floor(Math.random() * this.nodes.length)];
  }

  // A* with a binary heap; returns smoothed list of Vector3 waypoints
  findPath(fromPos, goal) {
    const start = this.nearest(fromPos);
    if (!start || !goal) return null;
    const N = this.nodes.length;
    if (!this._g || this._g.length !== N) {
      this._g = new Float32Array(N);
      this._came = new Int32Array(N);
      this._seen = new Uint32Array(N);
      this._closed = new Uint32Array(N);
      this._stamp = 0;
    }
    const stamp = ++this._stamp;
    const g = this._g, came = this._came, seen = this._seen, closed = this._closed;
    const heap = [];
    const hf = (n) => Math.hypot(n.x - goal.x, n.y - goal.y, n.z - goal.z);
    const push = (id, f) => {
      heap.push([f, id]);
      let i = heap.length - 1;
      while (i > 0) {
        const p = (i - 1) >> 1;
        if (heap[p][0] <= heap[i][0]) break;
        [heap[p], heap[i]] = [heap[i], heap[p]];
        i = p;
      }
    };
    const pop = () => {
      const top = heap[0];
      const last = heap.pop();
      if (heap.length) {
        heap[0] = last;
        let i = 0;
        for (;;) {
          const l = 2 * i + 1, r = l + 1;
          let m = i;
          if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
          if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
          if (m === i) break;
          [heap[m], heap[i]] = [heap[i], heap[m]];
          i = m;
        }
      }
      return top;
    };
    g[start.id] = 0;
    seen[start.id] = stamp;
    came[start.id] = -1;
    push(start.id, hf(start));
    let found = false;
    let iter = 0;
    while (heap.length && iter++ < 20000) {
      const [, id] = pop();
      if (closed[id] === stamp) continue;
      closed[id] = stamp;
      if (id === goal.id) { found = true; break; }
      const n = this.nodes[id];
      for (let k = 0; k < n.edges.length; k++) {
        const e = n.edges[k];
        if (closed[e] === stamp) continue;
        const ng = g[id] + n.cost[k];
        if (seen[e] !== stamp || ng < g[e]) {
          seen[e] = stamp;
          g[e] = ng;
          came[e] = id;
          push(e, ng + hf(this.nodes[e]));
        }
      }
    }
    if (!found) return null;
    const ids = [];
    for (let id = goal.id; id !== -1; id = came[id]) ids.push(id);
    ids.reverse();
    // String-pulling smoothing
    const pts = ids.map((i) => this.pos[i]);
    const out = [];
    let i = 0;
    const from = new THREE.Vector3().copy(fromPos);
    let cur = from;
    while (i < pts.length) {
      let j = Math.min(pts.length - 1, i + 8);
      while (j > i && !this.walkable(cur, pts[j])) j--;
      out.push(pts[j].clone());
      cur = pts[j];
      i = j + 1;
    }
    return out;
  }
}
