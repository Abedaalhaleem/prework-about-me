// Player weapons: stats, procedural first-person gun models with arms,
// ADS / recoil / reload / sprint animations, muzzle flash, shell casings,
// melee and frag grenades.
import * as THREE from 'three';
import * as TX from './textures.js';

export const WEAPONS = [
  {
    id: 'ar', name: 'ARC-7', type: 'ASSAULT RIFLE', auto: true, rpm: 700, dmg: [34, 24], range: [18, 45], head: 1.5,
    mag: 30, reserve: 180, reload: 2.0, emptyReload: 2.4, hip: 3.0, ads: 0.2, move: 2.2, air: 5, bloom: 0.35,
    recoilUp: 0.5, recoilSide: 0.28, zoom: 1.35, adsTime: 0.2, adsMove: 0.6, moveMult: 1.0, sound: 'ar',
    tracer: [3.2, 2.4, 1.3], hip3: [0.1, -0.118, -0.3], vmFovAds: 42,
  },
  {
    id: 'smg', name: 'VANTA-9', type: 'SUBMACHINE GUN', auto: true, rpm: 950, dmg: [27, 16], range: [9, 26], head: 1.4,
    mag: 36, reserve: 216, reload: 1.7, emptyReload: 2.1, hip: 2.3, ads: 0.45, move: 1.4, air: 4, bloom: 0.25,
    recoilUp: 0.36, recoilSide: 0.42, zoom: 1.2, adsTime: 0.14, adsMove: 0.75, moveMult: 1.08, sound: 'smg',
    tracer: [3.2, 2.2, 1.0], hip3: [0.095, -0.11, -0.27], vmFovAds: 45,
  },
  {
    id: 'sniper', name: 'LONGBOW-X', type: 'SNIPER RIFLE', auto: false, rpm: 52, dmg: [125, 108], range: [30, 80], head: 2.0,
    mag: 5, reserve: 25, reload: 2.7, emptyReload: 3.0, hip: 7, ads: 0, move: 3, air: 9, bloom: 0,
    recoilUp: 3.0, recoilSide: 0.5, zoom: 4.5, adsTime: 0.3, adsMove: 0.45, moveMult: 0.92, sound: 'sniper',
    tracer: [3.5, 3.5, 4.5], hip3: [0.105, -0.122, -0.31], vmFovAds: 40, scope: true,
  },
];

// ---------------------------------------------------------------------------
// Model building helpers
function slab(points, thickness, mat, bevel = 0.004) {
  const shape = new THREE.Shape(points.map(([x, y]) => new THREE.Vector2(x, y)));
  const g = new THREE.ExtrudeGeometry(shape, {
    depth: thickness, bevelEnabled: true, bevelThickness: bevel, bevelSize: bevel, bevelSegments: 2, steps: 1, curveSegments: 4,
  });
  g.translate(0, 0, -thickness / 2);
  g.rotateY(Math.PI / 2); // profile +x -> forward (-z)
  const m = new THREE.Mesh(g, mat);
  return m;
}
// box in gun-local space using forward coordinate f (z = -f)
function bx(w, h, len, f, y, x, mat) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, len), mat);
  m.position.set(x, y, -f);
  return m;
}
function cyl(r, len, f, y, x, mat, seg = 14) {
  const g = new THREE.CylinderGeometry(r, r, len, seg);
  g.rotateX(Math.PI / 2);
  const m = new THREE.Mesh(g, mat);
  m.position.set(x, y, -f);
  return m;
}
function limb(a, b, r, mat) {
  const len = a.distanceTo(b);
  const g = new THREE.CapsuleGeometry(r, Math.max(0.01, len - r * 2), 4, 10);
  const m = new THREE.Mesh(g, mat);
  m.position.copy(a).add(b).multiplyScalar(0.5);
  m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), b.clone().sub(a).normalize());
  return m;
}

function makeMats(accent) {
  return {
    polymer: new THREE.MeshStandardMaterial({ color: '#2b3039', roughness: 0.5, metalness: 0.35 }),
    polymer2: new THREE.MeshStandardMaterial({ color: '#56606f', roughness: 0.4, metalness: 0.5 }),
    metal: new THREE.MeshStandardMaterial({ color: '#6b7280', roughness: 0.3, metalness: 0.9 }),
    bright: new THREE.MeshStandardMaterial({ color: '#9aa3b2', roughness: 0.25, metalness: 1.0 }),
    accent: new THREE.MeshStandardMaterial({ color: '#0a0a0a', emissive: accent, emissiveIntensity: 3.2, roughness: 0.4 }),
    sleeve: new THREE.MeshStandardMaterial({ color: '#3a434e', roughness: 0.85, metalness: 0.1 }),
    glove: new THREE.MeshStandardMaterial({ color: '#23262c', roughness: 0.7, metalness: 0.2 }),
    armor: new THREE.MeshStandardMaterial({ color: '#6a7686', roughness: 0.35, metalness: 0.7 }),
    lens: new THREE.MeshStandardMaterial({ color: '#0a1830', roughness: 0.05, metalness: 1.0, emissive: '#081a3a', emissiveIntensity: 0.6 }),
  };
}

function addArms(g, M, rightHand, rightElbow, leftHand, leftElbow) {
  // gloves
  const rg = new THREE.Mesh(new THREE.BoxGeometry(0.055, 0.085, 0.075), M.glove);
  rg.position.copy(rightHand);
  rg.rotation.x = 0.25;
  g.add(rg);
  const lg = new THREE.Mesh(new THREE.BoxGeometry(0.07, 0.05, 0.085), M.glove);
  lg.position.copy(leftHand);
  lg.rotation.z = 0.4;
  g.add(lg);
  // forearms + armour plates + glowing armband
  g.add(limb(rightHand.clone().add(new THREE.Vector3(0.005, -0.02, 0.03)), rightElbow, 0.038, M.sleeve));
  g.add(limb(leftHand.clone().add(new THREE.Vector3(-0.01, -0.02, 0.03)), leftElbow, 0.038, M.sleeve));
  const rp = limb(rightHand.clone().lerp(rightElbow, 0.35), rightHand.clone().lerp(rightElbow, 0.75), 0.043, M.armor);
  const lp = limb(leftHand.clone().lerp(leftElbow, 0.35), leftHand.clone().lerp(leftElbow, 0.75), 0.043, M.armor);
  g.add(rp, lp);
  const band = limb(leftHand.clone().lerp(leftElbow, 0.8), leftHand.clone().lerp(leftElbow, 0.84), 0.046, M.accent);
  g.add(band);
}

function ammoDisplay() {
  const c = document.createElement('canvas');
  c.width = 64; c.height = 32;
  const ctx = c.getContext('2d');
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.MeshBasicMaterial({ map: tex, transparent: true, color: new THREE.Color(1.8, 1.8, 1.8) });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(0.05, 0.025), mat);
  let last = -1;
  function set(n, low) {
    if (n === last) return;
    last = n;
    ctx.clearRect(0, 0, 64, 32);
    ctx.fillStyle = 'rgba(0,10,20,0.6)';
    ctx.fillRect(0, 0, 64, 32);
    ctx.font = '700 24px Orbitron, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = low ? '#ff4a3a' : '#5ef0ff';
    ctx.fillText(String(n).padStart(2, '0'), 32, 17);
    tex.needsUpdate = true;
  }
  return { mesh, set };
}

function buildAR(M) {
  const g = new THREE.Group();
  g.add(slab([[-0.14, -0.03], [0.18, -0.03], [0.22, 0.0], [0.22, 0.03], [0.12, 0.05], [-0.14, 0.05]], 0.052, M.polymer));
  g.add(slab([[0.18, -0.025], [0.47, -0.016], [0.47, 0.03], [0.18, 0.042]], 0.058, M.polymer2));
  g.add(bx(0.066, 0.006, 0.24, 0.32, 0.004, 0, M.accent));
  g.add(bx(0.03, 0.012, 0.34, 0.03, 0.057, 0, M.metal));
  g.add(cyl(0.011, 0.16, 0.54, 0.012, 0, M.metal));
  g.add(cyl(0.019, 0.07, 0.62, 0.012, 0, M.bright));
  g.add(slab([[-0.14, 0.04], [-0.4, 0.024], [-0.43, -0.07], [-0.36, -0.085], [-0.3, -0.02], [-0.14, -0.025]], 0.042, M.polymer));
  g.add(bx(0.046, 0.1, 0.02, -0.425, -0.022, 0, M.accent));
  g.add(slab([[-0.075, -0.028], [-0.028, -0.028], [-0.05, -0.145], [-0.1, -0.145]], 0.036, M.polymer2));
  g.add(bx(0.012, 0.006, 0.07, 0.0, -0.058, 0, M.metal));
  const mag = new THREE.Group();
  mag.add(slab([[0.03, -0.028], [0.095, -0.028], [0.118, -0.18], [0.052, -0.192]], 0.034, M.metal));
  mag.add(bx(0.038, 0.1, 0.008, 0.078, -0.1, 0, M.accent));
  g.add(mag);
  // holographic sight
  g.add(bx(0.04, 0.014, 0.07, 0.0, 0.068, 0, M.metal));
  g.add(bx(0.006, 0.05, 0.05, 0.0, 0.095, 0.024, M.polymer));
  g.add(bx(0.006, 0.05, 0.05, 0.0, 0.095, -0.024, M.polymer));
  g.add(bx(0.054, 0.008, 0.05, 0.0, 0.122, 0, M.polymer));
  const ret = new THREE.Mesh(new THREE.PlaneGeometry(0.042, 0.042), new THREE.MeshBasicMaterial({
    map: TX.reticle('#ff3a3a'), transparent: true, depthWrite: false, color: new THREE.Color(2.5, 2.5, 2.5), blending: THREE.AdditiveBlending,
  }));
  ret.position.set(0, 0.095, 0.0);
  g.add(ret);
  const muzzle = new THREE.Object3D();
  muzzle.position.set(0, 0.012, -0.66);
  g.add(muzzle);
  const ammo = ammoDisplay();
  ammo.mesh.position.set(-0.031, 0.012, -0.05);
  ammo.mesh.rotation.y = -Math.PI / 2;
  g.add(ammo.mesh);
  addArms(g, M, new THREE.Vector3(0.0, -0.085, 0.07), new THREE.Vector3(0.1, -0.3, 0.42),
    new THREE.Vector3(-0.012, -0.035, -0.34), new THREE.Vector3(-0.24, -0.3, -0.04));
  return { group: g, mag, muzzle, sight: new THREE.Vector3(0, 0.095, 0), ammo, eject: new THREE.Vector3(0.03, 0.03, -0.05) };
}

function buildSMG(M) {
  const g = new THREE.Group();
  g.add(slab([[-0.1, -0.035], [0.16, -0.035], [0.2, 0.0], [0.2, 0.036], [-0.1, 0.046]], 0.05, M.polymer));
  g.add(bx(0.056, 0.006, 0.18, 0.06, 0.02, 0, M.accent));
  g.add(cyl(0.021, 0.13, 0.26, 0.008, 0, M.polymer2));
  g.add(cyl(0.012, 0.08, 0.35, 0.008, 0, M.metal));
  g.add(bx(0.03, 0.085, 0.034, 0.14, -0.078, 0, M.polymer2));
  g.add(slab([[-0.1, 0.03], [-0.25, 0.02], [-0.265, -0.045], [-0.24, -0.05], [-0.13, -0.01], [-0.1, -0.02]], 0.03, M.metal));
  g.add(slab([[-0.06, -0.03], [-0.02, -0.03], [-0.04, -0.13], [-0.085, -0.13]], 0.034, M.polymer2));
  const mag = new THREE.Group();
  mag.add(slab([[0.02, -0.033], [0.068, -0.033], [0.072, -0.22], [0.024, -0.22]], 0.03, M.metal));
  mag.add(bx(0.034, 0.12, 0.006, 0.046, -0.13, 0, M.accent));
  g.add(mag);
  g.add(bx(0.032, 0.012, 0.05, 0.02, 0.054, 0, M.metal));
  g.add(bx(0.036, 0.04, 0.008, 0.035, 0.078, 0, M.polymer));
  const ret = new THREE.Mesh(new THREE.PlaneGeometry(0.03, 0.03), new THREE.MeshBasicMaterial({
    map: TX.reticle('#3ce8ff'), transparent: true, depthWrite: false, color: new THREE.Color(2.5, 2.5, 2.5), blending: THREE.AdditiveBlending,
  }));
  ret.position.set(0, 0.078, -0.03);
  g.add(ret);
  const muzzle = new THREE.Object3D();
  muzzle.position.set(0, 0.008, -0.4);
  g.add(muzzle);
  const ammo = ammoDisplay();
  ammo.mesh.position.set(-0.029, 0.008, -0.08);
  ammo.mesh.rotation.y = -Math.PI / 2;
  g.add(ammo.mesh);
  addArms(g, M, new THREE.Vector3(0.0, -0.08, 0.06), new THREE.Vector3(0.1, -0.3, 0.4),
    new THREE.Vector3(-0.005, -0.1, -0.14), new THREE.Vector3(-0.22, -0.32, 0.05));
  return { group: g, mag, muzzle, sight: new THREE.Vector3(0, 0.078, -0.03), ammo, eject: new THREE.Vector3(0.03, 0.02, -0.02) };
}

function buildSniper(M) {
  const g = new THREE.Group();
  g.add(slab([[-0.16, -0.03], [0.26, -0.03], [0.26, 0.035], [-0.16, 0.042]], 0.05, M.polymer));
  g.add(bx(0.056, 0.006, 0.3, 0.06, 0.0, 0, M.accent));
  g.add(cyl(0.014, 0.46, 0.49, 0.012, 0, M.metal));
  g.add(cyl(0.024, 0.08, 0.74, 0.012, 0, M.bright));
  g.add(slab([[-0.16, 0.042], [-0.44, 0.03], [-0.46, -0.09], [-0.38, -0.1], [-0.33, -0.03], [-0.25, -0.03], [-0.2, -0.08], [-0.16, -0.03]], 0.044, M.polymer2));
  g.add(slab([[-0.08, -0.03], [-0.03, -0.03], [-0.055, -0.14], [-0.1, -0.14]], 0.036, M.polymer));
  const mag = new THREE.Group();
  mag.add(slab([[0.04, -0.028], [0.12, -0.028], [0.12, -0.09], [0.04, -0.09]], 0.036, M.metal));
  mag.add(bx(0.04, 0.03, 0.06, 0.08, -0.06, 0, M.accent));
  g.add(mag);
  // scope
  g.add(bx(0.02, 0.03, 0.02, -0.04, 0.055, 0, M.metal));
  g.add(bx(0.02, 0.03, 0.02, 0.12, 0.055, 0, M.metal));
  g.add(cyl(0.026, 0.3, 0.03, 0.098, 0, M.polymer));
  g.add(cyl(0.036, 0.07, 0.19, 0.098, 0, M.polymer));
  g.add(cyl(0.032, 0.06, -0.12, 0.098, 0, M.polymer));
  g.add(cyl(0.033, 0.01, 0.2, 0.098, 0, M.accent));
  const lens = cyl(0.03, 0.005, 0.226, 0.098, 0, M.lens);
  g.add(lens);
  // bolt
  const bolt = cyl(0.008, 0.06, -0.02, 0.03, 0.045, M.bright);
  bolt.rotation.set(0, Math.PI / 2, 0);
  g.add(bolt);
  const muzzle = new THREE.Object3D();
  muzzle.position.set(0, 0.012, -0.79);
  g.add(muzzle);
  const ammo = ammoDisplay();
  ammo.mesh.position.set(-0.03, 0.005, -0.12);
  ammo.mesh.rotation.y = -Math.PI / 2;
  g.add(ammo.mesh);
  addArms(g, M, new THREE.Vector3(0.0, -0.085, 0.08), new THREE.Vector3(0.1, -0.3, 0.44),
    new THREE.Vector3(-0.012, -0.035, -0.36), new THREE.Vector3(-0.24, -0.3, -0.05));
  return { group: g, mag, muzzle, sight: new THREE.Vector3(0, 0.098, 0.05), ammo, eject: new THREE.Vector3(0.03, 0.03, -0.02), bolt };
}

// ---------------------------------------------------------------------------
export class WeaponSystem {
  constructor(game, vmScene, vmCamera) {
    this.game = game;
    this.vmScene = vmScene;
    this.vmCamera = vmCamera;
    this.root = new THREE.Group();
    vmCamera.add(this.root);
    vmScene.add(vmCamera);
    const M = makeMats(new THREE.Color('#3ce8ff'));
    this.models = [buildAR(M), buildSMG(M), buildSniper(M)];
    const S = 0.62; // overall viewmodel scale
    for (const m of this.models) {
      m.group.visible = false;
      m.group.scale.setScalar(S);
      m.group.traverse((o) => { if (o.isMesh) o.frustumCulled = false; });
      this.root.add(m.group);
      m.adsPos = new THREE.Vector3(-m.sight.x * S, -m.sight.y * S, -0.19 - m.sight.z * S);
      m.magBase = m.mag.position.clone();
    }
    // lights for the viewmodel scene (reflections come from the world's sky env map)
    vmScene.environment = game.scene.environment;
    vmScene.environmentIntensity = 0.9;
    vmScene.add(new THREE.HemisphereLight('#9fb4ff', '#40251a', 1.1));
    const key = new THREE.DirectionalLight('#ffc28a', 2.2);
    key.position.set(-1, 1.5, 1);
    vmScene.add(key);
    const rim = new THREE.DirectionalLight('#6a7dff', 1.2);
    rim.position.set(1, 0.5, -1);
    vmScene.add(rim);
    this.flashLight = new THREE.PointLight('#ffb060', 0, 2.5, 2);
    vmScene.add(this.flashLight);

    // muzzle flash (3 crossed quads)
    const flashTex = TX.muzzleFlash();
    const fm = new THREE.MeshBasicMaterial({ map: flashTex, color: new THREE.Color(5, 3.4, 1.6), transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
    this.flash = new THREE.Group();
    const q1 = new THREE.Mesh(new THREE.PlaneGeometry(0.24, 0.24), fm);
    const q2 = new THREE.Mesh(new THREE.PlaneGeometry(0.12, 0.34), fm);
    q2.rotation.y = Math.PI / 2;
    q2.position.z = -0.12;
    const q3 = q2.clone();
    q3.rotation.z = Math.PI / 2;
    this.flash.add(q1, q2, q3);
    this.flash.visible = false;

    // shell casings
    this.shells = [];
    const shellGeo = new THREE.CylinderGeometry(0.005, 0.005, 0.028, 8);
    const shellMat = new THREE.MeshStandardMaterial({ color: '#c89b3c', metalness: 1, roughness: 0.3 });
    for (let i = 0; i < 14; i++) {
      const s = new THREE.Mesh(shellGeo, shellMat);
      s.visible = false;
      s.userData = { v: new THREE.Vector3(), t: 0, spin: new THREE.Vector3() };
      vmCamera.add(s);
      this.shells.push(s);
    }
    this.shellIdx = 0;

    this.reset();
  }

  reset() {
    this.state = WEAPONS.map((w) => ({ ammo: w.mag, reserve: w.reserve }));
    this.slot = 0;
    this.nades = 2;
    this.adsT = 0;
    this.lastShot = -9;
    this.reloadT = 0;
    this.reloadDur = 0;
    this.switchT = 0;
    this.pendingSlot = -1;
    this.kick = 0;
    this.bloom = 0;
    this.recoilAccum = 0;
    this.sprintT = 0;
    this.meleeT = 0;
    this.throwT = 0;
    this.meleeDone = false;
    this.throwDone = false;
    this.swayX = 0; this.swayY = 0;
    this.triggerWas = false;
    this.punch = 0;
    this.boltT = 0;
    this._equip(0);
  }

  get def() { return WEAPONS[this.slot]; }
  get st() { return this.state[this.slot]; }

  _equip(i) {
    this.slot = i;
    this.models.forEach((m, k) => { m.group.visible = k === i; });
    const m = this.models[i];
    m.muzzle.add(this.flash);
    this.flash.position.set(0, 0, -0.06);
    m.mag.position.copy(m.magBase);
    m.ammo.set(this.st.ammo, this.st.ammo <= this.def.mag * 0.25);
  }

  firingRecently() {
    return this.game.time - this.lastShot < 0.25 || this.triggerHeld;
  }

  refill() {
    this.state.forEach((s, i) => { s.ammo = WEAPONS[i].mag; s.reserve = WEAPONS[i].reserve; });
    this.nades = 2;
    this.models[this.slot].ammo.set(this.st.ammo, false);
  }

  update(dt, input, player) {
    const game = this.game;
    const def = this.def;
    const st = this.st;
    const m = this.models[this.slot];
    this.triggerHeld = input.fire;
    const busy = this.meleeT > 0 || this.throwT > 0 || this.switchT > 0;

    // weapon switch
    if (input.switchTo >= 0 && input.switchTo !== this.slot && this.switchT <= 0 && this.meleeT <= 0) {
      this.pendingSlot = input.switchTo;
      this.switchT = 0.5;
      this.reloadT = 0;
      game.audio.reload(2);
    }
    if (this.switchT > 0) {
      const before = this.switchT;
      this.switchT -= dt;
      if (before > 0.25 && this.switchT <= 0.25 && this.pendingSlot >= 0) {
        this._equip(this.pendingSlot);
        this.pendingSlot = -1;
      }
    }

    // ADS
    const wantAds = input.ads && player.alive && this.switchT <= 0 && this.meleeT <= 0 && player.slideT <= 0 && !player.wall;
    this.adsT = THREE.MathUtils.clamp(this.adsT + (wantAds ? dt : -dt) / def.adsTime, 0, 1);

    // sprint pose
    const sprinting = player.sprinting && !this.firingRecently() && this.reloadT <= 0;
    this.sprintT = THREE.MathUtils.clamp(this.sprintT + (sprinting ? dt : -dt) * 5, 0, 1);

    // reload
    if (input.reload && this.reloadT <= 0 && st.ammo < def.mag && st.reserve > 0 && !busy) this._startReload();
    if (this.reloadT > 0) {
      const prev = this.reloadT;
      this.reloadT -= dt;
      const k0 = 1 - prev / this.reloadDur, k1 = 1 - this.reloadT / this.reloadDur;
      if (k0 < 0.18 && k1 >= 0.18) game.audio.reload(0);
      if (k0 < 0.55 && k1 >= 0.55) game.audio.reload(1);
      if (k0 < 0.85 && k1 >= 0.85) game.audio.reload(2);
      if (this.reloadT <= 0) {
        const need = def.mag - st.ammo;
        const take = Math.min(need, st.reserve);
        st.ammo += take;
        st.reserve -= take;
        m.ammo.set(st.ammo, false);
      }
    }

    // fire
    this.boltT -= dt;
    const pressed = input.fire && !this.triggerWas;
    this.triggerWas = input.fire;
    const interval = 60 / def.rpm;
    const canFire = player.alive && !busy && this.reloadT <= 0 && this.sprintT < 0.35 && this.boltT <= 0;
    if (input.fire && canFire && (def.auto || pressed) && game.time - this.lastShot >= interval) {
      if (st.ammo > 0) this._fire(player);
      else if (pressed) {
        game.audio.dryFire();
        if (st.reserve > 0) this._startReload();
      }
    }
    if (st.ammo === 0 && st.reserve > 0 && this.reloadT <= 0 && !busy && game.time - this.lastShot > 0.25) this._startReload();

    // recoil recovery (returns part of the kick when you stop shooting)
    if (game.time - this.lastShot > 0.12 && this.recoilAccum > 0) {
      const r = Math.min(this.recoilAccum, dt * 0.9);
      player.pitch -= r * 0.55;
      this.recoilAccum -= r;
    }
    this.bloom = Math.max(0, this.bloom - dt * 5);
    this.kick *= Math.pow(0.0005, dt);
    this.punch *= Math.pow(0.002, dt);

    // melee
    if (input.melee && !busy && player.alive) {
      this.meleeT = 0.5;
      this.meleeDone = false;
      this.reloadT = 0;
      game.audio.melee();
    }
    if (this.meleeT > 0) {
      this.meleeT -= dt;
      if (!this.meleeDone && this.meleeT < 0.36) {
        this.meleeDone = true;
        game.playerMelee();
      }
    }

    // grenade
    if (input.grenade && !busy && this.nades > 0 && player.alive) {
      this.throwT = 0.55;
      this.throwDone = false;
      this.reloadT = 0;
      game.audio.pin();
    }
    if (this.throwT > 0) {
      this.throwT -= dt;
      if (!this.throwDone && this.throwT < 0.32) {
        this.throwDone = true;
        this.nades--;
        const cam = game.camera;
        const dir = new THREE.Vector3(0, 0, -1).applyQuaternion(cam.quaternion);
        const vel = dir.multiplyScalar(17).add(new THREE.Vector3(0, 3.5, 0)).add(player.vel.clone().multiplyScalar(0.5));
        const pos = cam.position.clone().add(new THREE.Vector3(0, -0.1, 0)).addScaledVector(dir.normalize(), 0.5);
        game.throwGrenade(player, pos, vel);
      }
    }

    this._animate(dt, input, player, m);
    this._updateShells(dt);
  }

  _startReload() {
    const def = this.def;
    this.reloadDur = this.st.ammo === 0 ? def.emptyReload : def.reload;
    this.reloadT = this.reloadDur;
  }

  spread(player) {
    const def = this.def;
    let s = THREE.MathUtils.lerp(def.hip, def.ads, this.adsT) + this.bloom * (1 - this.adsT * 0.7);
    if (player.speed2d > 1 && player.onGround) s += def.move * (1 - this.adsT * 0.85) * Math.min(1, player.speed2d / 5);
    if (!player.onGround && !player.wall) s += def.air * (1 - this.adsT * 0.5);
    if (player.slideT > 0) s += 1.5;
    if (player.crouch && player.onGround) s *= 0.8;
    if (def.scope && this.adsT > 0.95) s = 0;
    return s;
  }

  _fire(player) {
    const game = this.game;
    const def = this.def;
    const st = this.st;
    const m = this.models[this.slot];
    st.ammo--;
    this.lastShot = game.time;
    m.ammo.set(st.ammo, st.ammo <= def.mag * 0.25);
    if (!def.auto) this.boltT = 60 / def.rpm;

    const cam = game.camera;
    const spreadDeg = this.spread(player);
    const a = THREE.MathUtils.degToRad(spreadDeg) * Math.sqrt(Math.random());
    const th = Math.random() * Math.PI * 2;
    const dir = new THREE.Vector3(Math.tan(a) * Math.cos(th), Math.tan(a) * Math.sin(th), -1).normalize().applyQuaternion(cam.quaternion);
    const origin = cam.position.clone();
    const muzzle = this.muzzleWorld();
    game.fireBullet(player, origin, dir, def, muzzle);

    // feel
    const adsK = 1 - this.adsT * 0.35;
    const up = THREE.MathUtils.degToRad(def.recoilUp) * (0.75 + Math.random() * 0.45) * adsK;
    player.pitch += up;
    player.yaw += THREE.MathUtils.degToRad(def.recoilSide) * (Math.random() - 0.4) * adsK;
    this.recoilAccum += up;
    this.bloom = Math.min(4, this.bloom + def.bloom);
    this.kick = Math.min(1.5, this.kick + (def.scope ? 1.5 : 0.55));
    this.punch = Math.min(0.03, this.punch + up * 0.25);
    game.audio.shot(def.sound);
    // flash
    this.flash.visible = true;
    this.flash.rotation.z = Math.random() * Math.PI;
    const fs = 0.8 + Math.random() * 0.5;
    this.flash.scale.set(fs, fs, fs * (def.scope ? 1.6 : 1));
    this.flashT = 0.045;
    this.flashLight.intensity = 3;
    this.flashLight.position.copy(m.muzzle.getWorldPosition(new THREE.Vector3()));
    game.fx.lights.flash(cam.position.clone().addScaledVector(dir, 1.2), 0xffb060, 25, 9, 0.07);
    this._ejectShell(m);
    game.onPlayerFired();
  }

  // Muzzle position projected from viewmodel space into the world camera so tracers line up
  muzzleWorld() {
    const m = this.models[this.slot];
    const p = m.muzzle.getWorldPosition(new THREE.Vector3());
    p.project(this.vmCamera);
    p.z = 0.5;
    p.unproject(this.game.camera);
    const cam = this.game.camera.position;
    const d = p.sub(cam).normalize();
    return cam.clone().addScaledVector(d, 0.9);
  }

  _ejectShell(m) {
    const s = this.shells[this.shellIdx];
    this.shellIdx = (this.shellIdx + 1) % this.shells.length;
    const p = m.group.localToWorld(m.eject.clone());
    this.vmCamera.worldToLocal(p);
    s.position.copy(p);
    s.userData.v.set(0.9 + Math.random() * 0.5, 1.1 + Math.random() * 0.6, 0.25);
    s.userData.spin.set(Math.random() * 20, Math.random() * 20, Math.random() * 20);
    s.userData.t = 0.7;
    s.visible = this.adsT < 0.9 || !this.def.scope;
  }

  _updateShells(dt) {
    for (const s of this.shells) {
      if (!s.visible) continue;
      const u = s.userData;
      u.t -= dt;
      if (u.t <= 0) { s.visible = false; continue; }
      u.v.y -= 6 * dt;
      s.position.addScaledVector(u.v, dt);
      s.rotation.x += u.spin.x * dt;
      s.rotation.y += u.spin.y * dt;
      s.rotation.z += u.spin.z * dt;
    }
  }

  _animate(dt, input, player, m) {
    const def = this.def;
    const g = m.group;
    const ads = easeInOut(this.adsT);
    const hip = _p1.fromArray(def.hip3);
    const pos = _p2.copy(hip).lerp(m.adsPos, ads);
    const rot = _e.set(0, 0, 0);

    // sway from mouse movement
    const tx = THREE.MathUtils.clamp(-input.mouseDX * 2.2, -0.08, 0.08);
    const ty = THREE.MathUtils.clamp(-input.mouseDY * 2.2, -0.08, 0.08);
    this.swayX += (tx - this.swayX) * Math.min(1, dt * 10);
    this.swayY += (ty - this.swayY) * Math.min(1, dt * 10);
    const swayK = 1 - ads * 0.8;
    rot.y += this.swayX * swayK;
    rot.x += this.swayY * swayK;
    pos.x += this.swayX * 0.1 * swayK;
    pos.y += this.swayY * 0.1 * swayK;

    // bob
    const moving = player.onGround && player.speed2d > 1 && player.slideT <= 0;
    const bobAmt = (moving || player.wall ? Math.min(1, player.speed2d / 6) : 0) * (1 - ads * 0.9) * (player.sprinting ? 1.8 : 1);
    const bt = player.bobT;
    pos.x += Math.sin(bt) * 0.012 * bobAmt;
    pos.y += -Math.abs(Math.cos(bt)) * 0.012 * bobAmt;
    rot.z += Math.sin(bt) * 0.02 * bobAmt;
    // idle breathing
    const t = this.game.time;
    pos.y += Math.sin(t * 1.6) * 0.0025 * (1 - ads);
    pos.y -= player.landDip * 0.4;

    // sprint pose
    const sp = easeInOut(this.sprintT);
    pos.x -= 0.04 * sp; pos.y -= 0.05 * sp; pos.z += 0.03 * sp;
    rot.y += 0.75 * sp; rot.x -= 0.22 * sp; rot.z += 0.35 * sp;

    // slide / wall-run tilt
    if (player.slideT > 0) rot.z += 0.25;
    if (player.wall) rot.z += player.roll * 1.5;

    // recoil kick
    pos.z += this.kick * (def.scope ? 0.06 : 0.035);
    rot.x += this.kick * (def.scope ? 0.12 : 0.05);
    pos.y += this.kick * 0.006;

    // reload animation
    if (this.reloadT > 0) {
      const k = 1 - this.reloadT / this.reloadDur;
      const tilt = Math.sin(Math.min(1, k * 1.15) * Math.PI);
      rot.z += 0.55 * tilt;
      rot.x += 0.25 * tilt;
      pos.y -= 0.05 * tilt;
      pos.x -= 0.02 * tilt;
      const magOut = k < 0.2 ? k / 0.2 : k < 0.55 ? 1 : k < 0.75 ? 1 - (k - 0.55) / 0.2 : 0;
      m.mag.position.set(m.magBase.x, m.magBase.y - magOut * 0.25, m.magBase.z + magOut * 0.05);
      m.mag.visible = !(k > 0.2 && k < 0.45);
    } else {
      m.mag.position.copy(m.magBase);
      m.mag.visible = true;
    }

    // bolt cycle for the sniper
    if (m.bolt) {
      const b = this.boltT > 0 ? Math.sin(Math.min(1, 1 - this.boltT / (60 / def.rpm)) * Math.PI) : 0;
      m.bolt.position.z = 0.02 + b * 0.06;
      rot.z += b * 0.2;
      pos.y -= b * 0.02;
    }

    // switch lower/raise
    if (this.switchT > 0) {
      const k = this.switchT > 0.25 ? 1 - (this.switchT - 0.25) / 0.25 : this.switchT / 0.25;
      pos.y -= 0.3 * k;
      rot.x -= 0.6 * k;
    }
    // melee swipe
    if (this.meleeT > 0) {
      const k = Math.sin((1 - this.meleeT / 0.5) * Math.PI);
      pos.x -= 0.18 * k; pos.z -= 0.15 * k; pos.y += 0.04 * k;
      rot.y += 0.9 * k; rot.z -= 0.8 * k;
    }
    // grenade throw lower
    if (this.throwT > 0) {
      const k = Math.sin((1 - this.throwT / 0.55) * Math.PI);
      pos.y -= 0.25 * k;
      rot.x -= 0.4 * k;
    }

    g.position.copy(pos);
    g.rotation.copy(rot);

    // viewmodel camera FOV zoom
    const vmFov = THREE.MathUtils.lerp(54, def.vmFovAds, ads);
    if (Math.abs(this.vmCamera.fov - vmFov) > 0.01) {
      this.vmCamera.fov = vmFov;
      this.vmCamera.updateProjectionMatrix();
    }

    // scope: hide gun when fully zoomed
    const scoped = def.scope && this.adsT > 0.92 && this.switchT <= 0;
    this.root.visible = !scoped && player.alive;
    this.game.hud.setScope(scoped && player.alive);

    // flash
    if (this.flashT > 0) {
      this.flashT -= dt;
      if (this.flashT <= 0) { this.flash.visible = false; this.flashLight.intensity = 0; }
    }
  }
}

function easeInOut(t) { return t * t * (3 - 2 * t); }
const _p1 = new THREE.Vector3(), _p2 = new THREE.Vector3(), _e = new THREE.Euler();

// ---------------------------------------------------------------------------
// Frag grenade (used by player and bots)
const nadeGeo = new THREE.SphereGeometry(0.07, 10, 8);
const nadeMat = new THREE.MeshStandardMaterial({ color: '#2d3a2e', roughness: 0.5, metalness: 0.6 });
const nadeLightMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(4, 0.3, 0.2) });

export class Grenade {
  constructor(game, owner, pos, vel, fuse = 2.4) {
    this.game = game;
    this.owner = owner;
    this.pos = pos.clone();
    this.vel = vel.clone();
    this.fuse = fuse;
    this.alive = true;
    this.mesh = new THREE.Group();
    const body = new THREE.Mesh(nadeGeo, nadeMat);
    body.castShadow = true;
    this.led = new THREE.Mesh(new THREE.SphereGeometry(0.025, 6, 4), nadeLightMat);
    this.led.position.y = 0.07;
    this.mesh.add(body, this.led);
    this.mesh.position.copy(this.pos);
    game.scene.add(this.mesh);
    this.bounceSound = 0;
  }

  update(dt) {
    const w = this.game.world;
    this.fuse -= dt;
    this.vel.y -= 18 * dt;
    let remaining = dt;
    for (let iter = 0; iter < 3 && remaining > 0; iter++) {
      const step = this.vel.length() * remaining;
      if (step < 1e-5) break;
      const dir = this.vel.clone().normalize();
      const hit = w.raycast(this.pos, dir, step + 0.07);
      if (hit) {
        const travel = Math.max(0, hit.dist - 0.07);
        this.pos.addScaledVector(dir, travel);
        remaining -= travel / Math.max(1e-4, this.vel.length());
        const n = hit.normal;
        const vn = this.vel.dot(n);
        this.vel.addScaledVector(n, -vn * 1.4); // restitution ~0.4
        this.vel.multiplyScalar(0.72);
        if (Math.abs(vn) > 2 && this.bounceSound <= 0) {
          this.game.audio.grenadeBounce(this.pos);
          this.bounceSound = 0.12;
        }
        if (this.vel.length() < 0.4 && n.y > 0.7) { this.vel.set(0, 0, 0); break; }
      } else {
        this.pos.addScaledVector(dir, step);
        remaining = 0;
      }
    }
    this.bounceSound -= dt;
    this.mesh.position.copy(this.pos);
    this.mesh.rotation.x += this.vel.length() * dt * 3;
    this.led.visible = Math.sin(this.fuse * (this.fuse < 1 ? 40 : 14)) > 0;
    if (this.fuse <= 0) {
      this.alive = false;
      this.game.scene.remove(this.mesh);
      this.game.explode(this.pos.clone().setY(this.pos.y + 0.1), 6.5, 150, this.owner, 'FRAG', 0.85);
    }
  }
}
