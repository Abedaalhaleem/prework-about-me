// NEONTOWN 2071 — a compact, point-symmetric two-house suburb map.
// Everything is built from boxes: visuals are merged per material for speed,
// and every solid also registers an AABB in the collision world.
import * as THREE from 'three';
import { mergeGeometries } from '../vendor/three/addons/utils/BufferGeometryUtils.js';
import * as TX from './textures.js';

export const BOUNDS = { x: 34, z: 31 };

function neon(r, g, b) {
  return new THREE.MeshBasicMaterial({ color: new THREE.Color(r, g, b), toneMapped: true });
}

class Builder {
  constructor(scene, world) {
    this.scene = scene;
    this.world = world;
    this.batches = new Map();
    this.mats = {};
    this.minimap = [];
  }

  defineMaterials() {
    const std = (o) => new THREE.MeshStandardMaterial(o);
    const T = {
      asphalt: TX.asphalt(), concrete: TX.concrete(), grass: TX.grass(), panels: TX.panels(),
      metal: TX.metal(), tiles: TX.darkTiles(), solar: TX.solar(), hazard: TX.hazard(),
      contBlue: TX.container('#2c5f7c'), contOrange: TX.container('#a4471d'), contGray: TX.container('#5b6068'),
      plain: TX.concrete(false),
    };
    this.tex = T;
    const m = this.mats;
    const def = (key, mat, tile = 4, opts = {}) => { m[key] = { mat, tile, cast: opts.cast !== false, receive: opts.receive !== false }; };
    def('asphalt', std({ map: T.asphalt, roughness: 0.9, metalness: 0.05 }), 8);
    def('sidewalk', std({ map: T.concrete, roughness: 0.85 }), 3);
    def('concrete', std({ map: T.plain, color: '#c9ccd1', roughness: 0.8 }), 4);
    def('grass', std({ map: T.grass, roughness: 1 }), 6);
    def('dirt', std({ map: T.plain, color: '#6b5646', roughness: 1 }), 10);
    def('panelA', std({ map: T.panels, color: '#fff1e2', roughness: 0.5, metalness: 0.05 }), 4);
    def('panelB', std({ map: T.panels, color: '#e6f3ff', roughness: 0.5, metalness: 0.05 }), 4);
    def('accentA', std({ color: '#ff6a1a', roughness: 0.35, metalness: 0.45, emissive: '#3a1000', emissiveIntensity: 0.4 }), 2);
    def('accentB', std({ color: '#10c4b6', roughness: 0.35, metalness: 0.45, emissive: '#002a26', emissiveIntensity: 0.4 }), 2);
    def('trim', std({ color: '#1a1d23', roughness: 0.35, metalness: 0.7 }), 2);
    def('metal', std({ map: T.metal, color: '#b8bcc6', roughness: 0.4, metalness: 0.85 }), 2);
    def('floor', std({ map: T.tiles, roughness: 0.3, metalness: 0.25 }), 3);
    def('interior', std({ map: T.panels, color: '#8e97a3', roughness: 0.7 }), 4);
    def('solar', std({ map: T.solar, roughness: 0.15, metalness: 0.8 }), 2);
    def('glass', std({ color: '#0b1822', roughness: 0.06, metalness: 0.95 }), 4);
    def('contBlue', std({ map: T.contBlue, roughness: 0.6, metalness: 0.5 }), 2.6);
    def('contOrange', std({ map: T.contOrange, roughness: 0.6, metalness: 0.5 }), 2.6);
    def('contGray', std({ map: T.contGray, roughness: 0.6, metalness: 0.5 }), 2.6);
    def('hazard', std({ map: T.hazard, roughness: 0.7 }), 1.6);
    def('hedge', std({ map: T.grass, color: '#5c8f55', roughness: 1 }), 1.5);
    def('wreck', std({ map: T.metal, color: '#3b3029', roughness: 0.95, metalness: 0.3 }), 1.5);
    def('rubber', std({ color: '#0d0d0f', roughness: 0.95 }), 1);
    def('bark', std({ color: '#3d2c22', roughness: 1 }), 1);
    def('leaves', std({ color: '#2f7a4a', roughness: 0.9, emissive: '#0a2a18', emissiveIntensity: 0.6 }), 2);
    def('leavesNeon', std({ color: '#6b2fa0', roughness: 0.8, emissive: '#4a1080', emissiveIntensity: 0.9 }), 2);
    def('busBody', std({ color: '#f2f4f7', roughness: 0.25, metalness: 0.6 }), 4);
    def('crate', std({ map: T.contGray, color: '#8a8f99', roughness: 0.7, metalness: 0.4 }), 1.2);
    def('neonCyan', neon(0.25, 2.2, 3.0), 1, { cast: false, receive: false });
    def('neonOrange', neon(3.4, 1.05, 0.15), 1, { cast: false, receive: false });
    def('neonMagenta', neon(2.6, 0.25, 2.2), 1, { cast: false, receive: false });
    def('neonWhite', neon(2.4, 2.4, 2.6), 1, { cast: false, receive: false });
    def('neonRed', neon(3.2, 0.12, 0.08), 1, { cast: false, receive: false });
    // dimmer variants for strips at eye level so bloom doesn't flood the screen up close
    def('neonCyanSoft', neon(0.12, 1.0, 1.4), 1, { cast: false, receive: false });
    def('neonOrangeSoft', neon(1.6, 0.5, 0.08), 1, { cast: false, receive: false });
    def('neonMagentaSoft', neon(1.2, 0.12, 1.0), 1, { cast: false, receive: false });
    def('neonYellow', neon(3.0, 2.2, 0.3), 1, { cast: false, receive: false });
    def('lineWhite', neon(0.9, 0.9, 0.95), 1, { cast: false });
  }

  // Box with world-projected UVs, batched by material.
  box(x0, y0, z0, x1, y1, z1, matKey, opts = {}) {
    const minX = Math.min(x0, x1), maxX = Math.max(x0, x1);
    const minY = Math.min(y0, y1), maxY = Math.max(y0, y1);
    const minZ = Math.min(z0, z1), maxZ = Math.max(z0, z1);
    const w = maxX - minX, h = maxY - minY, d = maxZ - minZ;
    if (w <= 0 || h <= 0 || d <= 0) return null;
    if (matKey) {
      const g = new THREE.BoxGeometry(w, h, d);
      g.translate((minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2);
      this.addGeo(g, matKey);
    }
    let col = null;
    if (opts.collide !== false) {
      col = this.world.add(minX, minY, minZ, maxX, maxY, maxZ, opts);
      if (opts.minimap !== false && maxY > 0.5) this.minimap.push({ x0: minX, z0: minZ, x1: maxX, z1: maxZ, h: maxY, floor: minY > 2.5 });
    }
    return col;
  }

  addGeo(g, matKey) {
    let arr = this.batches.get(matKey);
    if (!arr) this.batches.set(matKey, (arr = []));
    arr.push(g.index ? g.toNonIndexed() : g);
  }

  // Wall rectangle along the X axis (occupying z∈[za,zb]) with rectangular openings in x/y
  wallX(za, zb, x0, x1, y0, y1, holes, mat, opts) {
    this._wall('x', za, zb, x0, x1, y0, y1, holes, mat, opts);
  }
  wallZ(xa, xb, z0, z1, y0, y1, holes, mat, opts) {
    this._wall('z', xa, xb, z0, z1, y0, y1, holes, mat, opts);
  }
  _wall(axis, ta, tb, a0, a1, y0, y1, holes, mat, opts = {}) {
    const cuts = new Set([a0, a1]);
    for (const h of holes) { cuts.add(Math.max(a0, Math.min(a1, h.a))); cuts.add(Math.max(a0, Math.min(a1, h.b))); }
    const xs = [...cuts].sort((p, q) => p - q);
    for (let i = 0; i < xs.length - 1; i++) {
      const s0 = xs[i], s1 = xs[i + 1];
      if (s1 - s0 < 1e-4) continue;
      const mid = (s0 + s1) / 2;
      // vertical intervals left after subtracting holes that span this slice
      let spans = [[y0, y1]];
      for (const h of holes) {
        if (mid <= h.a || mid >= h.b) continue;
        const next = [];
        for (const [p, q] of spans) {
          if (h.y1 <= p || h.y0 >= q) { next.push([p, q]); continue; }
          if (h.y0 > p) next.push([p, h.y0]);
          if (h.y1 < q) next.push([h.y1, q]);
        }
        spans = next;
      }
      for (const [p, q] of spans) {
        if (axis === 'x') this.box(s0, p, ta, s1, q, tb, mat, opts);
        else this.box(ta, p, s0, tb, q, s1, mat, opts);
      }
    }
  }

  mesh(geo, matKey, x, y, z, ry = 0) {
    const g = geo.clone();
    if (ry) g.rotateY(ry);
    g.translate(x, y, z);
    this.addGeo(g, matKey);
  }

  finalize() {
    for (const [key, geos] of this.batches) {
      const info = this.mats[key];
      // World-projected ("box mapped") UVs so textures tile continuously
      for (const g of geos) {
        const pos = g.attributes.position, nor = g.attributes.normal;
        let uv = g.attributes.uv;
        if (!uv) { uv = new THREE.BufferAttribute(new Float32Array(pos.count * 2), 2); g.setAttribute('uv', uv); }
        for (let i = 0; i < pos.count; i++) {
          const nx = Math.abs(nor.getX(i)), ny = Math.abs(nor.getY(i)), nz = Math.abs(nor.getZ(i));
          const px = pos.getX(i), py = pos.getY(i), pz = pos.getZ(i);
          let u, v;
          if (ny >= nx && ny >= nz) { u = px; v = pz; }
          else if (nx >= nz) { u = pz; v = py; }
          else { u = px; v = py; }
          uv.setXY(i, u / info.tile, v / info.tile);
        }
        for (const k of Object.keys(g.attributes)) if (!['position', 'normal', 'uv'].includes(k)) g.deleteAttribute(k);
        g.clearGroups();
      }
      const merged = mergeGeometries(geos, false);
      const mesh = new THREE.Mesh(merged, info.mat);
      mesh.castShadow = info.cast;
      mesh.receiveShadow = info.receive;
      mesh.matrixAutoUpdate = false;
      this.scene.add(mesh);
    }
    this.batches.clear();
  }
}

// Mirror helper: sign=+1 builds the north side, sign=-1 the point-reflected south side.
function sided(B, s) {
  const X = (x) => x * s, Z = (z) => z * s;
  return {
    box: (x0, y0, z0, x1, y1, z1, mat, opts) => B.box(X(x0), y0, Z(z0), X(x1), y1, Z(z1), mat, opts),
    wallX: (za, zb, x0, x1, y0, y1, holes, mat, opts) =>
      B.wallX(Z(za), Z(zb), X(x0), X(x1), y0, y1, holes.map((h) => ({ a: Math.min(X(h.a), X(h.b)), b: Math.max(X(h.a), X(h.b)), y0: h.y0, y1: h.y1 })), mat, opts),
    wallZ: (xa, xb, z0, z1, y0, y1, holes, mat, opts) =>
      B.wallZ(X(xa), X(xb), Z(z0), Z(z1), y0, y1, holes.map((h) => ({ a: Math.min(Z(h.a), Z(h.b)), b: Math.max(Z(h.a), Z(h.b)), y0: h.y0, y1: h.y1 })), mat, opts),
    mesh: (geo, mat, x, y, z, ry = 0) => B.mesh(geo, mat, X(x), y, Z(z), ry + (s < 0 ? Math.PI : 0)),
    v: (x, y, z) => new THREE.Vector3(X(x), y, Z(z)),
    ry: (r) => r + (s < 0 ? Math.PI : 0),
    s,
  };
}

export function buildMap(scene, world) {
  const B = new Builder(scene, world);
  B.defineMaterials();
  const out = {
    spawns: [[], []],
    fires: [],
    explosives: [],
    holos: [],
    animated: [],
    lampPositions: [],
    minimap: B.minimap,
  };

  buildGround(B, scene);
  buildStreet(B, scene, out);
  for (const s of [1, -1]) buildSide(B, sided(B, s), scene, out);
  buildBoundary(B, scene);
  B.finalize();
  return out;
}

// ---------------------------------------------------------------------------
function buildGround(B, scene) {
  // one ground collider covering everything
  B.world.add(-80, -2, -80, 80, 0, 80, { surface: 'dirt', runnable: false });
  // lawn inside, wasteland outside
  const lawn = new THREE.Mesh(new THREE.PlaneGeometry(2 * BOUNDS.x, 2 * BOUNDS.z), B.mats.grass.mat);
  lawn.rotation.x = -Math.PI / 2;
  lawn.geometry.attributes.uv.array.forEach((v, i, a) => { a[i] = v * (i % 2 ? (2 * BOUNDS.z) / 6 : (2 * BOUNDS.x) / 6); });
  lawn.receiveShadow = true;
  scene.add(lawn);
  const dirt = new THREE.Mesh(new THREE.PlaneGeometry(900, 900), B.mats.dirt.mat);
  dirt.rotation.x = -Math.PI / 2;
  dirt.position.y = -0.03;
  dirt.geometry.attributes.uv.array.forEach((v, i, a) => { a[i] = v * 90; });
  dirt.receiveShadow = true;
  scene.add(dirt);
}

function buildStreet(B, scene, out) {
  // Asphalt road (visual slab, very thin)
  B.box(-BOUNDS.x, -0.05, -5.5, BOUNDS.x, 0.02, 5.5, 'asphalt', { collide: false });
  // Glowing lane markers
  for (let x = -32; x < 32; x += 4) B.box(x, 0.02, -0.08, x + 2, 0.035, 0.08, 'neonCyanSoft', { collide: false });
  B.box(-BOUNDS.x, 0.02, 5.05, BOUNDS.x, 0.03, 5.2, 'lineWhite', { collide: false });
  B.box(-BOUNDS.x, 0.02, -5.2, BOUNDS.x, 0.03, -5.05, 'lineWhite', { collide: false });

  // ---- The hover bus: centerpiece cover ----
  const bx0 = -5.6, bx1 = 5.6, bz0 = -1.35, bz1 = 1.65;
  B.box(bx0, 0.6, bz0, bx1, 3.3, bz1, null, { surface: 'metal' }); // collider
  B.world.add(bx0 + 0.3, 0, bz0 + 0.3, bx1 - 0.3, 0.6, bz1 - 0.3, { surface: 'metal' }); // skirt so nobody crawls under
  B.box(bx0 + 0.2, 0.6, bz0, bx1 - 0.2, 1.4, bz1, 'busBody', { collide: false });
  B.box(bx0 + 0.2, 1.4, bz0 + 0.05, bx1 - 0.2, 2.5, bz1 - 0.05, 'glass', { collide: false });
  B.box(bx0 + 0.2, 2.5, bz0, bx1 - 0.2, 3.25, bz1, 'busBody', { collide: false });
  // pillars between windows
  for (let x = bx0 + 1.2; x < bx1 - 0.6; x += 1.8) B.box(x, 1.4, bz0 - 0.02, x + 0.18, 2.5, bz1 + 0.02, 'busBody', { collide: false });
  // rounded noses
  const nose = new THREE.CylinderGeometry(1.5, 1.5, 2.65, 20, 1, false, 0, Math.PI);
  B.mesh(nose, 'busBody', bx1 - 0.2, 1.925, 0.15, 0);
  B.mesh(nose, 'busBody', bx0 + 0.2, 1.925, 0.15, Math.PI);
  const noseGlass = new THREE.CylinderGeometry(1.52, 1.52, 0.9, 20, 1, false, 0.3, Math.PI - 0.6);
  B.mesh(noseGlass, 'glass', bx1 - 0.2, 2.0, 0.15, 0);
  B.mesh(noseGlass, 'glass', bx0 + 0.2, 2.0, 0.15, Math.PI);
  // neon stripe + underglow
  B.box(bx0 + 0.2, 1.3, bz0 - 0.03, bx1 - 0.2, 1.4, bz1 + 0.03, 'neonCyan', { collide: false });
  B.box(bx0 + 0.2, 3.25, bz0 + 0.3, bx1 - 0.2, 3.3, bz1 - 0.3, 'neonMagenta', { collide: false });
  B.box(bx0 + 0.6, 0.35, bz0 + 0.25, bx0 + 1.8, 0.6, bz1 - 0.25, 'trim', { collide: false });
  B.box(bx1 - 1.8, 0.35, bz0 + 0.25, bx1 - 0.6, 0.6, bz1 - 0.25, 'trim', { collide: false });
  B.box(bx0 + 0.7, 0.3, bz0 + 0.3, bx0 + 1.7, 0.35, bz1 - 0.3, 'neonCyan', { collide: false });
  B.box(bx1 - 1.7, 0.3, bz0 + 0.3, bx1 - 0.7, 0.35, bz1 - 0.3, 'neonCyan', { collide: false });
  const glow = new THREE.Mesh(new THREE.PlaneGeometry(13, 5), new THREE.MeshBasicMaterial({
    map: TX.softDot(), color: new THREE.Color(0.03, 0.3, 0.45), transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
  }));
  glow.rotation.x = -Math.PI / 2;
  glow.position.set(0, 0.04, 0.15);
  scene.add(glow);
  // bus route hologram on the roof
  const busHolo = TX.holoSign(['ROUTE 77', 'NEONTOWN ⇄ SECTOR 9'], '#ff4fd8', 512, 128);
  for (const side of [-1, 1]) {
    const p = new THREE.Mesh(new THREE.PlaneGeometry(5, 1.25), new THREE.MeshBasicMaterial({
      map: busHolo.texture, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, color: new THREE.Color(1.6, 1.6, 1.6),
    }));
    p.position.set(0, 4.0, 0.15 + side * 0.02);
    p.rotation.y = side > 0 ? 0 : Math.PI;
    scene.add(p);
  }
  out.holos.push({ draw: busHolo.draw, rate: 0.12 });
}

// ---------------------------------------------------------------------------
function buildSide(B, S, scene, out) {
  const s = S.s;
  const north = s > 0;
  const panel = north ? 'panelA' : 'panelB';
  const accent = north ? 'accentA' : 'accentB';
  const neonA = north ? 'neonOrange' : 'neonCyan';
  const neonLow = north ? 'neonOrangeSoft' : 'neonCyanSoft';
  const team = north ? 1 : 0;

  // ---- Sidewalk + curb ----
  S.box(-BOUNDS.x, 0, 5.5, BOUNDS.x, 0.12, 7.5, 'sidewalk', { minimap: false });

  // ================= HOUSE =================
  const hx0 = -17, hx1 = -3, hz0 = 10, hz1 = 20, t = 0.3, F2 = 3.2, TOP = 6.2;
  const win1 = (a, b) => ({ a, b, y0: 1.0, y1: 2.3 });
  const win2 = (a, b) => ({ a, b, y0: 4.1, y1: 5.6 });
  const door = (a, b) => ({ a, b, y0: 0, y1: 2.5 });
  // floor
  S.box(hx0, 0, hz0, hx1, 0.06, hz1, 'floor', { minimap: false });
  // front / back / sides
  S.wallX(hz0, hz0 + t, hx0, hx1, 0, TOP, [door(-11, -9.4), win1(-15.6, -13), win1(-7, -4.6), win2(-15.6, -12.6), win2(-8.4, -4.4)], panel);
  S.wallX(hz1 - t, hz1, hx0, hx1, 0, TOP, [door(-7, -5.4), win1(-9.8, -8.2), win2(-15, -12), win2(-7, -4.5)], panel);
  S.wallZ(hx0, hx0 + t, hz0 + t, hz1 - t, 0, TOP, [win1(12, 14.5), win2(12, 15)], panel);
  S.wallZ(hx1 - t, hx1, hz0 + t, hz1 - t, 0, TOP, [door(12, 13.6), win1(15.5, 18), win2(13, 17)], panel);
  // interior walls
  S.wallZ(-10.15, -9.85, 13, 18.3, 0, 3.0, [door(15, 16.4)], 'interior');
  S.wallZ(-9.15, -8.85, hz0 + t, 14.5, F2, TOP, [], 'interior');
  // second floor slab with stairwell hole
  S.box(hx0 + t, 3.0, hz0 + t, hx1 - t, F2, 18.3, 'floor');
  S.box(-10.7, 3.0, 18.3, hx1 - t, F2, hz1 - t, 'floor');
  // stairs (solid steps)
  for (let i = 0; i < 12; i++) {
    S.box(-16.7 + i * 0.5, 0, 18.3, -16.2 + i * 0.5, ((i + 1) * F2) / 12, hz1 - t, i % 2 ? 'trim' : 'metal', { minimap: false });
  }
  // railing around the stairwell (glass + neon cap)
  S.box(-16.7, F2, 18.2, -10.9, F2 + 1.0, 18.3, 'glass', { runnable: false, minimap: false });
  S.box(-16.7, F2 + 1.0, 18.18, -10.9, F2 + 1.06, 18.32, neonLow, { collide: false });
  // furniture
  S.box(-8, 0, 16.6, -5, 1.0, 17.3, 'metal', { surface: 'metal', runnable: false });
  S.box(-8, 1.0, 16.55, -5, 1.05, 17.35, neonLow, { collide: false });
  S.box(-15.6, 0, 13.4, -13, 0.8, 14.3, 'trim', { runnable: false });
  S.box(-16.4, F2, 10.8, -14.2, F2 + 0.6, 13.2, 'trim', { runnable: false });
  S.box(-6.5, F2, 18.8, -4.0, F2 + 1.0, 19.5, 'metal', { runnable: false });
  // roof + solar panels + antenna
  S.box(hx0 - 0.6, TOP, hz0 - 0.6, hx1 + 0.6, TOP + 0.3, hz1 + 0.6, 'trim');
  S.box(-15.5, TOP + 0.3, 12, -9.5, TOP + 0.45, 18, 'solar', { collide: false });
  S.box(-8.5, TOP + 0.3, 12, -4.5, TOP + 0.45, 18, 'solar', { collide: false });
  S.box(-4.2, TOP + 0.3, 11, -4.0, TOP + 3.2, 11.2, 'metal', { collide: false });
  // exterior neon trims
  const ring = (y, h, mat) => {
    S.box(hx0 - 0.62, y, hz0 - 0.62, hx1 + 0.62, y + h, hz0 - 0.56, mat, { collide: false });
    S.box(hx0 - 0.62, y, hz1 + 0.56, hx1 + 0.62, y + h, hz1 + 0.62, mat, { collide: false });
    S.box(hx0 - 0.62, y, hz0 - 0.62, hx0 - 0.56, y + h, hz1 + 0.62, mat, { collide: false });
    S.box(hx1 + 0.56, y, hz0 - 0.62, hx1 + 0.62, y + h, hz1 + 0.62, mat, { collide: false });
  };
  ring(TOP + 0.05, 0.12, neonA);
  // accent floor band
  S.box(hx0 - 0.06, 3.0, hz0 - 0.06, hx1 + 0.06, 3.3, hz0, accent, { collide: false });
  S.box(hx0 - 0.06, 3.0, hz1, hx1 + 0.06, 3.3, hz1 + 0.06, accent, { collide: false });
  S.box(hx0 - 0.06, 3.0, hz0, hx0, 3.3, hz1, accent, { collide: false });
  S.box(hx1, 3.0, hz0, hx1 + 0.06, 3.3, hz1, accent, { collide: false });
  // accent corner fins
  S.box(hx0 - 0.25, 0, hz0 - 0.25, hx0 + 0.15, TOP + 0.3, hz0 + 0.15, accent);
  S.box(hx1 - 0.15, 0, hz0 - 0.25, hx1 + 0.25, TOP + 0.3, hz0 + 0.15, accent);
  // glowing window sills
  for (const [a, b] of [[-15.6, -13], [-7, -4.6]]) S.box(a, 0.94, hz0 - 0.08, b, 1.0, hz0, neonLow, { collide: false });
  for (const [a, b] of [[-15.6, -12.6], [-8.4, -4.4]]) S.box(a, 4.04, hz0 - 0.08, b, 4.1, hz0, neonA, { collide: false });
  S.box(-11, 2.5, hz0 - 0.08, -9.4, 2.58, hz0, 'neonWhite', { collide: false });
  // interior ceiling light strips
  S.box(-15, 2.94, 12, -11, 3.0, 12.15, 'neonWhite', { collide: false });
  S.box(-8, 2.94, 12, -4.5, 3.0, 12.15, 'neonWhite', { collide: false });
  S.box(-15, 6.14, 14.5, -5, 6.2, 14.65, 'neonWhite', { collide: false });

  // porch
  S.box(-12, 0, 8.4, -8.4, 0.2, hz0, 'concrete', { minimap: false });
  S.box(-12.2, 2.8, 8.0, -8.2, 3.0, hz0, 'trim');
  S.box(-12, 2.72, 8.0, -8.4, 2.8, 8.1, neonLow, { collide: false });
  S.box(-12, 0, 8.1, -11.75, 2.8, 8.35, 'metal');
  S.box(-8.65, 0, 8.1, -8.4, 2.8, 8.35, 'metal');
  // front-yard planters (low cover)
  S.box(-16.4, 0, 8.2, -13.2, 0.55, 8.9, 'concrete');
  S.box(-16.3, 0.55, 8.25, -13.3, 1.05, 8.85, 'hedge', { collide: false });
  B.world.add(...orderedBox(S, -16.4, 0.55, 8.2, -13.2, 1.05, 8.9), { surface: 'grass', runnable: false, bulletproof: false });
  S.box(-7.6, 0, 8.2, -3.6, 0.55, 8.9, 'concrete');
  S.box(-7.5, 0.55, 8.25, -3.7, 1.05, 8.85, 'hedge', { collide: false });
  B.world.add(...orderedBox(S, -7.6, 0.55, 8.2, -3.6, 1.05, 8.9), { surface: 'grass', runnable: false, bulletproof: false });

  // ================= GARAGE / WORKSHOP =================
  const gx0 = 6, gx1 = 15, gz0 = 12, gz1 = 19, GH = 3.4;
  S.box(gx0, 0, gz0, gx1, 0.06, gz1, 'floor', { minimap: false });
  S.wallX(gz0, gz0 + t, gx0, gx1, 0, GH, [{ a: 7.2, b: 13.8, y0: 0, y1: 2.8 }], 'metal', { surface: 'metal' });
  S.wallX(gz1 - t, gz1, gx0, gx1, 0, GH, [{ a: 9, b: 12, y0: 1.2, y1: 2.2 }], 'metal', { surface: 'metal' });
  S.wallZ(gx0, gx0 + t, gz0 + t, gz1 - t, 0, GH, [door(15, 16.4)], 'metal', { surface: 'metal' });
  S.wallZ(gx1 - t, gx1, gz0 + t, gz1 - t, 0, GH, [{ a: 14, b: 17, y0: 1.2, y1: 2.2 }], 'metal', { surface: 'metal' });
  S.box(gx0 - 0.3, GH, gz0 - 0.4, gx1 + 0.3, GH + 0.2, gz1 + 0.3, accent);
  S.box(7.2, 2.8, gz0 - 0.08, 13.8, 2.9, gz0, 'neonYellow', { collide: false });
  S.box(7.0, 0, 17.6, 10, 1.0, 18.6, 'metal', { surface: 'metal', runnable: false });
  S.box(7.0, 1.0, 17.55, 10, 1.05, 18.65, 'neonWhite', { collide: false });
  S.box(gx0 + 0.5, GH - 0.1, 15, gx1 - 0.5, GH - 0.05, 15.12, 'neonWhite', { collide: false });
  // garage apron
  S.box(7.2, 0, 7.5, 13.8, 0.04, gz0, 'concrete', { collide: false });
  // fusion core inside (big explosive)
  addExplosive(B, S, scene, out, 12.6, 16.6, { big: true });

  // ================= BURNING WRECK =================
  S.box(-2.6, 0, 7.5, 3.4, 0.04, 18, 'concrete', { collide: false });
  S.box(-0.9, 0.25, 10.8, 1.3, 1.1, 15.2, 'wreck', { surface: 'metal' });
  B.world.add(...orderedBox(S, -0.9, 0, 10.8, 1.3, 0.25, 15.2), { surface: 'metal' });
  S.box(-0.7, 1.1, 11.9, 1.1, 1.6, 14.1, 'wreck', { surface: 'metal' });
  S.box(-0.65, 1.12, 11.8, 1.05, 1.5, 11.95, 'glass', { collide: false });
  const wheel = new THREE.CylinderGeometry(0.36, 0.36, 0.28, 14);
  wheel.rotateZ(Math.PI / 2);
  for (const [wx, wz] of [[-0.95, 11.6], [1.35, 11.6], [-0.95, 14.4], [1.35, 14.4]]) S.mesh(wheel, 'rubber', wx, 0.3, wz);
  out.fires.push({ pos: S.v(0.2, 1.1, 14.4), radius: 0.9, rate: 70, scale: 1.25, light: true, damageRadius: 1.2 });
  out.fires.push({ pos: S.v(0.2, 1.6, 13.0), radius: 0.7, rate: 55, scale: 1.0, light: false, damageRadius: 1.2 });

  // ================= STREET PROPS =================
  // hover truck
  S.box(-23, 0.45, 2.9, -19.8, 1.4, 5.1, 'metal', { surface: 'metal' });
  B.world.add(...orderedBox(S, -22.8, 0, 3.1, -18, 0.45, 4.9), { surface: 'metal' });
  S.box(-19.8, 0.45, 2.9, -17.8, 2.25, 5.1, north ? 'accentA' : 'accentB', { surface: 'metal' });
  S.box(-19.7, 1.4, 2.88, -17.9, 2.1, 5.12, 'glass', { collide: false });
  S.box(-23, 1.4, 2.9, -22.8, 2.0, 5.1, 'metal', { surface: 'metal' });
  S.box(-22.6, 0.25, 3.2, -18.2, 0.45, 4.8, 'trim', { collide: false });
  S.box(-22.4, 0.2, 3.4, -18.4, 0.25, 4.6, neonLow, { collide: false });
  S.box(-17.84, 0.8, 3.1, -17.78, 0.95, 3.7, 'neonWhite', { collide: false });
  S.box(-17.84, 0.8, 4.3, -17.78, 0.95, 4.9, 'neonWhite', { collide: false });
  S.box(-22.8, 1.4, 3.2, -21.4, 2.1, 4.1, 'crate', { surface: 'metal', runnable: false });

  // street lamps
  for (const lx of [-25, -2, 18]) {
    const p = S.v(lx, 0, 6.9);
    B.box(p.x - 0.12, 0.12, p.z - 0.12, p.x + 0.12, 5.6, p.z + 0.12, 'trim', { runnable: false, minimap: false });
    B.box(p.x - 0.1, 5.4, p.z - 1.6 * s, p.x + 0.1, 5.55, p.z, 'trim', { collide: false });
    B.box(p.x - 0.08, 5.3, p.z - 1.55 * s, p.x + 0.08, 5.4, p.z - 0.15 * s, 'neonWhite', { collide: false });
    B.box(p.x - 0.13, 1.0, p.z - 0.13, p.x + 0.13, 1.25, p.z + 0.13, neonLow, { collide: false });
    out.lampPositions.push(new THREE.Vector3(p.x, 5.3, p.z - 0.8 * s));
  }

  // west end: barriers + gate
  S.box(-31.5, 0, -4.8, -30.5, 1.1, -2.2, 'hazard', { surface: 'concrete' });
  S.box(-31.5, 0, 1.8, -30.5, 1.1, 4.4, 'hazard', { surface: 'concrete' });
  S.box(-32, 0, -7.6, -31, 7.2, -6.6, 'trim');
  S.box(-32, 0, 6.6, -31, 7.2, 7.6, 'trim');
  S.box(-31.95, 0.5, -7.65, -31.05, 6.8, -7.55, neonA, { collide: false });
  S.box(-31.95, 0.5, 7.55, -31.05, 6.8, 7.65, neonA, { collide: false });
  S.box(-32, 6.8, -7.6, -31, 7.2, 7.6, 'trim', { collide: false });
  const gateText = north
    ? ['NEONTOWN', (t) => `EST. 2071  ·  POP. ${String(out.population ?? 0).padStart(2, '0')}`]
    : ['SECTOR 7', 'AUTHORIZED PERSONNEL ONLY'];
  const holo = TX.holoSign(gateText, north ? '#ffb347' : '#35f0ff', 1024, 256);
  const hp = new THREE.Mesh(new THREE.PlaneGeometry(13, 3.2), new THREE.MeshBasicMaterial({
    map: holo.texture, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
    color: new THREE.Color(1.8, 1.8, 1.8),
  }));
  const hpPos = S.v(-31.5, 5.1, 0);
  hp.position.copy(hpPos);
  hp.rotation.y = S.ry(Math.PI / 2);
  scene.add(hp);
  out.holos.push({ draw: holo.draw, rate: 0.1, mesh: hp });

  // ================= SIDE YARD (west) =================
  S.box(-33, 0, 12, -27, 2.6, 14.5, 'contBlue', { surface: 'metal' });
  S.box(-32.4, 2.6, 12, -28, 5.2, 14.5, 'contOrange', { surface: 'metal' });
  S.box(-33, 0, 17, -30.5, 2.6, 23, 'contGray', { surface: 'metal' });
  S.box(-26, 0, 15, -22, 1.1, 15.5, 'concrete');
  S.box(-26, 1.1, 15.05, -22, 1.16, 15.45, neonLow, { collide: false });
  addBurningBarrel(B, S, out, -22, 10.8);
  addExplosive(B, S, scene, out, -20.2, 14.2);
  addExplosive(B, S, scene, out, -24.6, 19.2);
  addTree(B, S, -25.5, 10.5, north);
  addTree(B, S, -30.5, 27.5, !north);

  // ================= BACKYARD =================
  S.box(-27, 0, 23, -22, 2.8, 28, panel);
  S.box(-27.3, 2.8, 22.7, -21.7, 3.0, 28.3, accent);
  S.box(-27.05, 2.4, 22.95, -21.95, 2.5, 23, neonLow, { collide: false });
  S.box(-13, 0, 24, -7, 1.1, 24.5, 'concrete');
  S.box(-13, 1.1, 24.05, -7, 1.16, 24.45, neonLow, { collide: false });
  S.box(2, 0, 24.2, 3.2, 2.4, 25.4, 'crate', { surface: 'metal' });
  S.box(3.3, 0, 24.2, 4.5, 1.2, 25.4, 'crate', { surface: 'metal' });
  S.box(-1, 0, 27, 0.2, 1.2, 28.2, 'crate', { surface: 'metal' });
  // fence between house back and garage
  S.box(-3, 0, 21.5, 1, 1.3, 21.7, 'metal', { surface: 'metal', runnable: false });
  S.box(3, 0, 21.5, 6, 1.3, 21.7, 'metal', { surface: 'metal', runnable: false });
  S.box(-3, 1.3, 21.48, 1, 1.36, 21.72, neonLow, { collide: false });
  S.box(3, 1.3, 21.48, 6, 1.36, 21.72, neonLow, { collide: false });
  // fire pit
  S.box(-4.9, 0, 25.6, -3.1, 0.35, 25.9, 'concrete', { minimap: false });
  S.box(-4.9, 0, 27.1, -3.1, 0.35, 27.4, 'concrete', { minimap: false });
  S.box(-4.9, 0, 25.9, -4.6, 0.35, 27.1, 'concrete', { minimap: false });
  S.box(-3.4, 0, 25.9, -3.1, 0.35, 27.1, 'concrete', { minimap: false });
  out.fires.push({ pos: S.v(-4.0, 0.25, 26.5), radius: 0.55, rate: 40, scale: 0.8, light: false, damageRadius: 0.9 });
  // antenna mast
  S.box(9.8, 0, 26.8, 10.2, 7, 27.2, 'metal', { runnable: false, surface: 'metal' });
  S.box(9.3, 6.2, 26.95, 10.7, 6.3, 27.05, 'neonRed', { collide: false });
  addExplosive(B, S, scene, out, 12.5, 23.5);
  addTree(B, S, -17, 27.5, north);
  addTree(B, S, 16.5, 25.5, !north);
  addTree(B, S, 24, 21.5, north);

  // ================= EAST SIDE YARD =================
  S.box(19, 0.3, 10, 23, 1.3, 12.2, 'wreck', { surface: 'metal' });
  B.world.add(...orderedBox(S, 19.2, 0, 10.2, 22.8, 0.3, 12), { surface: 'metal' });
  S.box(19.8, 1.3, 10.3, 21.9, 1.8, 11.9, 'glass', { surface: 'metal' });
  addTree(B, S, 26.5, 14, !north);
  S.box(28, 0, 16, 33, 2.6, 18.5, 'contOrange', { surface: 'metal' });
  S.box(20, 0, 19, 24, 1.1, 19.5, 'concrete');
  S.box(20, 1.1, 19.05, 24, 1.16, 19.45, neonLow, { collide: false });
  addExplosive(B, S, scene, out, 17.6, 21.2);

  // ================= SPAWNS =================
  for (const [x, z] of [[-18, 26], [-9, 28.5], [0, 29], [8, 29.5], [18, 28.5], [-29, 25], [27, 24], [-5, 22.5]]) {
    out.spawns[team].push(S.v(x, 0.1, z));
  }
}

// Returns an axis-ordered box array after side transform
function orderedBox(S, x0, y0, z0, x1, y1, z1) {
  const a = S.v(x0, y0, z0), b = S.v(x1, y1, z1);
  return [Math.min(a.x, b.x), y0, Math.min(a.z, b.z), Math.max(a.x, b.x), y1, Math.max(a.z, b.z)];
}

function addTree(B, S, x, z, neonLeaves) {
  const trunk = new THREE.CylinderGeometry(0.16, 0.26, 3.2, 8);
  S.mesh(trunk, 'bark', x, 1.6, z);
  const p = S.v(x, 0, z);
  B.world.add(p.x - 0.25, 0, p.z - 0.25, p.x + 0.25, 3.2, p.z + 0.25, { surface: 'wood', runnable: false });
  const crown = new THREE.IcosahedronGeometry(1.7, 0);
  S.mesh(crown, neonLeaves ? 'leavesNeon' : 'leaves', x, 3.9, z);
  const crown2 = new THREE.IcosahedronGeometry(1.1, 0);
  S.mesh(crown2, neonLeaves ? 'leavesNeon' : 'leaves', x + 0.7, 4.8, z - 0.4);
}

function addBurningBarrel(B, S, out, x, z) {
  const g = new THREE.CylinderGeometry(0.34, 0.3, 1.0, 12, 1, true);
  S.mesh(g, 'wreck', x, 0.5, z);
  const p = S.v(x, 0, z);
  B.world.add(p.x - 0.33, 0, p.z - 0.33, p.x + 0.33, 1.0, p.z + 0.33, { surface: 'metal', runnable: false });
  out.fires.push({ pos: S.v(x, 0.95, z), radius: 0.28, rate: 45, scale: 0.75, light: true, damageRadius: 0.6 });
}

const explosiveGeo = new THREE.CylinderGeometry(0.34, 0.34, 1.1, 14);
const explosiveBandGeo = new THREE.CylinderGeometry(0.35, 0.35, 0.12, 14);
const coreGeo = new THREE.CylinderGeometry(0.55, 0.55, 2.2, 18);
const coreRingGeo = new THREE.TorusGeometry(0.62, 0.06, 8, 24);

function addExplosive(B, S, scene, out, x, z, { big = false } = {}) {
  const p = S.v(x, 0, z);
  const group = new THREE.Group();
  group.position.copy(p);
  const bodyMat = new THREE.MeshStandardMaterial({ color: big ? '#2a2f38' : '#b3261e', roughness: 0.4, metalness: 0.6 });
  const glowMat = new THREE.MeshBasicMaterial({ color: big ? new THREE.Color(0.4, 2.2, 3.2) : new THREE.Color(3.2, 0.9, 0.1) });
  if (big) {
    const body = new THREE.Mesh(coreGeo, bodyMat);
    body.position.y = 1.1;
    body.castShadow = true;
    group.add(body);
    for (const y of [0.4, 1.1, 1.8]) {
      const r = new THREE.Mesh(coreRingGeo, glowMat);
      r.rotation.x = Math.PI / 2;
      r.position.y = y;
      group.add(r);
    }
  } else {
    const body = new THREE.Mesh(explosiveGeo, bodyMat);
    body.position.y = 0.55;
    body.castShadow = true;
    group.add(body);
    for (const y of [0.3, 0.8]) {
      const r = new THREE.Mesh(explosiveBandGeo, glowMat);
      r.position.y = y;
      group.add(r);
    }
  }
  scene.add(group);
  const half = big ? 0.6 : 0.35;
  const hgt = big ? 2.2 : 1.1;
  const ex = {
    pos: p.clone().setY(hgt * 0.5),
    group,
    glowMat,
    health: big ? 90 : 45,
    radius: big ? 9 : 6,
    damage: big ? 220 : 160,
    big,
    exploded: false,
    fuse: -1,
  };
  ex.box = B.world.add(p.x - half, 0, p.z - half, p.x + half, hgt, p.z + half, { surface: 'metal', ref: ex, runnable: false });
  out.explosives.push(ex);
}

function buildBoundary(B, scene) {
  const X = BOUNDS.x, Zb = BOUNDS.z;
  // invisible tall walls
  B.world.add(-X - 1, 0, -Zb - 1, -X, 40, Zb + 1, { runnable: false });
  B.world.add(X, 0, -Zb - 1, X + 1, 40, Zb + 1, { runnable: false });
  B.world.add(-X - 1, 0, -Zb - 1, X + 1, 40, -Zb, { runnable: false });
  B.world.add(-X - 1, 0, Zb, X + 1, 40, Zb + 1, { runnable: false });
  // visible low perimeter barrier with neon cap
  const segs = [
    [-X - 0.6, -Zb - 0.6, -X, Zb + 0.6], [X, -Zb - 0.6, X + 0.6, Zb + 0.6],
    [-X, -Zb - 0.6, X, -Zb], [-X, Zb, X, Zb + 0.6],
  ];
  for (const [x0, z0, x1, z1] of segs) {
    B.box(x0, 0, z0, x1, 1.0, z1, 'concrete', { collide: false });
    B.box(x0, 1.0, z0, x1, 1.06, z1, 'neonMagentaSoft', { collide: false });
  }
  // warning pylons outside
  for (let i = 0; i < 16; i++) {
    const a = (i / 16) * Math.PI * 2;
    const r = 48 + (i % 3) * 6;
    const x = Math.cos(a) * r, z = Math.sin(a) * r;
    B.box(x - 0.3, 0, z - 0.3, x + 0.3, 9, z + 0.3, 'trim', { collide: false });
    B.box(x - 0.35, 8.6, z - 0.35, x + 0.35, 9.0, z + 0.35, 'neonRed', { collide: false });
  }
}
