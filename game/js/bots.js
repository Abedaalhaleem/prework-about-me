// Combat-robot soldiers: procedural model, walk/aim/death animation and AI
// (perception, A* navigation, strafing firefights, bursts, grenades).
import * as THREE from 'three';
import { mergeGeometries } from '../vendor/three/addons/utils/BufferGeometryUtils.js';
import { moveCharacter } from './collision.js';
import { raySphere, rayBox } from './player.js';
import * as TX from './textures.js';

export const BOT_WEAPON = {
  name: 'M-27 PULSE', dmg: [21, 14], range: [15, 40], head: 1.4, tracer: [3, 1.6, 0.6], sound: 'bot',
};
const BOT_WEAPON_ALLY = { ...BOT_WEAPON, tracer: [0.8, 2.4, 3.6] };

const DIFF = {
  recruit: { reaction: [0.7, 1.2], err: 0.9, turn: 4.5, burst: [2, 4], pause: [0.6, 1.1], dmg: 0.7, nade: 0.1 },
  regular: { reaction: [0.45, 0.85], err: 0.62, turn: 6.5, burst: [3, 6], pause: [0.35, 0.8], dmg: 1.0, nade: 0.25 },
  veteran: { reaction: [0.25, 0.5], err: 0.4, turn: 9, burst: [4, 8], pause: [0.2, 0.5], dmg: 1.15, nade: 0.4 },
};

const rnd = (a, b) => a + Math.random() * (b - a);

function limb(a, b, r, mat) {
  const len = a.distanceTo(b);
  const g = new THREE.CapsuleGeometry(r, Math.max(0.01, len - r * 2), 3, 8);
  const m = new THREE.Mesh(g, mat);
  m.position.copy(a).add(b).multiplyScalar(0.5);
  m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), b.clone().sub(a).normalize());
  m.castShadow = true;
  return m;
}
function box(w, h, d, x, y, z, mat, parent) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
  m.position.set(x, y, z);
  m.castShadow = true;
  parent.add(m);
  return m;
}

function buildModel(team) {
  const accent = team === 0 ? new THREE.Color(0.25, 2.2, 3.2) : new THREE.Color(3.4, 0.9, 0.12);
  const M = {
    armor: new THREE.MeshStandardMaterial({ color: team === 0 ? '#3d4a58' : '#4a3f3a', roughness: 0.38, metalness: 0.75 }),
    dark: new THREE.MeshStandardMaterial({ color: '#16181d', roughness: 0.6, metalness: 0.5 }),
    joint: new THREE.MeshStandardMaterial({ color: '#2a2e36', roughness: 0.45, metalness: 0.85 }),
    paint: new THREE.MeshStandardMaterial({ color: team === 0 ? '#1f8fa8' : '#b8561c', roughness: 0.4, metalness: 0.5 }),
    glow: new THREE.MeshBasicMaterial({ color: accent }),
  };
  const root = new THREE.Group();
  root.rotation.order = 'YXZ';
  const hips = new THREE.Group();
  hips.position.y = 0.95;
  root.add(hips);
  box(0.36, 0.16, 0.22, 0, 0, 0, M.dark, hips);

  const torso = new THREE.Group();
  hips.add(torso);
  box(0.3, 0.2, 0.2, 0, 0.14, 0, M.joint, torso);
  box(0.48, 0.44, 0.3, 0, 0.42, 0, M.armor, torso);
  box(0.4, 0.3, 0.06, 0, 0.44, -0.16, M.paint, torso);
  box(0.3, 0.03, 0.02, 0, 0.5, -0.195, M.glow, torso);
  box(0.36, 0.4, 0.16, 0, 0.42, 0.22, M.dark, torso); // thruster pack
  for (const sx of [-0.1, 0.1]) {
    box(0.08, 0.12, 0.08, sx, 0.2, 0.28, M.joint, torso);
    box(0.06, 0.03, 0.06, sx, 0.13, 0.28, M.glow, torso);
  }
  // shoulders
  box(0.16, 0.12, 0.2, 0.3, 0.6, 0, M.paint, torso);
  box(0.16, 0.12, 0.2, -0.3, 0.6, 0, M.armor, torso);

  // head
  const head = new THREE.Group();
  head.position.set(0, 0.74, 0);
  torso.add(head);
  const helm = new THREE.Mesh(new THREE.SphereGeometry(0.15, 14, 10), M.armor);
  helm.scale.set(1, 1.08, 1.12);
  helm.castShadow = true;
  head.add(helm);
  box(0.24, 0.06, 0.08, 0, 0.0, -0.13, M.glow, head);
  box(0.26, 0.05, 0.2, 0, 0.1, -0.02, M.dark, head);
  box(0.02, 0.18, 0.02, 0.12, 0.16, 0.05, M.joint, head);

  // arms in an aiming pose, holding the rifle
  const sR = new THREE.Vector3(0.3, 0.56, 0), sL = new THREE.Vector3(-0.3, 0.56, 0);
  const eR = new THREE.Vector3(0.3, 0.3, -0.16), eL = new THREE.Vector3(-0.22, 0.32, -0.3);
  const hR = new THREE.Vector3(0.1, 0.36, -0.3), hL = new THREE.Vector3(0.06, 0.4, -0.58);
  torso.add(limb(sR, eR, 0.065, M.joint), limb(eR, hR, 0.06, M.armor), limb(sL, eL, 0.065, M.joint), limb(eL, hL, 0.06, M.armor));
  // rifle
  const gun = new THREE.Group();
  gun.position.set(0.08, 0.42, -0.46);
  torso.add(gun);
  box(0.07, 0.1, 0.62, 0, 0, 0, M.dark, gun);
  box(0.075, 0.02, 0.4, 0, 0.02, -0.05, M.glow, gun);
  box(0.03, 0.03, 0.25, 0, 0.01, -0.42, M.joint, gun);
  box(0.05, 0.16, 0.07, 0, -0.1, 0.06, M.joint, gun);
  const muzzle = new THREE.Object3D();
  muzzle.position.set(0, 0.01, -0.56);
  gun.add(muzzle);

  // legs
  const legs = [];
  for (const sx of [-0.13, 0.13]) {
    const thigh = new THREE.Group();
    thigh.position.set(sx, -0.02, 0);
    hips.add(thigh);
    thigh.add(limb(new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, -0.44, 0), 0.08, M.armor));
    const knee = new THREE.Group();
    knee.position.y = -0.46;
    thigh.add(knee);
    knee.add(limb(new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, -0.42, 0), 0.07, M.joint));
    box(0.12, 0.12, 0.1, 0, -0.08, -0.07, M.paint, knee);
    box(0.13, 0.07, 0.26, 0, -0.45, -0.04, M.dark, knee);
    legs.push({ thigh, knee });
  }
  // Merge each rigid part's meshes per material: ~45 draw calls per bot down to ~15
  for (const g of [hips, torso, head, gun, ...legs.flatMap((L) => [L.thigh, L.knee])]) mergeGroup(g);
  root.traverse((o) => { if (o.isMesh) o.castShadow = true; });
  return { root, hips, torso, head, legs, muzzle, gun };
}

function mergeGroup(group) {
  const byMat = new Map();
  for (const c of [...group.children]) {
    if (!c.isMesh) continue;
    c.updateMatrix();
    let g = c.geometry.clone().applyMatrix4(c.matrix);
    if (g.index) g = g.toNonIndexed();
    for (const k of Object.keys(g.attributes)) if (!['position', 'normal', 'uv'].includes(k)) g.deleteAttribute(k);
    g.clearGroups();
    if (!byMat.has(c.material)) byMat.set(c.material, []);
    byMat.get(c.material).push(g);
    group.remove(c);
  }
  for (const [mat, geos] of byMat) group.add(new THREE.Mesh(mergeGeometries(geos, false), mat));
}

const NAMES_ALLY = ['Kestrel', 'Nomad', 'Vortex', 'Halcyon'];
const NAMES_ENEMY = ['Havoc', 'Wraith', 'Ronin', 'Cipher', 'Specter', 'Blaze', 'Talon'];

export class Bot {
  constructor(game, team, index) {
    this.game = game;
    this.team = team;
    this.isPlayer = false;
    this.name = (team === 0 ? NAMES_ALLY : NAMES_ENEMY)[index % (team === 0 ? 4 : 7)];
    this.pos = new THREE.Vector3();
    this.vel = new THREE.Vector3();
    this.radius = 0.35;
    this.height = 1.75;
    this.kills = 0; this.deaths = 0; this.score = 0; this.streak = 0;
    this.model = buildModel(team);
    game.scene.add(this.model.root);
    this.weapon = team === 0 ? BOT_WEAPON_ALLY : BOT_WEAPON;
    if (team === 0) {
      const tag = new THREE.Sprite(new THREE.SpriteMaterial({ map: TX.nameTag(this.name, '#5ef0ff'), depthTest: false, transparent: true }));
      tag.scale.set(1.3, 0.33, 1);
      tag.position.y = 2.25;
      tag.renderOrder = 10;
      this.model.root.add(tag);
      this.tag = tag;
    }
    this.alive = false;
    this.model.root.visible = false;
  }

  get diff() { return DIFF[this.game.settings.difficulty] || DIFF.regular; }

  spawn(pos, yaw) {
    this.pos.copy(pos);
    this.vel.set(0, 0, 0);
    this.health = 100;
    this.alive = true;
    this.deathT = 0;
    this.yaw = yaw;
    this.aimYaw = yaw;
    this.aimPitch = 0;
    this.target = null;
    this.reactT = 0;
    this.lastSeen = -99;
    this.lastSeenPos = null;
    this.heardPos = null;
    this.heardT = -99;
    this.path = null;
    this.pathI = 0;
    this.goal = null;
    this.repathT = 0;
    this.thinkT = Math.random() * 0.2;
    this.strafeDir = Math.random() < 0.5 ? -1 : 1;
    this.strafeT = 0;
    this.burstLeft = 0;
    this.burstCD = 0;
    this.shotCD = 0;
    this.nadeCD = rnd(8, 16);
    this.crouch = false;
    this.stuckT = 0;
    this.holdT = 0;
    this.holdYaw = yaw;
    this.walkPhase = 0;
    this.onGround = true;
    this.burnT = 0;
    this.model.root.visible = true;
    this.model.root.rotation.set(0, yaw, 0);
    this.model.root.position.copy(pos);
  }

  eyePos(out = new THREE.Vector3()) {
    return out.set(this.pos.x, this.pos.y + (this.crouch ? 1.2 : 1.62), this.pos.z);
  }
  chestPos(out = new THREE.Vector3()) {
    return out.set(this.pos.x, this.pos.y + (this.crouch ? 0.95 : 1.25), this.pos.z);
  }
  headPos(out = new THREE.Vector3()) {
    return out.set(this.pos.x, this.pos.y + (this.crouch ? 1.32 : 1.7), this.pos.z);
  }

  rayHit(o, d, maxDist) {
    if (!this.alive) return null;
    const head = raySphere(o, d, this.headPos(_h), 0.19);
    const top = this.crouch ? 1.2 : 1.55;
    const body = rayBox(o, d, this.pos.x - 0.3, this.pos.y, this.pos.z - 0.3, this.pos.x + 0.3, this.pos.y + top, this.pos.z + 0.3);
    let best = null;
    if (head !== null && head < maxDist) best = { dist: head, head: true };
    if (body !== null && body < maxDist && (!best || body < best.dist)) best = { dist: body, head: false };
    return best;
  }

  onDamaged(attacker) {
    if (!attacker || attacker === this || attacker.team === this.team) return;
    if (!this.target || !this.canSee(this.target)) {
      this.lastSeenPos = attacker.pos.clone();
      this.lastSeen = this.game.time;
      this.aimYaw = Math.atan2(-(attacker.pos.x - this.pos.x), -(attacker.pos.z - this.pos.z));
      this.path = null;
    }
  }

  hear(pos) {
    if (this.target) return;
    this.heardPos = pos.clone();
    this.heardT = this.game.time;
  }

  canSee(c) {
    if (!c.alive) return false;
    const eye = this.eyePos(_e1);
    const d = eye.distanceTo(c.pos);
    if (d > 60) return false;
    const w = this.game.world;
    if (w.los(eye, c.chestPos ? c.chestPos(_e2) : c.eyePos(_e2))) return true;
    return w.los(eye, c.eyePos(_e2));
  }

  _perceive() {
    const g = this.game;
    const fx = -Math.sin(this.yaw), fz = -Math.cos(this.yaw);
    let best = null, bestD = Infinity;
    const keep = this.target && this.target.alive && this.canSee(this.target) ? this.target : null;
    for (const c of g.chars) {
      if (!c.alive || c.team === this.team) continue;
      const dx = c.pos.x - this.pos.x, dz = c.pos.z - this.pos.z;
      const d = Math.hypot(dx, dz);
      const inFov = (dx * fx + dz * fz) / Math.max(d, 1e-3) > 0.1 || d < 6;
      if (!inFov) continue;
      if (!this.canSee(c)) continue;
      const score = d - (c === keep ? 8 : 0);
      if (score < bestD) { bestD = score; best = c; }
    }
    if (best && best !== this.target) {
      const r = this.diff.reaction;
      this.reactT = rnd(r[0], r[1]);
      this.burstLeft = 0;
      this.crouch = Math.random() < 0.25;
    }
    this.target = best;
    if (best) {
      this.lastSeen = g.time;
      this.lastSeenPos = best.pos.clone();
    } else {
      this.crouch = false;
    }
  }

  _pickGoal() {
    const g = this.game;
    const nav = g.nav;
    // Head toward the last place an enemy was seen/heard, else roam the map
    if (this.lastSeenPos && g.time - this.lastSeen < 8) return nav.nearest(this.lastSeenPos);
    if (this.heardPos && g.time - this.heardT < 6) return nav.nearest(this.heardPos);
    const enemies = g.chars.filter((c) => c.alive && c.team !== this.team);
    if (enemies.length && Math.random() < 0.35) {
      const e = enemies[Math.floor(Math.random() * enemies.length)];
      const p = e.pos.clone().add(new THREE.Vector3(rnd(-8, 8), 0, rnd(-8, 8)));
      return nav.nearest(p);
    }
    return nav.randomNode((n) => Math.abs(n.z) < 26);
  }

  update(dt, frozen) {
    const g = this.game;
    const m = this.model;
    if (!this.alive) {
      this.deathT += dt;
      const k = Math.min(1, this.deathT * 2.8);
      m.root.rotation.x = k * (Math.PI / 2) * this.fallDir;
      m.root.position.y = this.pos.y + Math.sin(k * Math.PI) * 0.25 - (this.deathT > 2.5 ? (this.deathT - 2.5) * 0.6 : 0);
      if (this.deathT > 3.6) m.root.visible = false;
      return;
    }
    const diff = this.diff;
    let wantX = 0, wantZ = 0, speed = 0;

    if (!frozen) {
      this.thinkT -= dt;
      if (this.thinkT <= 0) {
        this.thinkT = rnd(0.12, 0.2);
        this._perceive();
      }
      const t = this.target;
      if (t) {
        const dx = t.pos.x - this.pos.x, dz = t.pos.z - this.pos.z;
        const dist = Math.hypot(dx, dz);
        // aim
        const aimAt = t.chestPos(_e2);
        const eye = this.eyePos(_e1);
        const desiredYaw = Math.atan2(-(aimAt.x - eye.x), -(aimAt.z - eye.z));
        const desiredPitch = Math.atan2(aimAt.y - eye.y, Math.hypot(aimAt.x - eye.x, aimAt.z - eye.z));
        this.aimYaw = turnToward(this.aimYaw, desiredYaw, diff.turn * dt);
        this.aimPitch += (desiredPitch - this.aimPitch) * Math.min(1, dt * 8);
        const aimErr = Math.abs(angleDiff(this.aimYaw, desiredYaw));
        // strafe / reposition
        this.strafeT -= dt;
        if (this.strafeT <= 0) { this.strafeT = rnd(0.5, 1.6); this.strafeDir *= Math.random() < 0.7 ? -1 : 1; if (Math.random() < 0.2) this.crouch = !this.crouch; }
        const nx = dx / Math.max(dist, 1e-3), nz = dz / Math.max(dist, 1e-3);
        let mvx = -nz * this.strafeDir, mvz = nx * this.strafeDir;
        if (dist > 26) { mvx = mvx * 0.4 + nx; mvz = mvz * 0.4 + nz; }
        else if (dist < 5) { mvx = mvx * 0.5 - nx; mvz = mvz * 0.5 - nz; }
        const ml = Math.hypot(mvx, mvz) || 1;
        wantX = mvx / ml; wantZ = mvz / ml;
        speed = this.crouch ? 1.6 : 3.4;
        // shooting
        this.reactT -= dt;
        this.burstCD -= dt;
        this.shotCD -= dt;
        if (this.reactT <= 0 && aimErr < 0.18 && this.burstCD <= 0) {
          if (this.burstLeft <= 0) this.burstLeft = Math.round(rnd(diff.burst[0], diff.burst[1]));
          if (this.shotCD <= 0) {
            this._shoot(t, dist);
            this.shotCD = 60 / 620;
            this.burstLeft--;
            if (this.burstLeft <= 0) this.burstCD = rnd(diff.pause[0], diff.pause[1]);
          }
        }
        this.path = null;
      } else {
        // navigate
        this.repathT -= dt;
        if (!this.path || this.pathI >= this.path.length || this.repathT <= 0) {
          this.goal = this._pickGoal();
          this.path = this.goal ? g.nav.findPath(this.pos, this.goal) : null;
          this.pathI = 0;
          this.repathT = rnd(4, 7);
          if (!this.path) this.repathT = 0.5;
        }
        this.holdT -= dt;
        if (this.holdT > 0) {
          // holding an angle: stand still and sweep the view
          this.repathT = Math.max(this.repathT, 0.2);
          if (Math.random() < dt * 0.8) this.holdYaw = this.aimYaw + rnd(-1.4, 1.4);
        } else if (this.path && this.pathI < this.path.length) {
          const wp = this.path[this.pathI];
          const dx = wp.x - this.pos.x, dz = wp.z - this.pos.z;
          const d = Math.hypot(dx, dz);
          if (d < 0.6 && Math.abs(wp.y - this.pos.y) < 1.2) {
            this.pathI++;
            if (this.pathI >= this.path.length && Math.random() < 0.7) { this.holdT = rnd(1.5, 4.5); this.holdYaw = this.aimYaw; }
          } else { wantX = dx / d; wantZ = dz / d; }
          speed = g.time - this.lastSeen < 8 ? 4.6 : 5.4;
          this.crouch = false;
        }
        // look where we're going, or toward sounds
        let lookYaw = wantX || wantZ ? Math.atan2(-wantX, -wantZ) : this.holdT > 0 ? this.holdYaw : this.aimYaw;
        if (this.heardPos && g.time - this.heardT < 1.5) lookYaw = Math.atan2(-(this.heardPos.x - this.pos.x), -(this.heardPos.z - this.pos.z));
        this.aimYaw = turnToward(this.aimYaw, lookYaw, 5 * dt);
        this.aimPitch *= 1 - Math.min(1, dt * 4);
        // grenade at a remembered position
        this.nadeCD -= dt;
        if (this.nadeCD <= 0 && this.lastSeenPos && g.time - this.lastSeen > 1.5 && g.time - this.lastSeen < 6) {
          const d = this.pos.distanceTo(this.lastSeenPos);
          if (d > 8 && d < 24 && Math.random() < diff.nade) this._throwNade(this.lastSeenPos);
          this.nadeCD = rnd(10, 20);
        }
      }
    }

    // physics
    const accel = 28;
    const tx = wantX * speed, tz = wantZ * speed;
    const ddx = tx - this.vel.x, ddz = tz - this.vel.z;
    const dl = Math.hypot(ddx, ddz);
    if (dl > 1e-4) {
      const s = Math.min(dl, accel * dt);
      this.vel.x += (ddx / dl) * s;
      this.vel.z += (ddz / dl) * s;
    }
    this.vel.y -= 22 * dt;
    this.height = this.crouch ? 1.25 : 1.75;
    const before = this.pos.clone();
    moveCharacter(g.world, this, dt, { stepHeight: 0.55 });
    const moved = before.distanceTo(this.pos);
    if (speed > 1 && moved < speed * dt * 0.25) {
      this.stuckT += dt;
      if (this.stuckT > 0.8) {
        if (this.onGround) this.vel.y = 7;
        this.stuckT = 0;
        this.path = null;
        this.strafeDir *= -1;
      }
    } else this.stuckT = Math.max(0, this.stuckT - dt);
    if (this.pos.y < -10) g.killCharacter(this, null, 'FALL');
    this.burnT = Math.max(0, this.burnT - dt);

    // animation
    this.yaw = this.aimYaw;
    m.root.position.copy(this.pos);
    m.root.rotation.set(0, this.yaw, 0);
    const hs = Math.hypot(this.vel.x, this.vel.z);
    this.walkPhase += dt * hs * 2.1;
    // leg swing relative to facing direction (backpedal / strafe blends)
    const fwdSpeed = (-Math.sin(this.yaw) * this.vel.x - Math.cos(this.yaw) * this.vel.z);
    const amp = Math.min(1, hs / 4) * (fwdSpeed < -0.5 ? -1 : 1);
    const crouchK = this.crouch ? 1 : 0;
    m.hips.position.y = 0.95 - crouchK * 0.34 + Math.abs(Math.sin(this.walkPhase)) * 0.04 * Math.abs(amp);
    for (let i = 0; i < 2; i++) {
      const ph = this.walkPhase + i * Math.PI;
      const L = m.legs[i];
      L.thigh.rotation.x = Math.sin(ph) * 0.65 * amp + crouchK * 0.95;
      L.knee.rotation.x = -Math.max(0, -Math.cos(ph)) * 0.9 * Math.abs(amp) - crouchK * 1.7;
      L.thigh.rotation.z = (i ? -1 : 1) * 0.04;
    }
    m.torso.rotation.x = this.aimPitch * 0.9 + (this.crouch ? 0.1 : 0);
    m.torso.rotation.y = Math.sin(this.walkPhase) * 0.05 * Math.abs(amp);
    m.head.rotation.x = this.aimPitch * 0.2;
  }

  _shoot(target, dist) {
    const g = this.game;
    const diff = this.diff;
    const eye = this.eyePos(new THREE.Vector3());
    const wantHead = Math.random() < 0.18;
    const aim = wantHead && target.headPos ? target.headPos(new THREE.Vector3()) : wantHead ? target.eyePos(new THREE.Vector3()) : target.chestPos(new THREE.Vector3());
    const tSpeed = target.vel ? Math.hypot(target.vel.x, target.vel.z) : 0;
    let errR = diff.err * (0.25 + dist * 0.028) * (1 + tSpeed * 0.1);
    if (!target.isPlayer) errR *= 1.9; // bot-vs-bot fights resolve slower so matches last
    if (target.isPlayer && target.slideT > 0) errR *= 1.5;
    if (target.isPlayer && !target.onGround) errR *= 1.3;
    aim.x += (Math.random() - 0.5) * 2 * errR;
    aim.y += (Math.random() - 0.5) * 1.6 * errR;
    aim.z += (Math.random() - 0.5) * 2 * errR;
    const dir = aim.sub(eye).normalize();
    // muzzle position for tracer / flash
    const rx = Math.cos(this.yaw), rz = -Math.sin(this.yaw);
    const muzzle = eye.clone().addScaledVector(dir, 0.75).add(new THREE.Vector3(rx * 0.1, -0.2, rz * 0.1));
    this.dmgMul = diff.dmg;
    g.fireBullet(this, eye, dir, this.weapon, muzzle);
    g.fx.muzzleWorld(muzzle, dir, this.team === 0 ? [1.5, 3.5, 5] : [5, 2.6, 0.8]);
    g.audio.shot('bot', muzzle);
    g.onBotFired(this);
  }

  _throwNade(at) {
    const g = this.game;
    const from = this.eyePos(new THREE.Vector3());
    const d = new THREE.Vector3(at.x - from.x, 0, at.z - from.z);
    const dist = d.length();
    d.normalize();
    // 40 degree lob
    const ang = 0.7;
    const v = Math.sqrt((18 * dist) / Math.sin(2 * ang)) * 0.95;
    const vel = d.multiplyScalar(Math.cos(ang) * v).add(new THREE.Vector3(0, Math.sin(ang) * v, 0));
    g.throwGrenade(this, from.addScaledVector(d, 0.4), vel);
  }

  die(fromDir) {
    this.alive = false;
    this.deathT = 0;
    // fromDir < 0: hit from the front, so fall backwards
    this.fallDir = fromDir < 0 ? 1 : -1;
    this.target = null;
  }
}

function angleDiff(a, b) {
  let d = b - a;
  while (d > Math.PI) d -= Math.PI * 2;
  while (d < -Math.PI) d += Math.PI * 2;
  return d;
}
function turnToward(a, b, maxStep) {
  const d = angleDiff(a, b);
  return a + Math.max(-maxStep, Math.min(maxStep, d));
}

const _h = new THREE.Vector3(), _e1 = new THREE.Vector3(), _e2 = new THREE.Vector3();
