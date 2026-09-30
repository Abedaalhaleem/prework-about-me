// Axis-aligned box collision world with a uniform-grid broadphase,
// plus the shared character controller used by the player and the bots.
import * as THREE from 'three';

const CELL = 4;

export class World {
  constructor() {
    this.boxes = [];
    this.grid = new Map();
    this.stamp = 0;
  }

  // Add an axis-aligned solid. `ref` lets bullets identify what they hit
  // (explosive barrels etc). `surface` picks impact effects.
  add(minX, minY, minZ, maxX, maxY, maxZ, opts = {}) {
    const b = {
      min: new THREE.Vector3(Math.min(minX, maxX), Math.min(minY, maxY), Math.min(minZ, maxZ)),
      max: new THREE.Vector3(Math.max(minX, maxX), Math.max(minY, maxY), Math.max(minZ, maxZ)),
      surface: opts.surface || 'concrete',
      ref: opts.ref || null,
      runnable: opts.runnable !== false,
      bulletproof: opts.bulletproof !== false,
      id: this.boxes.length,
      alive: true,
      _stamp: 0,
    };
    this.boxes.push(b);
    this._forCells(b.min, b.max, (key) => {
      let arr = this.grid.get(key);
      if (!arr) this.grid.set(key, (arr = []));
      arr.push(b);
    });
    return b;
  }

  remove(b) {
    b.alive = false;
  }

  _forCells(min, max, fn) {
    const x0 = Math.floor(min.x / CELL), x1 = Math.floor(max.x / CELL);
    const z0 = Math.floor(min.z / CELL), z1 = Math.floor(max.z / CELL);
    for (let x = x0; x <= x1; x++) for (let z = z0; z <= z1; z++) fn(x * 73856093 ^ z * 19349663);
  }

  // Boxes whose cells touch the given AABB
  query(minX, minY, minZ, maxX, maxY, maxZ, out = []) {
    out.length = 0;
    const s = ++this.stamp;
    const x0 = Math.floor(minX / CELL), x1 = Math.floor(maxX / CELL);
    const z0 = Math.floor(minZ / CELL), z1 = Math.floor(maxZ / CELL);
    for (let x = x0; x <= x1; x++) {
      for (let z = z0; z <= z1; z++) {
        const arr = this.grid.get(x * 73856093 ^ z * 19349663);
        if (!arr) continue;
        for (let i = 0; i < arr.length; i++) {
          const b = arr[i];
          if (b._stamp === s || !b.alive) continue;
          b._stamp = s;
          if (b.max.x <= minX || b.min.x >= maxX || b.max.y <= minY || b.min.y >= maxY || b.max.z <= minZ || b.min.z >= maxZ) continue;
          out.push(b);
        }
      }
    }
    return out;
  }

  overlaps(minX, minY, minZ, maxX, maxY, maxZ) {
    return this.query(minX, minY, minZ, maxX, maxY, maxZ, _tmpList).length > 0;
  }

  // Slab-test raycast. dir must be normalized. Returns nearest hit or null.
  raycast(origin, dir, maxDist, ignoreNonBulletproof = false) {
    let best = null;
    let bestT = maxDist;
    const ox = origin.x, oy = origin.y, oz = origin.z;
    const ix = 1 / dir.x, iy = 1 / dir.y, iz = 1 / dir.z;
    // Broadphase: only the cells along the ray's AABB
    const ex = ox + dir.x * maxDist, ey = oy + dir.y * maxDist, ez = oz + dir.z * maxDist;
    const cands = maxDist < 40
      ? this.query(Math.min(ox, ex) - 0.01, Math.min(oy, ey) - 0.01, Math.min(oz, ez) - 0.01, Math.max(ox, ex) + 0.01, Math.max(oy, ey) + 0.01, Math.max(oz, ez) + 0.01, _rayList)
      : this.boxes;
    for (let i = 0; i < cands.length; i++) {
      const b = cands[i];
      if (!b.alive) continue;
      if (ignoreNonBulletproof && !b.bulletproof) continue;
      let t1 = (b.min.x - ox) * ix, t2 = (b.max.x - ox) * ix;
      let tmin = Math.min(t1, t2), tmax = Math.max(t1, t2);
      t1 = (b.min.y - oy) * iy; t2 = (b.max.y - oy) * iy;
      tmin = Math.max(tmin, Math.min(t1, t2)); tmax = Math.min(tmax, Math.max(t1, t2));
      t1 = (b.min.z - oz) * iz; t2 = (b.max.z - oz) * iz;
      tmin = Math.max(tmin, Math.min(t1, t2)); tmax = Math.min(tmax, Math.max(t1, t2));
      if (tmax >= Math.max(tmin, 0) && tmin < bestT && tmin >= 0) {
        bestT = tmin;
        best = b;
      }
    }
    if (!best) return null;
    const point = new THREE.Vector3(ox + dir.x * bestT, oy + dir.y * bestT, oz + dir.z * bestT);
    // Normal = face closest to the hit point
    const n = new THREE.Vector3();
    const e = 1e-3;
    if (Math.abs(point.x - best.min.x) < e) n.set(-1, 0, 0);
    else if (Math.abs(point.x - best.max.x) < e) n.set(1, 0, 0);
    else if (Math.abs(point.y - best.min.y) < e) n.set(0, -1, 0);
    else if (Math.abs(point.y - best.max.y) < e) n.set(0, 1, 0);
    else if (Math.abs(point.z - best.min.z) < e) n.set(0, 0, -1);
    else n.set(0, 0, 1);
    return { dist: bestT, point, normal: n, box: best };
  }

  // Line of sight between two points (true if clear)
  los(a, b) {
    _d.subVectors(b, a);
    const len = _d.length();
    if (len < 1e-4) return true;
    _d.multiplyScalar(1 / len);
    return !this.raycast(a, _d, len - 0.05);
  }

  // Highest walkable surface top under (x,z) that is <= fromY
  groundHeight(x, z, fromY, radius = 0.05) {
    const list = this.query(x - radius, -5, z - radius, x + radius, fromY + 0.001, z + radius, _gList);
    let best = -Infinity;
    for (const b of list) {
      if (b.max.y <= fromY + 1e-3 && b.max.y > best) best = b.max.y;
    }
    return best;
  }
}

const _tmpList = [];
const _rayList = [];
const _gList = [];
const _d = new THREE.Vector3();
const _q = [];

// ---------------------------------------------------------------------------
// Character controller: axis-separated AABB sweep with step-up and ground snap.
// body: { pos (feet), vel, radius, height, onGround, wallNormal }
export function moveCharacter(world, body, dt, opts = {}) {
  const r = body.radius;
  const stepH = opts.stepHeight ?? 0.5;
  const wasGrounded = body.onGround;
  body.onGround = false;
  body.hitWall = false;
  body.ceiling = false;
  const p = body.pos;

  // --- Y ---
  p.y += body.vel.y * dt;
  let list = world.query(p.x - r, p.y, p.z - r, p.x + r, p.y + body.height, p.z + r, _q);
  for (const b of list) {
    if (body.vel.y <= 0 && b.max.y - p.y < 0.6 + Math.abs(body.vel.y * dt)) {
      p.y = b.max.y;
      body.vel.y = 0;
      body.onGround = true;
    } else if (body.vel.y > 0) {
      p.y = b.min.y - body.height - 1e-3;
      body.vel.y = 0;
      body.ceiling = true;
    }
    // Otherwise we are embedded sideways (should not happen); let XZ resolve it.
  }

  // --- X then Z ---
  for (let axis = 0; axis < 2; axis++) {
    const key = axis === 0 ? 'x' : 'z';
    const dv = body.vel[key] * dt;
    if (dv === 0) continue;
    const prev = p[key];
    p[key] += dv;
    list = world.query(p.x - r, p.y + 0.01, p.z - r, p.x + r, p.y + body.height, p.z + r, _q);
    for (const b of list) {
      // Already overlapping on this axis before moving: never shove through to the far side
      if (prev + r > b.min[key] + 1e-4 && prev - r < b.max[key] - 1e-4) continue;
      const rise = b.max.y - p.y;
      if ((wasGrounded || body.onGround) && rise > 0 && rise <= stepH &&
          !world.overlaps(p.x - r, b.max.y + 0.01, p.z - r, p.x + r, b.max.y + body.height, p.z + r)) {
        p.y = b.max.y;
        body.onGround = true;
        continue;
      }
      // Push out along this axis
      if (dv > 0) p[key] = b.min[key] - r - 1e-3;
      else p[key] = b.max[key] + r + 1e-3;
      body.vel[key] = 0;
      body.hitWall = true;
      body.wallBox = b;
      body.wallAxis = key;
      body.wallSign = dv > 0 ? -1 : 1;
    }
  }

  // --- snap down stairs / curbs ---
  if (wasGrounded && !body.onGround && body.vel.y <= 0) {
    const gy = world.groundHeight(p.x, p.z, p.y + 0.01, r * 0.8);
    if (gy > p.y - 0.55) {
      p.y = gy;
      body.onGround = true;
      body.vel.y = 0;
    }
  }
  return body;
}

// Probe for a wall to the side of the body (used for wall-running)
export function probeWall(world, body, sideDir, dist = 0.3) {
  const r = body.radius;
  const cx = body.pos.x + sideDir.x * dist;
  const cz = body.pos.z + sideDir.z * dist;
  const list = world.query(cx - r, body.pos.y + 0.4, cz - r, cx + r, body.pos.y + body.height - 0.2, cz + r, _q);
  for (const b of list) {
    if (!b.runnable) continue;
    if (b.max.y - b.min.y < 2.0) continue;
    // Determine which face we are touching
    const n = new THREE.Vector3();
    const dx1 = Math.abs(body.pos.x - b.min.x), dx2 = Math.abs(body.pos.x - b.max.x);
    const dz1 = Math.abs(body.pos.z - b.min.z), dz2 = Math.abs(body.pos.z - b.max.z);
    const m = Math.min(dx1, dx2, dz1, dz2);
    if (m === dx1) n.set(-1, 0, 0);
    else if (m === dx2) n.set(1, 0, 0);
    else if (m === dz1) n.set(0, 0, -1);
    else n.set(0, 0, 1);
    return { box: b, normal: n };
  }
  return null;
}
