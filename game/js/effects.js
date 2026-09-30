// GPU-friendly particle pools for fire, smoke, sparks and explosions,
// plus tracers, decals, shockwaves and a pooled set of dynamic lights.
import * as THREE from 'three';
import * as TX from './textures.js';

const STRIDE = 22;
// layout: 0-2 pos, 3-5 vel, 6 life, 7 maxLife, 8 size0, 9 size1, 10-12 c0, 13-15 c1, 16 a0, 17 a1, 18 drag, 19 grav, 20 rot, 21 rotV

const vert = /* glsl */`
  attribute float size;
  attribute float alpha;
  attribute float rot;
  attribute vec3 pcolor;
  uniform float uScale;
  varying float vAlpha;
  varying vec3 vColor;
  varying float vRot;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = min(size * uScale / max(-mv.z, 0.05), 2048.0);
    vAlpha = alpha;
    vColor = pcolor;
    vRot = rot;
  }`;
const frag = /* glsl */`
  uniform sampler2D map;
  uniform float uAdditive;
  varying float vAlpha;
  varying vec3 vColor;
  varying float vRot;
  void main() {
    vec2 uv = vec2(gl_PointCoord.x, 1.0 - gl_PointCoord.y) - 0.5; // canvas textures are flipY
    float c = cos(vRot), s = sin(vRot);
    uv = vec2(c * uv.x - s * uv.y, s * uv.x + c * uv.y) + 0.5;
    vec4 tex = texture2D(map, uv);
    float a = tex.a * vAlpha;
    if (a < 0.003) discard;
    gl_FragColor = vec4(vColor * tex.rgb * mix(1.0, a, uAdditive), uAdditive > 0.5 ? 1.0 : a);
  }`;

class ParticlePool {
  constructor(scene, max, map, additive) {
    this.max = max;
    this.count = 0;
    this.data = new Float32Array(max * STRIDE);
    const g = new THREE.BufferGeometry();
    this.aPos = new THREE.BufferAttribute(new Float32Array(max * 3), 3).setUsage(THREE.DynamicDrawUsage);
    this.aCol = new THREE.BufferAttribute(new Float32Array(max * 3), 3).setUsage(THREE.DynamicDrawUsage);
    this.aAlpha = new THREE.BufferAttribute(new Float32Array(max), 1).setUsage(THREE.DynamicDrawUsage);
    this.aSize = new THREE.BufferAttribute(new Float32Array(max), 1).setUsage(THREE.DynamicDrawUsage);
    this.aRot = new THREE.BufferAttribute(new Float32Array(max), 1).setUsage(THREE.DynamicDrawUsage);
    g.setAttribute('position', this.aPos);
    g.setAttribute('pcolor', this.aCol);
    g.setAttribute('alpha', this.aAlpha);
    g.setAttribute('size', this.aSize);
    g.setAttribute('rot', this.aRot);
    this.mat = new THREE.ShaderMaterial({
      uniforms: { map: { value: map }, uScale: { value: 800 }, uAdditive: { value: additive ? 1 : 0 } },
      vertexShader: vert,
      fragmentShader: frag,
      transparent: true,
      depthWrite: false,
      blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending,
    });
    this.points = new THREE.Points(g, this.mat);
    this.points.frustumCulled = false;
    this.points.renderOrder = additive ? 3 : 2;
    scene.add(this.points);
  }

  spawn(px, py, pz, vx, vy, vz, life, s0, s1, c0, c1, a0, a1, drag = 0, grav = 0, rotV = 0, rot0 = null) {
    if (this.count >= this.max) return;
    const d = this.data, o = this.count++ * STRIDE;
    d[o] = px; d[o + 1] = py; d[o + 2] = pz;
    d[o + 3] = vx; d[o + 4] = vy; d[o + 5] = vz;
    d[o + 6] = 0; d[o + 7] = life;
    d[o + 8] = s0; d[o + 9] = s1;
    d[o + 10] = c0[0]; d[o + 11] = c0[1]; d[o + 12] = c0[2];
    d[o + 13] = c1[0]; d[o + 14] = c1[1]; d[o + 15] = c1[2];
    d[o + 16] = a0; d[o + 17] = a1;
    d[o + 18] = drag; d[o + 19] = grav;
    d[o + 20] = rot0 === null ? Math.random() * 6.283 : rot0; d[o + 21] = rotV;
  }

  update(dt, wind) {
    const d = this.data;
    const P = this.aPos.array, C = this.aCol.array, A = this.aAlpha.array, S = this.aSize.array, R = this.aRot.array;
    let i = 0;
    while (i < this.count) {
      const o = i * STRIDE;
      d[o + 6] += dt;
      if (d[o + 6] >= d[o + 7]) {
        // swap-remove
        const last = (this.count - 1) * STRIDE;
        if (o !== last) d.copyWithin(o, last, last + STRIDE);
        this.count--;
        continue;
      }
      const drag = Math.max(0, 1 - d[o + 18] * dt);
      d[o + 3] = d[o + 3] * drag + wind.x * dt * (d[o + 19] < 0 ? 1 : 0.2);
      d[o + 4] = d[o + 4] * drag - d[o + 19] * dt;
      d[o + 5] = d[o + 5] * drag + wind.z * dt * (d[o + 19] < 0 ? 1 : 0.2);
      d[o] += d[o + 3] * dt; d[o + 1] += d[o + 4] * dt; d[o + 2] += d[o + 5] * dt;
      d[o + 20] += d[o + 21] * dt;
      const t = d[o + 6] / d[o + 7];
      const tc = Math.pow(t, 0.7);
      const fadeIn = Math.min(1, t * 12);
      P[i * 3] = d[o]; P[i * 3 + 1] = d[o + 1]; P[i * 3 + 2] = d[o + 2];
      C[i * 3] = d[o + 10] + (d[o + 13] - d[o + 10]) * tc;
      C[i * 3 + 1] = d[o + 11] + (d[o + 14] - d[o + 11]) * tc;
      C[i * 3 + 2] = d[o + 12] + (d[o + 15] - d[o + 12]) * tc;
      A[i] = (d[o + 16] + (d[o + 17] - d[o + 16]) * t) * fadeIn;
      S[i] = d[o + 8] + (d[o + 9] - d[o + 8]) * t;
      R[i] = d[o + 20];
      i++;
    }
    this.points.geometry.setDrawRange(0, this.count);
    this.aPos.needsUpdate = this.aCol.needsUpdate = this.aAlpha.needsUpdate = this.aSize.needsUpdate = this.aRot.needsUpdate = true;
  }
}

class LightPool {
  constructor(scene, n) {
    this.lights = [];
    for (let i = 0; i < n; i++) {
      const l = new THREE.PointLight(0xffaa55, 0, 20, 2);
      l.userData = { t: 0, dur: 1, peak: 0 };
      scene.add(l);
      this.lights.push(l);
    }
  }
  flash(pos, color, intensity, range, dur) {
    let best = this.lights[0];
    for (const l of this.lights) if (l.intensity < best.intensity) best = l;
    best.position.copy(pos);
    best.color.set(color);
    best.distance = range;
    best.userData.t = 0;
    best.userData.dur = dur;
    best.userData.peak = intensity;
    best.intensity = intensity;
  }
  update(dt) {
    for (const l of this.lights) {
      if (l.intensity <= 0) continue;
      l.userData.t += dt;
      const k = 1 - l.userData.t / l.userData.dur;
      l.intensity = k > 0 ? l.userData.peak * k * k : 0;
    }
  }
}

const HOT = [2.1, 0.95, 0.22];
const RED = [0.8, 0.1, 0.015];
const SMOKE_DARK = [0.06, 0.055, 0.055];
const SMOKE_GREY = [0.35, 0.34, 0.33];

export class Effects {
  constructor(scene, quality = 'medium') {
    this.scene = scene;
    this.wind = new THREE.Vector3(0.7, 0, 0.3);
    const mult = quality === 'low' ? 0.5 : 1;
    this.density = mult;
    this.fire = new ParticlePool(scene, Math.floor(5000 * mult), TX.flame(), true);
    this.flames = new ParticlePool(scene, Math.floor(3000 * mult), TX.flameTongue(), true);
    this.glow = new ParticlePool(scene, Math.floor(2500 * mult), TX.softDot(), true);
    this.smoke = new ParticlePool(scene, Math.floor(2200 * mult), TX.smoke(), false);
    this.lights = new LightPool(scene, 4);
    this.emitters = [];
    this.shake = 0;
    this.camPos = new THREE.Vector3();

    // Decals
    const decalGeo = new THREE.PlaneGeometry(1, 1);
    const holeMat = new THREE.MeshBasicMaterial({ map: TX.bulletHole(), transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 });
    this.holes = new THREE.InstancedMesh(decalGeo, holeMat, 200);
    this.holes.frustumCulled = false;
    this.holes.count = 0;
    this.holeIdx = 0;
    scene.add(this.holes);
    const scorchMat = new THREE.MeshBasicMaterial({ map: TX.scorch(), transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -3 });
    this.scorches = new THREE.InstancedMesh(decalGeo, scorchMat, 40);
    this.scorches.frustumCulled = false;
    this.scorches.count = 0;
    this.scorchIdx = 0;
    scene.add(this.scorches);

    // Tracers: thin additive cross-quads stretched between points
    const tg = new THREE.BufferGeometry();
    const tv = new Float32Array([
      -0.5, 0, 0, 0.5, 0, 0, 0.5, 0, 1, -0.5, 0, 0, 0.5, 0, 1, -0.5, 0, 1,
      0, -0.5, 0, 0, 0.5, 0, 0, 0.5, 1, 0, -0.5, 0, 0, 0.5, 1, 0, -0.5, 1,
    ]);
    tg.setAttribute('position', new THREE.BufferAttribute(tv, 3));
    const tracerMat = new THREE.MeshBasicMaterial({ color: 0xffffff, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false, side: THREE.DoubleSide });
    this.tracerMesh = new THREE.InstancedMesh(tg, tracerMat, 96);
    this.tracerMesh.frustumCulled = false;
    this.tracerMesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(96 * 3), 3);
    this.tracerMesh.count = 0;
    this.tracers = [];
    scene.add(this.tracerMesh);

    // Shockwave rings
    this.rings = [];
    const ringTex = TX.ring();
    for (let i = 0; i < 6; i++) {
      const m = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
        map: ringTex, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, color: new THREE.Color(3, 1.6, 0.8), side: THREE.DoubleSide,
      }));
      m.rotation.x = -Math.PI / 2;
      m.visible = false;
      m.userData = { t: 0, dur: 0.5, size: 10 };
      scene.add(m);
      this.rings.push(m);
    }

    this._dummy = new THREE.Object3D();
    this._v = new THREE.Vector3();
  }

  setCamera(camera, heightPx) {
    const s = heightPx / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2));
    this.fire.mat.uniforms.uScale.value = s;
    this.flames.mat.uniforms.uScale.value = s;
    this.glow.mat.uniforms.uScale.value = s;
    this.smoke.mat.uniforms.uScale.value = s;
  }

  // ----- persistent / timed fire -----
  addFire(opts) {
    const e = {
      pos: opts.pos.clone(), radius: opts.radius ?? 0.5, rate: (opts.rate ?? 40) * this.density, scale: opts.scale ?? 1,
      life: opts.life ?? Infinity, age: 0, acc: 0, smokeAcc: 0, light: null, damageRadius: opts.damageRadius ?? 0,
      owner: opts.owner || null, phase: Math.random() * 100, baseIntensity: opts.lightIntensity ?? 22,
    };
    if (opts.light) {
      e.light = new THREE.PointLight(0xff7a2a, e.baseIntensity, 16, 1.6);
      e.light.position.copy(e.pos).add(new THREE.Vector3(0, 0.6, 0));
      this.scene.add(e.light);
    }
    this.emitters.push(e);
    return e;
  }

  _updateEmitter(e, dt) {
    e.age += dt;
    const fade = e.life === Infinity ? 1 : Math.max(0, Math.min(1, (e.life - e.age) / 1.5));
    e.acc += e.rate * dt * fade;
    const sc = e.scale;
    while (e.acc >= 1) {
      e.acc -= 1;
      const a = Math.random() * 6.283, r = Math.sqrt(Math.random()) * e.radius;
      const x = e.pos.x + Math.cos(a) * r, z = e.pos.z + Math.sin(a) * r;
      this.flames.spawn(x, e.pos.y + 0.25 * sc + Math.random() * 0.1, z,
        (Math.random() - 0.5) * 0.4, (1.4 + Math.random() * 1.4) * sc, (Math.random() - 0.5) * 0.4,
        0.4 + Math.random() * 0.5, (0.75 + Math.random() * 0.5) * sc, 0.2 * sc, HOT, RED, 0.42, 0, 1.2, -0.6,
        (Math.random() - 0.5) * 0.8, (Math.random() - 0.5) * 0.35);
      if (Math.random() < 0.35) {
        this.fire.spawn(x, e.pos.y + 0.1, z, 0, 0.6 * sc, 0, 0.35, 1.1 * sc, 0.6 * sc, [1.6, 0.55, 0.1], [0.6, 0.08, 0.01], 0.25, 0);
      }
      if (Math.random() < 0.08) {
        this.glow.spawn(x, e.pos.y + 0.3, z, (Math.random() - 0.5) * 1.2, 2 + Math.random() * 3, (Math.random() - 0.5) * 1.2,
          1.2 + Math.random() * 1.6, 0.07, 0.03, [4, 1.6, 0.3], [2, 0.3, 0.05], 1, 0.2, 0.3, -0.4);
      }
    }
    e.smokeAcc += e.rate * 0.22 * dt * fade;
    while (e.smokeAcc >= 1) {
      e.smokeAcc -= 1;
      this.smoke.spawn(e.pos.x + (Math.random() - 0.5) * e.radius, e.pos.y + 0.9 * sc, e.pos.z + (Math.random() - 0.5) * e.radius,
        (Math.random() - 0.5) * 0.3, 1.2 + Math.random() * 1.0, (Math.random() - 0.5) * 0.3,
        3.5 + Math.random() * 2.5, 1.0 * sc, 4.5 * sc, SMOKE_DARK, [0.03, 0.03, 0.035], 0.5, 0, 0.15, -0.05, (Math.random() - 0.5) * 0.6);
    }
    if (e.light) {
      const t = e.age + e.phase;
      const f = 0.72 + 0.14 * Math.sin(t * 13.1) + 0.1 * Math.sin(t * 23.7) + 0.06 * Math.sin(t * 41.3);
      e.light.intensity = e.baseIntensity * f * fade;
    }
    return e.age < e.life;
  }

  // ----- one-shot effects -----
  explosion(pos, scale = 1) {
    const s = scale;
    this.glow.spawn(pos.x, pos.y + 0.5, pos.z, 0, 0, 0, 0.14, 5 * s, 8 * s, [3, 2, 1.1], [1.4, 0.45, 0.08], 0.9, 0);
    for (let i = 0; i < 45 * this.density; i++) {
      const v = this._rand3(1).multiplyScalar((3 + Math.random() * 8) * s);
      v.y = Math.abs(v.y) * 0.9 + 1.5 * s;
      this.fire.spawn(pos.x, pos.y + 0.3, pos.z, v.x, v.y, v.z, 0.45 + Math.random() * 0.6,
        (1.0 + Math.random() * 0.9) * s, (2.4 + Math.random() * 1.4) * s, [2.2, 0.95, 0.25], RED, 0.3, 0, 3.4, -1.2, (Math.random() - 0.5) * 3);
    }
    for (let i = 0; i < 90 * this.density; i++) {
      const v = this._rand3(1).multiplyScalar((8 + Math.random() * 16) * s);
      v.y = Math.abs(v.y) + 2;
      this.glow.spawn(pos.x, pos.y + 0.3, pos.z, v.x, v.y, v.z, 0.5 + Math.random() * 1.1,
        0.12, 0.05, [5, 2.6, 0.6], [2.5, 0.5, 0.05], 1, 0.3, 0.8, 14);
    }
    for (let i = 0; i < 34 * this.density; i++) {
      const v = this._rand3(1).multiplyScalar((1.5 + Math.random() * 4) * s);
      v.y = Math.abs(v.y) * 0.6 + 1.2;
      this.smoke.spawn(pos.x + v.x * 0.2, pos.y + 0.8 + Math.random(), pos.z + v.z * 0.2, v.x, v.y, v.z,
        3 + Math.random() * 3, 2.2 * s, (7 + Math.random() * 4) * s, [0.12, 0.1, 0.09], SMOKE_DARK, 0.75, 0, 1.2, -0.25, (Math.random() - 0.5) * 0.8);
    }
    this.lights.flash(this._v.copy(pos).setY(pos.y + 1.5), 0xff8a3a, 220 * s, 26 * s, 0.55);
    const ring = this.rings.find((r) => !r.visible) || this.rings[0];
    ring.visible = true;
    ring.position.set(pos.x, Math.max(0.08, pos.y - 0.3), pos.z);
    ring.userData.t = 0;
    ring.userData.dur = 0.45;
    ring.userData.size = 16 * s;
    this.scorch(pos, 3.2 * s);
    this.addFire({ pos: pos.clone().setY(Math.max(0.05, pos.y - 0.4)), radius: 0.9 * s, rate: 35, scale: 0.8 * s, life: 7, damageRadius: 1.4 * s });
  }

  impact(point, normal, surface = 'concrete') {
    const n = normal;
    const sparks = surface === 'metal' ? 10 : surface === 'robot' ? 12 : 4;
    const sparkCol = surface === 'robot' ? [2.5, 3.5, 5] : [5, 3, 1];
    for (let i = 0; i < sparks; i++) {
      const v = this._rand3(1).add(this._v.copy(n).multiplyScalar(1.4)).normalize().multiplyScalar(3 + Math.random() * 7);
      this.glow.spawn(point.x, point.y, point.z, v.x, v.y, v.z, 0.15 + Math.random() * 0.35, 0.06, 0.02, sparkCol, [2, 0.5, 0.1], 1, 0.4, 1.5, 12);
    }
    if (surface === 'robot') {
      this.glow.spawn(point.x, point.y, point.z, 0, 0, 0, 0.07, 0.5, 0.9, [3, 4, 6], [1, 1, 3], 1, 0);
      return;
    }
    const dustCol = surface === 'grass' || surface === 'dirt' ? [0.22, 0.17, 0.1] : surface === 'wood' ? [0.25, 0.18, 0.12] : SMOKE_GREY;
    for (let i = 0; i < 3; i++) {
      const v = this._v.copy(n).multiplyScalar(0.8 + Math.random() * 1.2).add(this._rand3(0.3));
      this.smoke.spawn(point.x, point.y, point.z, v.x, v.y, v.z, 0.6 + Math.random() * 0.6, 0.12, 0.7, dustCol, dustCol, 0.55, 0, 2.5, 0.3);
    }
    if (surface !== 'grass' && surface !== 'dirt') this.bulletHole(point, n);
  }

  bulletHole(point, normal) {
    const d = this._dummy;
    d.position.copy(point).addScaledVector(normal, 0.012);
    d.lookAt(this._v.copy(d.position).add(normal));
    d.rotateZ(Math.random() * 6.28);
    const s = 0.1 + Math.random() * 0.05;
    d.scale.set(s, s, s);
    d.updateMatrix();
    this.holes.setMatrixAt(this.holeIdx, d.matrix);
    this.holeIdx = (this.holeIdx + 1) % 200;
    this.holes.count = Math.max(this.holes.count, this.holeIdx === 0 ? 200 : this.holeIdx);
    this.holes.instanceMatrix.needsUpdate = true;
  }

  scorch(pos, size) {
    const d = this._dummy;
    d.position.set(pos.x, 0.03, pos.z);
    d.rotation.set(-Math.PI / 2, 0, Math.random() * 6.28);
    d.scale.set(size, size, size);
    d.updateMatrix();
    this.scorches.setMatrixAt(this.scorchIdx, d.matrix);
    this.scorchIdx = (this.scorchIdx + 1) % 40;
    this.scorches.count = Math.max(this.scorches.count, this.scorchIdx === 0 ? 40 : this.scorchIdx);
    this.scorches.instanceMatrix.needsUpdate = true;
  }

  tracer(from, to, color = [3, 2.2, 1.2], speed = 420) {
    const dir = this._v.subVectors(to, from);
    const len = dir.length();
    if (len < 1) return;
    if (this.tracers.length >= 96) this.tracers.shift();
    this.tracers.push({ from: from.clone(), dir: dir.clone().divideScalar(len), len, age: 0, speed, color, streak: Math.min(7, len * 0.5) });
  }

  muzzleWorld(pos, dir, color = [5, 3, 1.2]) {
    this.glow.spawn(pos.x, pos.y, pos.z, dir.x * 2, dir.y * 2, dir.z * 2, 0.06, 0.55, 0.3, color, color, 1, 0);
    this.glow.spawn(pos.x + dir.x * 0.2, pos.y + dir.y * 0.2, pos.z + dir.z * 0.2, 0, 0, 0, 0.05, 0.35, 0.2, [6, 5, 3], color, 1, 0);
  }

  thruster(pos, down) {
    for (let i = 0; i < 14; i++) {
      const v = this._rand3(1.2).addScaledVector(down, 5 + Math.random() * 4);
      this.glow.spawn(pos.x, pos.y, pos.z, v.x, v.y, v.z, 0.25 + Math.random() * 0.2, 0.35, 0.05, [1.2, 3, 6], [0.2, 0.6, 2], 1, 0, 3);
    }
  }

  robotDeath(pos) {
    for (let i = 0; i < 30; i++) {
      const v = this._rand3(1).multiplyScalar(3 + Math.random() * 5);
      v.y = Math.abs(v.y) + 1;
      this.glow.spawn(pos.x, pos.y, pos.z, v.x, v.y, v.z, 0.3 + Math.random() * 0.6, 0.08, 0.03, [3, 4, 6], [1, 0.6, 0.2], 1, 0.2, 0.8, 12);
    }
    for (let i = 0; i < 6; i++) {
      this.smoke.spawn(pos.x, pos.y, pos.z, (Math.random() - 0.5), 1 + Math.random(), (Math.random() - 0.5), 1.5 + Math.random(), 0.5, 2, SMOKE_GREY, SMOKE_DARK, 0.5, 0, 0.8, -0.1);
    }
  }

  // Orbital strike beam coming down from the sky
  beam(pos) {
    for (let i = 0; i < 40; i++) {
      const y = pos.y + Math.random() * 80;
      this.glow.spawn(pos.x + (Math.random() - 0.5) * 0.4, y, pos.z + (Math.random() - 0.5) * 0.4, 0, -60, 0, 0.3, 1.2, 0.4, [2, 3.5, 6], [1, 1, 4], 1, 0);
    }
  }

  // End-of-match detonation on the horizon
  detonation(pos) {
    this.nuke = { pos: pos.clone(), t: 0 };
    this.lights.flash(this._v.set(0, 40, pos.z * 0.3), 0xffe0b0, 200000, 400, 4);
  }

  _updateNuke(dt) {
    const N = this.nuke;
    if (!N) return;
    N.t += dt;
    const t = N.t;
    if (t > 9) { this.nuke = null; return; }
    const p = N.pos;
    const k = Math.min(1, t / 3);
    const capY = 20 + k * 80;
    // spawn counts are per-second rates so the cloud looks the same at any frame rate
    N.acc = (N.acc || 0) + dt * 60;
    const n = Math.floor(N.acc);
    N.acc -= n;
    const fade = t > 6 ? Math.max(0, 1 - (t - 6) / 3) : 1;
    // stem
    for (let i = 0; i < Math.round(n * 3 * fade); i++) {
      const y = Math.random() * capY;
      this.fire.spawn(p.x + (Math.random() - 0.5) * 12, p.y + y, p.z + (Math.random() - 0.5) * 12, 0, 8, 0, 1.4, 14, 20, [2.2, 0.9, 0.25], [0.8, 0.12, 0.03], 0.35, 0, 0.5);
    }
    // cap (torus)
    for (let i = 0; i < Math.round(n * 5 * fade); i++) {
      const a = Math.random() * 6.283;
      const r = 20 + k * 40 + Math.random() * 10;
      this.fire.spawn(p.x + Math.cos(a) * r * 0.8, p.y + capY + (Math.random() - 0.5) * 20, p.z + Math.sin(a) * r * 0.8,
        Math.cos(a) * 6, 4, Math.sin(a) * 6, 1.8, 24, 36, [2.4, 1.0, 0.3], [0.9, 0.14, 0.03], 0.35, 0, 0.3);
      if (i % 2 === 0) this.smoke.spawn(p.x + Math.cos(a) * r, p.y + capY + 12, p.z + Math.sin(a) * r, Math.cos(a) * 4, 3, Math.sin(a) * 4, 6, 34, 60, [0.3, 0.14, 0.09], [0.06, 0.045, 0.045], 0.8, 0, 0.1);
    }
    // ground shockwave dust
    if (t < 4) {
      for (let i = 0; i < n * 8; i++) {
        const a = Math.random() * 6.283;
        const r = t * 55;
        this.smoke.spawn(p.x + Math.cos(a) * r, 3, p.z + Math.sin(a) * r, Math.cos(a) * 20, 2, Math.sin(a) * 20, 3, 14, 30, [0.5, 0.3, 0.2], [0.2, 0.12, 0.1], 0.6, 0, 0.3);
      }
    }
  }

  update(dt, camPos) {
    this.emitters = this.emitters.filter((e) => {
      const alive = this._updateEmitter(e, dt);
      if (!alive && e.light) this.scene.remove(e.light);
      return alive;
    });
    this._updateNuke(dt);
    this.fire.update(dt, this.wind);
    this.flames.update(dt, this.wind);
    this.glow.update(dt, this.wind);
    this.smoke.update(dt, this.wind);
    this.lights.update(dt);

    // tracers
    const d = this._dummy;
    let n = 0;
    const col = new THREE.Color();
    this.tracers = this.tracers.filter((t) => {
      t.age += dt;
      const travel = t.age * t.speed;
      const head = Math.min(t.len, travel);
      const tail = Math.max(0, travel - t.streak);
      if (tail >= t.len - 0.01) return false;
      const segLen = head - tail;
      if (segLen <= 0.01) return true;
      d.position.copy(t.from).addScaledVector(t.dir, tail);
      d.lookAt(this._v.copy(d.position).add(t.dir));
      const distToCam = camPos ? d.position.distanceTo(camPos) : 10;
      const w = Math.min(0.06, 0.012 + distToCam * 0.0012);
      d.scale.set(w, w, segLen);
      d.updateMatrix();
      this.tracerMesh.setMatrixAt(n, d.matrix);
      col.setRGB(t.color[0], t.color[1], t.color[2]);
      this.tracerMesh.setColorAt(n, col);
      n++;
      return true;
    });
    this.tracerMesh.count = n;
    this.tracerMesh.instanceMatrix.needsUpdate = true;
    if (this.tracerMesh.instanceColor) this.tracerMesh.instanceColor.needsUpdate = true;

    for (const r of this.rings) {
      if (!r.visible) continue;
      r.userData.t += dt;
      const k = r.userData.t / r.userData.dur;
      if (k >= 1) { r.visible = false; continue; }
      const sz = r.userData.size * (0.15 + Math.pow(k, 0.5));
      r.scale.set(sz, sz, sz);
      r.material.opacity = 1 - k;
    }
    this.shake = Math.max(0, this.shake - dt * 1.6);
  }

  _rand3(s) {
    return new THREE.Vector3((Math.random() - 0.5) * 2 * s, (Math.random() - 0.5) * 2 * s, (Math.random() - 0.5) * 2 * s);
  }
}
