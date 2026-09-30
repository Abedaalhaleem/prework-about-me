// First-person player: advanced movement (sprint, power-slide, thrust jump,
// wall-run, mantle), health regen and hitboxes.
import * as THREE from 'three';
import { moveCharacter, probeWall } from './collision.js';

const GRAV = 22;
const STAND_H = 1.8, CROUCH_H = 1.2, SLIDE_H = 1.0;
const EYE_STAND = 1.64, EYE_CROUCH = 1.08, EYE_SLIDE = 0.82;

export class Player {
  constructor(game) {
    this.game = game;
    this.isPlayer = true;
    this.name = 'YOU';
    this.team = 0;
    this.pos = new THREE.Vector3();
    this.vel = new THREE.Vector3();
    this.radius = 0.35;
    this.height = STAND_H;
    this.yaw = 0;
    this.pitch = 0;
    this.roll = 0;
    this.eyeH = EYE_STAND;
    this.kills = 0; this.deaths = 0; this.score = 0; this.streak = 0;
    this.reset();
  }

  reset() {
    this.health = 100;
    this.alive = true;
    this.lastHurt = -99;
    this.onGround = false;
    this.crouch = false;
    this.slideT = 0;
    this.thrustFuel = 1;
    this.thrustUsed = false;
    this.wall = null;
    this.wallT = 0;
    this.wallCooldown = 0;
    this.lastWallBox = null;
    this.mantleT = 0;
    this.mantleTop = 0;
    this.airT = 0;
    this.coyote = 0;
    this.stepT = 0;
    this.bobT = 0;
    this.landDip = 0;
    this.burnT = 0;
    this.speed2d = 0;
    this.sprinting = false;
    this.vel.set(0, 0, 0);
  }

  spawn(pos, yaw) {
    this.reset();
    this.pos.copy(pos);
    this.yaw = yaw;
    this.pitch = 0;
    this.height = STAND_H;
    this.eyeH = EYE_STAND;
  }

  eyePos(out = new THREE.Vector3()) {
    return out.set(this.pos.x, this.pos.y + this.eyeH, this.pos.z);
  }
  chestPos(out = new THREE.Vector3()) {
    return out.set(this.pos.x, this.pos.y + this.height * 0.62, this.pos.z);
  }

  forward(out = new THREE.Vector3()) {
    return out.set(-Math.sin(this.yaw), 0, -Math.cos(this.yaw));
  }
  right(out = new THREE.Vector3()) {
    return out.set(Math.cos(this.yaw), 0, -Math.sin(this.yaw));
  }

  // Ray vs head sphere + body box. Returns {dist, head} or null.
  rayHit(o, d, maxDist) {
    if (!this.alive) return null;
    const hc = _v1.set(this.pos.x, this.pos.y + this.eyeH - 0.02, this.pos.z);
    const head = raySphere(o, d, hc, 0.2);
    const body = rayBox(o, d, this.pos.x - 0.3, this.pos.y, this.pos.z - 0.3, this.pos.x + 0.3, this.pos.y + this.height - 0.28, this.pos.z + 0.3);
    let best = null;
    if (head !== null && head < maxDist) best = { dist: head, head: true };
    if (body !== null && body < maxDist && (!best || body < best.dist)) best = { dist: body, head: false };
    return best;
  }

  update(dt, input, weapons) {
    if (!this.alive) return;
    const w = this.game.world;
    const fx = this.game.fx;
    const audio = this.game.audio;

    // --- look ---
    this.yaw -= input.mouseDX;
    this.pitch -= input.mouseDY;
    this.pitch = Math.max(-1.5, Math.min(1.5, this.pitch));

    const fwd = this.forward(_f), rgt = this.right(_r);
    const wish = _wish.set(0, 0, 0).addScaledVector(fwd, input.fwd).addScaledVector(rgt, input.strafe);
    const hasInput = wish.lengthSq() > 0.01;
    if (hasInput) wish.normalize();

    const ads = weapons.adsT > 0.5;
    const def = weapons.def;
    this.sprinting = input.sprint && input.fwd > 0.1 && !ads && !this.crouch && this.slideT <= 0 && !weapons.firingRecently();
    // --- crouch / slide ---
    if (input.crouchPressed) {
      if (this.onGround && this.sprinting && this.speed2d > 5.5) {
        this.slideT = 0.85;
        const boost = Math.max(this.speed2d, 9.5);
        this.vel.x = fwd.x * boost;
        this.vel.z = fwd.z * boost;
        audio.slide();
      } else {
        this.crouch = !this.crouch;
      }
    }
    if (this.slideT > 0) {
      this.slideT -= dt;
      if (this.slideT <= 0 || this.speed2d < 3) { this.slideT = 0; this.crouch = true; }
    }
    // stand up if there's headroom
    let targetH = this.slideT > 0 ? SLIDE_H : this.crouch ? CROUCH_H : STAND_H;
    if (targetH > this.height && w.overlaps(this.pos.x - this.radius, this.pos.y + this.height, this.pos.z - this.radius, this.pos.x + this.radius, this.pos.y + targetH, this.pos.z + this.radius)) {
      targetH = this.height;
      this.crouch = true;
    }
    this.height = targetH;
    const eyeTarget = this.slideT > 0 ? EYE_SLIDE : this.height < STAND_H - 0.1 ? EYE_CROUCH : EYE_STAND;
    this.eyeH += (eyeTarget - this.eyeH) * Math.min(1, dt * 12);
    if (this.eyeH > this.height - 0.08) this.eyeH = this.height - 0.08;

    // --- horizontal speed target ---
    let maxSpeed = this.sprinting ? 7.6 : 5.0;
    if (this.crouch) maxSpeed = 2.6;
    if (ads) maxSpeed *= def.adsMove;
    maxSpeed *= def.moveMult;

    this.wallCooldown -= dt;
    this.coyote = this.onGround ? 0.12 : this.coyote - dt;

    // --- jump / thrust / wall-jump ---
    if (input.jumpPressed) {
      if (this.wall) {
        const n = this.wall.normal;
        this.vel.x = n.x * 6.5 + fwd.x * 4.5;
        this.vel.z = n.z * 6.5 + fwd.z * 4.5;
        this.vel.y = 7.4;
        this.lastWallBox = this.wall.box;
        this.wall = null;
        this.wallCooldown = 0.35;
        audio.thrust();
      } else if (this.coyote > 0) {
        this.vel.y = 7.3;
        this.coyote = 0;
        if (this.slideT > 0) { this.slideT = 0; this.crouch = false; }
        else if (this.crouch) this.crouch = false;
      } else if (!this.thrustUsed && this.thrustFuel >= 0.3) {
        this.vel.y = Math.max(this.vel.y, 0) + 6.4;
        if (hasInput) { this.vel.x += wish.x * 2.2; this.vel.z += wish.z * 2.2; }
        this.thrustFuel -= 0.45;
        this.thrustUsed = true;
        fx.thruster(_v1.set(this.pos.x, this.pos.y + 0.2, this.pos.z), _v2.set(0, -1, 0));
        audio.thrust();
      }
    }

    // --- movement integration ---
    const hv = _hv.set(this.vel.x, 0, this.vel.z);
    if (this.mantleT > 0) {
      this.mantleT -= dt;
      if (this.pos.y >= this.mantleTop + 0.02) {
        // cleared the ledge: pop forward onto it instead of flying upward
        this.mantleT = 0;
        this.vel.y = Math.min(this.vel.y, 1.2);
        this.vel.x = fwd.x * 3.2;
        this.vel.z = fwd.z * 3.2;
      } else {
        this.vel.y = 6.5;
        this.vel.x = fwd.x * 1.5;
        this.vel.z = fwd.z * 1.5;
      }
    } else if (this.wall) {
      // Wall-run: stick to the wall, low gravity, keep speed along it
      this.wallT += dt;
      const n = this.wall.normal;
      const along = _v1.copy(fwd).addScaledVector(n, -fwd.dot(n)).normalize();
      const sp = Math.max(7.2, hv.length());
      this.vel.x = along.x * sp - n.x * 0.5;
      this.vel.z = along.z * sp - n.z * 0.5;
      this.vel.y = this.wallT < 0.25 ? Math.max(this.vel.y, 1.4) : this.vel.y - 5 * dt;
      const still = probeWall(w, this, _v2.copy(n).negate(), 0.35);
      if (this.wallT > 1.7 || !still || input.fwd <= 0 || this.onGround) {
        this.lastWallBox = this.wall.box;
        this.wall = null;
        this.wallCooldown = 0.3;
      }
    } else if (this.slideT > 0) {
      const sp = hv.length();
      const ns = Math.max(0, sp - 7 * dt);
      if (sp > 0.01) { this.vel.x *= ns / sp; this.vel.z *= ns / sp; }
      this.vel.y -= GRAV * dt;
    } else if (this.onGround) {
      const target = _v1.copy(wish).multiplyScalar(maxSpeed);
      const accel = hasInput ? 55 : 38;
      const dx = target.x - this.vel.x, dz = target.z - this.vel.z;
      const dl = Math.hypot(dx, dz);
      const step = Math.min(dl, accel * dt);
      if (dl > 1e-4) { this.vel.x += (dx / dl) * step; this.vel.z += (dz / dl) * step; }
      this.vel.y -= GRAV * dt;
    } else {
      // air control that preserves momentum
      const before = hv.length();
      this.vel.x += wish.x * 14 * dt;
      this.vel.z += wish.z * 14 * dt;
      const after = Math.hypot(this.vel.x, this.vel.z);
      const cap = Math.max(before, maxSpeed);
      if (after > cap) { this.vel.x *= cap / after; this.vel.z *= cap / after; }
      this.vel.y -= GRAV * dt;
    }
    this.vel.y = Math.max(this.vel.y, -40);

    const wasGround = this.onGround;
    const fallSpeed = -this.vel.y;
    moveCharacter(w, this, dt, { stepHeight: 0.55 });
    this.speed2d = Math.hypot(this.vel.x, this.vel.z);

    if (this.onGround) {
      if (!wasGround) {
        if (fallSpeed > 7) { this.landDip = Math.min(0.18, fallSpeed * 0.012); audio.footstep(null, 2); }
        this.thrustUsed = false;
        this.lastWallBox = null;
      }
      this.thrustFuel = Math.min(1, this.thrustFuel + dt * 0.55);
      this.airT = 0;
      this.wall = null;
      this.mantleT = 0;
    } else {
      this.airT += dt;
      // try to start a wall-run
      if (!this.wall && this.wallCooldown <= 0 && input.fwd > 0.1 && this.speed2d > 4 && this.airT > 0.08 && this.pos.y > 0.3) {
        for (const side of [-1, 1]) {
          const dir = _v2.copy(rgt).multiplyScalar(side);
          const hit = probeWall(w, this, dir, 0.3);
          if (hit && hit.box !== this.lastWallBox) {
            // wall must be roughly parallel to where we look
            if (Math.abs(fwd.dot(hit.normal)) < 0.55) {
              this.wall = hit;
              this.wallT = 0;
              this.thrustUsed = false;
              break;
            }
          }
        }
      }
      // mantle onto ledges we run into
      if (this.hitWall && this.wallBox && input.fwd > 0.1 && this.mantleT <= 0) {
        const nx = this.wallAxis === 'x' ? this.wallSign : 0, nz = this.wallAxis === 'z' ? this.wallSign : 0;
        const facing = fwd.x * nx + fwd.z * nz < -0.45;
        const top = this.wallBox.max.y;
        const rise = top - this.pos.y;
        const ox = this.pos.x - nx * (this.radius + 0.25), oz = this.pos.z - nz * (this.radius + 0.25);
        if (facing && rise > 0.05 && rise < 1.75 &&
            !w.overlaps(ox - this.radius, top + 0.05, oz - this.radius, ox + this.radius, top + this.height, oz + this.radius)) {
          this.mantleT = 0.5;
          this.mantleTop = top;
        }
      }
    }

    // camera roll during wall-run / slide
    let targetRoll = 0;
    if (this.wall) targetRoll = this.wall.normal.dot(rgt) > 0 ? -0.2 : 0.2;
    else if (this.slideT > 0) targetRoll = 0.06;
    this.roll += (targetRoll - this.roll) * Math.min(1, dt * 8);
    this.landDip *= Math.pow(0.02, dt);

    // footsteps / bob
    if ((this.onGround && this.slideT <= 0 && this.speed2d > 1.5) || this.wall) {
      this.bobT += dt * (this.speed2d * 1.35);
      this.stepT -= dt;
      if (this.stepT <= 0) {
        this.stepT = this.sprinting || this.wall ? 0.3 : 0.44;
        if (!this.crouch) audio.footstep(null, this.sprinting ? 1.2 : 0.8);
      }
    }

    // health regen
    const now = this.game.time;
    if (now - this.lastHurt > 4.2 && this.health < 100) this.health = Math.min(100, this.health + 45 * dt);
    this.burnT = Math.max(0, this.burnT - dt);

    if (this.pos.y < -10) this.game.killCharacter(this, null, 'FALL');
  }
}

// ---- small ray helpers shared with bots ----
export function raySphere(o, d, c, r) {
  const ox = o.x - c.x, oy = o.y - c.y, oz = o.z - c.z;
  const b = ox * d.x + oy * d.y + oz * d.z;
  const cc = ox * ox + oy * oy + oz * oz - r * r;
  const h = b * b - cc;
  if (h < 0) return null;
  const t = -b - Math.sqrt(h);
  return t >= 0 ? t : null;
}

export function rayBox(o, d, x0, y0, z0, x1, y1, z1) {
  let t1 = (x0 - o.x) / d.x, t2 = (x1 - o.x) / d.x;
  let tmin = Math.min(t1, t2), tmax = Math.max(t1, t2);
  t1 = (y0 - o.y) / d.y; t2 = (y1 - o.y) / d.y;
  tmin = Math.max(tmin, Math.min(t1, t2)); tmax = Math.min(tmax, Math.max(t1, t2));
  t1 = (z0 - o.z) / d.z; t2 = (z1 - o.z) / d.z;
  tmin = Math.max(tmin, Math.min(t1, t2)); tmax = Math.min(tmax, Math.max(t1, t2));
  if (tmax < Math.max(tmin, 0)) return null;
  return tmin >= 0 ? tmin : null;
}

const _v1 = new THREE.Vector3(), _v2 = new THREE.Vector3();
const _f = new THREE.Vector3(), _r = new THREE.Vector3(), _wish = new THREE.Vector3(), _hv = new THREE.Vector3();
