// NEONTOWN 2071 — game bootstrap and main loop.
import * as THREE from 'three';
import { EffectComposer } from '../vendor/three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from '../vendor/three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from '../vendor/three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from '../vendor/three/addons/postprocessing/OutputPass.js';
import { World } from './collision.js';
import { buildMap, BOUNDS } from './map.js';
import { buildEnvironment } from './environment.js';
import { NavGraph } from './nav.js';
import { Effects } from './effects.js';
import { Audio } from './audio.js';
import { HUD } from './hud.js';
import { Player } from './player.js';
import { WeaponSystem, Grenade } from './weapons.js';
import { Bot } from './bots.js';

const $ = (id) => document.getElementById(id);
const SCORE_LIMIT = 75;
const TIME_LIMIT = 10 * 60;

const DEFAULTS = { sens: 1, fov: 80, volume: 0.7, difficulty: 'regular', quality: 'medium', voice: true, invert: false };

function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('neontown.settings') || '{}');
    return { ...DEFAULTS, ...s };
  } catch {
    return { ...DEFAULTS };
  }
}
function saveSettings(s) {
  try { localStorage.setItem('neontown.settings', JSON.stringify(s)); } catch { /* storage unavailable */ }
}

class Game {
  constructor() {
    this.params = new URLSearchParams(location.search);
    this.testMode = this.params.has('test');
    this.settings = loadSettings();
    if (this.params.get('quality')) this.settings.quality = this.params.get('quality');
    const q = this.settings.quality;

    // ---- renderer ----
    const canvas = $('game');
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: false, powerPreference: 'high-performance', preserveDrawingBuffer: this.testMode });
    const dpr = window.devicePixelRatio || 1;
    this.pixelRatio = q === 'high' ? Math.min(dpr, 2) : q === 'low' ? Math.min(dpr, 1) * 0.75 : Math.min(dpr, 1.25);
    this.renderer.setPixelRatio(this.pixelRatio);
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = q !== 'low';
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(this.settings.fov, window.innerWidth / window.innerHeight, 0.05, 1500);
    this.camera.rotation.order = 'YXZ';
    this.vmScene = new THREE.Scene();
    this.vmCamera = new THREE.PerspectiveCamera(54, window.innerWidth / window.innerHeight, 0.01, 10);

    // ---- post-processing: world -> viewmodel -> bloom -> tone map ----
    this.composer = new EffectComposer(this.renderer);
    this.composer.setPixelRatio(this.pixelRatio);
    this.composer.setSize(window.innerWidth, window.innerHeight);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    const vmPass = new RenderPass(this.vmScene, this.vmCamera);
    vmPass.clear = false;
    vmPass.clearDepth = true;
    this.vmPass = vmPass;
    this.composer.addPass(vmPass);
    this.bloom = new UnrealBloomPass(new THREE.Vector2(window.innerWidth / 2, window.innerHeight / 2), q === 'low' ? 0.45 : 0.55, 0.4, 0.92);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    // ---- world ----
    this.world = new World();
    this.env = buildEnvironment(this.scene, this.renderer, q);
    this.map = buildMap(this.scene, this.world);
    this.nav = new NavGraph(this.world, BOUNDS, 1);
    this.fx = new Effects(this.scene, q);
    this.audio = new Audio();
    this.audio.setVolume(this.settings.volume);
    this.audio.voiceEnabled = this.settings.voice;
    this.hud = new HUD();
    this.hud.buildMinimap(this.map.minimap);
    for (const f of this.map.fires) this.fx.addFire(f);

    this.player = new Player(this);
    this.weapons = new WeaponSystem(this, this.vmScene, this.vmCamera);
    this.bots = [];
    for (let i = 0; i < 4; i++) this.bots.push(new Bot(this, 0, i));
    for (let i = 0; i < 5; i++) this.bots.push(new Bot(this, 1, i));
    this.chars = [this.player, ...this.bots];
    this.grenades = [];
    this.strikes = [];
    this.pings = new Map();
    this.markers = this._makeStrikeMarkers();

    this.time = 0;
    this.state = 'menu';
    this.scores = [0, 0];
    this.timeLeft = TIME_LIMIT;
    this.clock = new THREE.Clock();
    this.holoT = 0;
    this.menuT = 0;

    this._initInput();
    this._initMenus();
    window.addEventListener('resize', () => this._resize());
    this._resize();

    // attract mode: bots fight behind the menu
    this._resetMatch(true);
    this.player.alive = false;

    $('loading').classList.add('hidden');
    if (this.testMode) {
      window.__game = this;
      // deterministic simulation stepping for automated tests
      this.advance = (sec, step = 1 / 30) => { for (let t = 0; t < sec; t += step) this._step(step); };
      // debug viewpoint for automated screenshots
      this.debugView = (px, py, pz, tx, ty, tz, fov = 75) => {
        this.state = 'debug';
        $('menu').classList.add('hidden');
        this.hud.show(false);
        this.camera.position.set(px, py, pz);
        this.camera.lookAt(tx, ty, tz);
        this.camera.fov = fov;
        this.camera.updateProjectionMatrix();
      };
    }
    this.renderer.setAnimationLoop(() => this._frame());
  }

  // ------------------------------------------------------------------ input
  _initInput() {
    this.keys = new Set();
    this.mdx = 0; this.mdy = 0;
    this.pressed = new Set();
    this.mouse = { fire: false, ads: false };
    this.wheel = 0;
    const canvas = this.renderer.domElement;
    document.addEventListener('keydown', (e) => {
      if (e.code === 'Tab') e.preventDefault();
      if (!this.keys.has(e.code)) this.pressed.add(e.code);
      this.keys.add(e.code);
      if (e.code === 'Space' && this.state === 'playing') e.preventDefault();
    });
    document.addEventListener('keyup', (e) => { this.keys.delete(e.code); });
    document.addEventListener('mousemove', (e) => {
      if (this.locked || this.testMode) { this.mdx += e.movementX || 0; this.mdy += e.movementY || 0; }
    });
    document.addEventListener('mousedown', (e) => {
      if (this.state !== 'playing') return;
      if (!this.locked && !this.testMode) { this._lock(); return; }
      if (e.button === 0) this.mouse.fire = true;
      if (e.button === 2) this.mouse.ads = true;
    });
    document.addEventListener('mouseup', (e) => {
      if (e.button === 0) this.mouse.fire = false;
      if (e.button === 2) this.mouse.ads = false;
    });
    document.addEventListener('contextmenu', (e) => e.preventDefault());
    document.addEventListener('wheel', (e) => { if (this.state === 'playing') this.wheel += Math.sign(e.deltaY); }, { passive: true });
    document.addEventListener('pointerlockchange', () => {
      this.locked = document.pointerLockElement === canvas;
      if (!this.locked && this.state === 'playing' && !this.testMode) this.pause();
    });
    window.addEventListener('blur', () => { this.keys.clear(); this.mouse.fire = false; this.mouse.ads = false; });
  }

  _lock() {
    const c = this.renderer.domElement;
    const plain = () => {
      try {
        const p2 = c.requestPointerLock();
        if (p2 && p2.catch) p2.catch(() => { /* user can click the canvas to retry */ });
      } catch { /* ignore */ }
    };
    try {
      const p = c.requestPointerLock({ unadjustedMovement: true });
      if (p && p.catch) p.catch(plain);
    } catch {
      plain();
    }
  }

  _readInput() {
    const k = this.keys, p = this.pressed;
    const zoom = THREE.MathUtils.lerp(1, this.weapons.def.zoom, this.weapons.adsT);
    const sens = 0.0022 * this.settings.sens / Math.pow(zoom, 0.85);
    let switchTo = -1;
    if (p.has('Digit1')) switchTo = 0;
    if (p.has('Digit2')) switchTo = 1;
    if (p.has('Digit3')) switchTo = 2;
    if (this.wheel !== 0) {
      switchTo = (this.weapons.slot + (this.wheel > 0 ? 1 : 2)) % 3;
      this.wheel = 0;
    }
    const input = {
      fwd: (k.has('KeyW') || k.has('ArrowUp') ? 1 : 0) - (k.has('KeyS') || k.has('ArrowDown') ? 1 : 0),
      strafe: (k.has('KeyD') || k.has('ArrowRight') ? 1 : 0) - (k.has('KeyA') || k.has('ArrowLeft') ? 1 : 0),
      sprint: k.has('ShiftLeft') || k.has('ShiftRight'),
      jumpPressed: p.has('Space'),
      crouchPressed: p.has('KeyC'),
      reload: p.has('KeyR'),
      grenade: p.has('KeyG'),
      melee: p.has('KeyF') || p.has('KeyV'),
      fire: this.mouse.fire || (this.testMode && k.has('KeyJ')),
      ads: this.mouse.ads || (this.testMode && k.has('KeyK')),
      switchTo,
      mouseDX: this.mdx * sens,
      mouseDY: this.mdy * sens * (this.settings.invert ? -1 : 1),
      uav: p.has('Digit4'),
      orbital: p.has('Digit5'),
    };
    this.mdx = 0; this.mdy = 0;
    this.scoreboardHeld = k.has('Tab');
    if (p.has('Escape') && this.testMode && this.state === 'playing') this.pause();
    p.clear();
    return input;
  }

  // ------------------------------------------------------------------ menus
  _initMenus() {
    const s = this.settings;
    const bind = (id, key, fmt, apply) => {
      const el = $(id);
      const out = $(id.replace('set-', 'val-'));
      if (el.type === 'checkbox') el.checked = !!s[key];
      else el.value = s[key];
      if (out) out.textContent = fmt ? fmt(s[key]) : s[key];
      el.addEventListener('input', () => {
        s[key] = el.type === 'checkbox' ? el.checked : el.type === 'range' ? parseFloat(el.value) : el.value;
        if (out) out.textContent = fmt ? fmt(s[key]) : s[key];
        if (apply) apply(s[key]);
        saveSettings(s);
      });
    };
    bind('set-sens', 'sens', (v) => v.toFixed(2));
    bind('set-fov', 'fov', (v) => `${v}°`);
    bind('set-vol', 'volume', (v) => `${Math.round(v * 100)}%`, (v) => this.audio.setVolume(v));
    bind('set-diff', 'difficulty');
    bind('set-quality', 'quality');
    bind('set-voice', 'voice', null, (v) => { this.audio.voiceEnabled = v; });
    bind('set-invert', 'invert');

    const toggle = (panel, other) => {
      $(panel).classList.toggle('hidden');
      $(other).classList.add('hidden');
      this.audio.ui();
    };
    $('btn-settings').onclick = () => toggle('settings-panel', 'controls-panel');
    $('btn-controls').onclick = () => toggle('controls-panel', 'settings-panel');
    $('btn-play').onclick = () => this.startMatch();
    $('btn-resume').onclick = () => this.resume();
    $('btn-quit').onclick = () => this.toMenu();
    $('btn-again').onclick = () => this.startMatch();
    $('btn-menu').onclick = () => this.toMenu();
    if ('ontouchstart' in window && !matchMedia('(pointer:fine)').matches) {
      $('menu-note').textContent = 'This game needs a keyboard and mouse — open it on a desktop browser.';
    }
  }

  startMatch() {
    this.audio.init();
    this.audio.ui();
    $('menu').classList.add('hidden');
    $('pause').classList.add('hidden');
    $('endscreen').classList.add('hidden');
    this._resetMatch(false);
    this.state = 'playing';
    this.hud.show(true);
    if (!this.testMode) this._lock();
    this.hud.message('TEAM DEATHMATCH', `ELIMINATE THE SYNDICATE · FIRST TO ${SCORE_LIMIT}`, false, 4);
    this.audio.announce('Team deathmatch. Eliminate the Syndicate.');
  }

  pause() {
    if (this.state !== 'playing') return;
    this.state = 'paused';
    this.mouse.fire = false;
    this.mouse.ads = false;
    $('pause').classList.remove('hidden');
    if (this.audio.ctx) this.audio.ctx.suspend();
  }

  resume() {
    $('pause').classList.add('hidden');
    this.state = 'playing';
    if (this.audio.ctx) this.audio.ctx.resume();
    if (!this.testMode) this._lock();
  }

  toMenu() {
    $('pause').classList.add('hidden');
    $('endscreen').classList.add('hidden');
    $('menu').classList.remove('hidden');
    this.hud.show(false);
    this.hud.death(false);
    this.hud.setScope(false);
    this.state = 'menu';
    if (document.pointerLockElement) document.exitPointerLock();
    if (this.audio.ctx) this.audio.ctx.suspend();
    this._resetMatch(true);
    this.player.alive = false;
  }

  _resetMatch(attract) {
    this.attract = attract;
    this.scores = [0, 0];
    this.timeLeft = TIME_LIMIT;
    this.uavUntil = 0;
    this.available = [];
    this.bestStreak = 0;
    this.lastKillTimes = [];
    this.firstBlood = false;
    this.lastKiller = null;
    this.endT = 0;
    this.nuked = false;
    this.shockHit = false;
    this.skyFlash = 0;
    this.env.skyMat.uniforms.uFlash.value = 0;
    this.env.drone.visible = false;
    for (const g of this.grenades) this.scene.remove(g.mesh);
    this.grenades = [];
    this.strikes = [];
    for (const mk of this.markers) mk.visible = false;
    for (const c of this.chars) { c.kills = 0; c.deaths = 0; c.score = 0; c.streak = 0; }
    // restore explosives
    for (const ex of this.map.explosives) {
      if (ex.exploded) {
        ex.exploded = false;
        ex.box.alive = true;
        this.scene.add(ex.group);
      }
      ex.health = ex.big ? 90 : 45;
      ex.fuse = -1;
      ex.fireFx = null;
    }
    // drop temporary fires
    this.fx.emitters = this.fx.emitters.filter((e) => {
      if (e.life !== Infinity) { if (e.light) this.scene.remove(e.light); return false; }
      return true;
    });
    this.fx.nuke = null;
    this.weapons.reset();
    for (const b of this.bots) this._spawn(b, true);
    this._spawn(this.player, true);
    this.player.deathT = 0;
    this.hud.death(false);
  }

  // ------------------------------------------------------------------ spawning
  _pickSpawn(c, initial) {
    const own = this.map.spawns[c.team];
    const other = this.map.spawns[1 - c.team];
    const cands = initial ? own : [...own, ...other];
    let best = own[0], bestScore = -Infinity;
    const enemies = this.chars.filter((e) => e.alive && e.team !== c.team && e !== c);
    for (const p of cands) {
      let minD = 60;
      let seen = false;
      for (const e of enemies) {
        const d = e.pos.distanceTo(p);
        minD = Math.min(minD, d);
        if (d < 40 && this.world.los(_s1.copy(p).setY(p.y + 1.5), e.eyePos(_s2))) seen = true;
      }
      let score = Math.min(minD, 40) - (seen ? 30 : 0) + (own.includes(p) ? 6 : 0) + Math.random() * (initial ? 10 : 4);
      // avoid stacking on teammates exactly
      for (const f of this.chars) if (f !== c && f.alive && f.pos.distanceTo(p) < 1.2) score -= 20;
      if (score > bestScore) { bestScore = score; best = p; }
    }
    return best;
  }

  _spawn(c, initial = false) {
    const p = this._pickSpawn(c, initial).clone();
    p.x += (Math.random() - 0.5) * 0.6;
    p.z += (Math.random() - 0.5) * 0.6;
    const yaw = Math.atan2(p.x * 0.3, p.z); // roughly face the street
    if (c.isPlayer) {
      c.spawn(p, yaw);
      this.weapons.refill();
    } else c.spawn(p, yaw);
  }

  // ------------------------------------------------------------------ combat
  falloff(def, dist) {
    const k = THREE.MathUtils.clamp((dist - def.range[0]) / (def.range[1] - def.range[0]), 0, 1);
    return THREE.MathUtils.lerp(def.dmg[0], def.dmg[1], k);
  }

  fireBullet(shooter, origin, dir, def, muzzle) {
    const maxD = 250;
    const wh = this.world.raycast(origin, dir, maxD, true);
    let best = wh ? wh.dist : maxD;
    let hitC = null, head = false;
    for (const c of this.chars) {
      if (c === shooter || !c.alive || c.team === shooter.team) continue;
      const h = c.rayHit(origin, dir, best);
      if (h && h.dist < best) { best = h.dist; hitC = c; head = h.head; }
    }
    const end = origin.clone().addScaledVector(dir, best);
    if (hitC) {
      const dmg = this.falloff(def, best) * (head ? def.head : 1) * (shooter.dmgMul || 1);
      const killed = this.applyDamage(hitC, dmg, shooter, { head, weapon: def.name, dist: best });
      this.fx.impact(end, dir.clone().negate(), 'robot');
      if (shooter.isPlayer) {
        this.hud.hit(killed, head);
        this.audio.hitmarker(killed, head);
      }
    } else if (wh) {
      this.fx.impact(end, wh.normal, wh.box.surface);
      this.audio.bulletImpact(end, wh.box.surface);
      if (wh.box.ref) this.damageExplosive(wh.box.ref, this.falloff(def, best), shooter);
    }
    this.fx.tracer(muzzle || origin, end, def.tracer, def.sound === 'sniper' ? 900 : 420);
    // near-miss whiz for the player
    const P = this.player;
    if (!shooter.isPlayer && P.alive && hitC !== P) {
      const eye = P.eyePos(_s1);
      const t = Math.max(0, Math.min(best, _s2.subVectors(eye, origin).dot(dir)));
      const closest = _s2.copy(origin).addScaledVector(dir, t);
      if (closest.distanceTo(eye) < 1.4 && t < best - 0.5) this.audio.whiz(closest);
    }
  }

  applyDamage(target, dmg, attacker, info = {}) {
    if (!target.alive || this.state === 'ending') return false;
    target.health -= dmg;
    if (target.isPlayer) {
      target.lastHurt = this.time;
      if (attacker && attacker !== target) {
        const dx = attacker.pos.x - target.pos.x, dz = attacker.pos.z - target.pos.z;
        const f = target.forward(_s1), r = target.right(_s2);
        this.hud.damageFrom(Math.atan2(dx * r.x + dz * r.z, dx * f.x + dz * f.z));
      }
      this.audio.hurt();
      this.camPunch = (this.camPunch || 0) + 0.015;
    } else if (target.onDamaged) {
      target.onDamaged(attacker);
    }
    if (target.health <= 0) {
      this.killCharacter(target, attacker, info.weapon, info.head, info);
      return true;
    }
    return false;
  }

  killCharacter(victim, killer, weapon, head = false, info = {}) {
    if (!victim.alive) return;
    victim.alive = false;
    victim.health = 0;
    victim.deaths++;
    victim.respawnAt = this.time + (victim.isPlayer ? 3.5 : 4.5);
    const validKiller = killer && killer !== victim;
    if (!victim.isPlayer) {
      let fromDir = 0;
      if (killer) {
        const fx = -Math.sin(victim.yaw), fz = -Math.cos(victim.yaw);
        fromDir = (victim.pos.x - killer.pos.x) * fx + (victim.pos.z - killer.pos.z) * fz;
      }
      victim.die(fromDir);
      this.fx.robotDeath(victim.chestPos(new THREE.Vector3()));
    } else {
      this.deathCam = { killer: validKiller ? killer : null, t: 0 };
      this.mouse.fire = false;
      this.hud.setScope(false);
    }
    if (validKiller && killer.team !== victim.team) {
      killer.kills++;
      killer.streak++;
      killer.score += 100 + (head ? 50 : 0);
      if (!this.attract) this.scores[killer.team]++;
    }
    const wasStreak = victim.streak;
    victim.streak = 0;
    if (!this.attract) {
      this.hud.killfeed(validKiller ? killer : null, victim, weapon || 'KILLED', head, this.player.team, this.player);
      if (killer && killer.isPlayer && validKiller) this._playerKill(victim, head, info, wasStreak);
      if (victim.isPlayer) this.lastKiller = validKiller ? killer : null;
      if (!this.firstBlood && validKiller) {
        this.firstBlood = true;
        if (killer.isPlayer) { this.hud.medal('FIRST BLOOD', '+100'); killer.score += 100; }
      }
      if (this.scores[0] >= SCORE_LIMIT || this.scores[1] >= SCORE_LIMIT) this.endMatch();
    }
  }

  _playerKill(victim, head, info, victimStreak) {
    const P = this.player;
    this.hud.popup(`+${100 + (head ? 50 : 0)}`);
    this.bestStreak = Math.max(this.bestStreak, P.streak);
    this.lastKillTimes = this.lastKillTimes.filter((t) => this.time - t < 4.5);
    this.lastKillTimes.push(this.time);
    const n = this.lastKillTimes.length;
    const multi = ['', '', 'DOUBLE KILL', 'TRIPLE KILL', 'FURY KILL', 'FRENZY KILL', 'SUPER KILL'];
    if (n >= 2) { this.hud.medal(multi[Math.min(n, 6)], `+${50 * n}`); P.score += 50 * n; this.audio.ui('medal'); }
    if (head) this.hud.medal('HEADSHOT', '+50');
    if (info.dist > 35) { this.hud.medal('LONGSHOT', '+50'); P.score += 50; }
    if (this.lastKiller === victim) { this.hud.medal('REVENGE', '+50'); P.score += 50; this.lastKiller = null; }
    if (victimStreak >= 3) { this.hud.medal('BUZZKILL', '+50'); P.score += 50; }
    if (P.streak === 3 && !this.available.includes('uav')) {
      this.available.push('uav');
      this.hud.message('UAV READY', 'PRESS [4]', false, 2.5);
      this.audio.ui('streak');
      this.audio.announce('U A V ready');
    }
    if (P.streak === 5 && !this.available.includes('orbital')) {
      this.available.push('orbital');
      this.hud.message('ORBITAL STRIKE READY', 'PRESS [5]', false, 2.5);
      this.audio.ui('streak');
      this.audio.announce('Orbital strike ready');
    }
    if (P.streak === 10) this.hud.medal('UNSTOPPABLE', '10 KILL STREAK');
  }

  playerMelee() {
    const P = this.player;
    const eye = P.eyePos(new THREE.Vector3());
    const f = new THREE.Vector3(0, 0, -1).applyQuaternion(this.camera.quaternion);
    for (const c of this.chars) {
      if (!c.alive || c.team === P.team) continue;
      const to = c.chestPos(new THREE.Vector3()).sub(eye);
      const d = to.length();
      if (d < 2.5 && to.normalize().dot(f) > 0.6 && this.world.los(eye, c.chestPos(new THREE.Vector3()))) {
        this.applyDamage(c, 200, P, { weapon: 'MELEE' });
        this.hud.hit(true, false);
        this.audio.hitmarker(true);
        this.fx.impact(c.chestPos(new THREE.Vector3()), f.clone().negate(), 'robot');
        return;
      }
    }
    const hit = this.world.raycast(eye, f, 2);
    if (hit) this.fx.impact(hit.point, hit.normal, hit.box.surface);
  }

  throwGrenade(owner, pos, vel) {
    this.grenades.push(new Grenade(this, owner, pos, vel));
  }

  explode(pos, radius, damage, owner, weapon, scale = 1) {
    this.fx.explosion(pos, scale);
    this.audio.explosion(pos, Math.min(1.5, scale));
    const up = _s1.copy(pos).setY(pos.y + 0.3);
    for (const c of this.chars) {
      if (!c.alive) continue;
      if (owner && c !== owner && c.team === owner.team) continue;
      const chest = c.chestPos(_s2);
      const d = chest.distanceTo(pos);
      if (d > radius) continue;
      if (!this.world.los(up, chest) && !this.world.los(up, c.eyePos(_s3))) continue;
      const k = d < radius * 0.3 ? 1 : 1 - (d - radius * 0.3) / (radius * 0.7);
      this.applyDamage(c, damage * k, owner, { weapon, dist: d });
    }
    for (const ex of this.map.explosives) {
      if (ex.exploded || ex.fuse >= 0) continue;
      if (ex.pos.distanceTo(pos) < radius * 0.8) {
        ex.fuse = 0.2 + Math.random() * 0.35;
        ex.lastAttacker = owner;
      }
    }
    if (this.player.alive || this.state === 'ending') {
      const d = this.camera.position.distanceTo(pos);
      this.fx.shake = Math.min(1.2, this.fx.shake + Math.max(0, 1 - d / (radius * 4)) * 0.9 * scale);
    }
    // bots hear explosions
    for (const b of this.bots) if (b.alive && b.pos.distanceTo(pos) < 30 && owner && owner.team !== b.team) b.hear(owner.pos);
  }

  damageExplosive(ex, dmg, attacker) {
    if (ex.exploded) return;
    ex.health -= dmg;
    ex.lastAttacker = attacker;
    if (ex.health <= 0 && ex.fuse < 0) {
      ex.fuse = 0.9;
      ex.fireFx = this.fx.addFire({ pos: ex.pos.clone().setY(ex.pos.y * 2 + 0.05), radius: 0.2, rate: 60, scale: 0.6, life: 1.0 });
    }
  }

  _updateExplosives(dt) {
    for (const ex of this.map.explosives) {
      if (ex.exploded) continue;
      const pulse = ex.fuse >= 0 ? 0.4 + Math.random() * 1.6 : 0.8 + 0.2 * Math.sin(this.time * 3 + ex.pos.x);
      ex.glowMat.color.setRGB(...(ex.big ? [0.4 * pulse, 2.2 * pulse, 3.2 * pulse] : [3.2 * pulse, 0.9 * pulse, 0.1 * pulse]));
      if (ex.fuse >= 0) {
        ex.fuse -= dt;
        if (ex.fuse <= 0) {
          ex.exploded = true;
          ex.box.alive = false;
          this.scene.remove(ex.group);
          const p = ex.pos.clone().setY(0.4);
          this.explode(p, ex.radius, ex.damage, ex.lastAttacker || null, ex.big ? 'FUSION CORE' : 'FUEL CELL', ex.big ? 1.7 : 1.15);
          this.fx.addFire({ pos: p.clone().setY(0.05), radius: ex.big ? 1.6 : 1.0, rate: 55, scale: ex.big ? 1.3 : 1, life: ex.big ? 16 : 11, damageRadius: ex.big ? 2 : 1.3, owner: ex.lastAttacker });
        }
      }
    }
  }

  onPlayerFired() {
    for (const b of this.bots) if (b.alive && b.team !== this.player.team && b.pos.distanceTo(this.player.pos) < 35) b.hear(this.player.pos);
  }

  onBotFired(bot) {
    if (bot.team !== this.player.team) this.pings.set(bot, this.time + 1.4);
    for (const b of this.bots) if (b.alive && b.team !== bot.team && b !== bot && b.pos.distanceTo(bot.pos) < 30) b.hear(bot.pos);
  }

  // ------------------------------------------------------------------ scorestreaks
  _useStreak(key) {
    const i = this.available.indexOf(key);
    if (i < 0) return;
    this.available.splice(i, 1);
    if (key === 'uav') {
      this.uavUntil = this.time + 30;
      this.env.drone.visible = true;
      this.hud.message('UAV ONLINE', 'ENEMY POSITIONS REVEALED', false, 2.5);
      this.audio.ui('streak');
      this.audio.announce('U A V online');
    } else if (key === 'orbital') {
      const enemies = this.chars.filter((c) => c.alive && c.team !== this.player.team);
      const targets = [];
      for (let k = 0; k < 3; k++) {
        const e = enemies[k % Math.max(1, enemies.length)];
        const base = e ? e.pos.clone() : new THREE.Vector3((Math.random() - 0.5) * 40, 0, 15 + Math.random() * 10);
        base.x += (Math.random() - 0.5) * 3;
        base.z += (Math.random() - 0.5) * 3;
        base.y = Math.max(0.1, this.world.groundHeight(base.x, base.z, (e ? e.pos.y : 0) + 0.5));
        targets.push(base);
      }
      targets.forEach((p, k) => this.strikes.push({ pos: p, t: -(1.6 + k * 0.55), marker: this.markers[k] }));
      this.hud.message('ORBITAL STRIKE INBOUND', '', true, 2.5);
      this.audio.ui('warning');
      this.audio.announce('Orbital strike inbound');
    }
  }

  _makeStrikeMarkers() {
    const out = [];
    for (let i = 0; i < 3; i++) {
      const m = new THREE.Mesh(new THREE.RingGeometry(2.2, 2.6, 40), new THREE.MeshBasicMaterial({ color: new THREE.Color(4, 0.3, 0.2), transparent: true, depthWrite: false, side: THREE.DoubleSide }));
      m.rotation.x = -Math.PI / 2;
      m.visible = false;
      this.scene.add(m);
      out.push(m);
    }
    return out;
  }

  _updateStrikes(dt) {
    this.strikes = this.strikes.filter((s) => {
      s.t += dt;
      s.marker.visible = s.t < 0;
      s.marker.position.set(s.pos.x, s.pos.y + 0.06, s.pos.z);
      s.marker.scale.setScalar(1 + 0.15 * Math.sin(this.time * 20));
      if (s.t > -0.35 && !s.beamed) { s.beamed = true; this.fx.beam(s.pos); }
      if (s.t >= 0) {
        s.marker.visible = false;
        this.explode(s.pos.clone().setY(s.pos.y + 0.4), 8, 260, this.player, 'ORBITAL STRIKE', 1.6);
        return false;
      }
      return true;
    });
  }

  // ------------------------------------------------------------------ match end
  endMatch() {
    if (this.state !== 'playing') return;
    this.state = 'ending';
    this.endT = 0;
    this.mouse.fire = false;
    this.endFrom = { pos: this.camera.position.clone(), quat: this.camera.quaternion.clone() };
    this.hud.show(false);
    this.hud.death(false);
    this.hud.setScope(false);
    const win = this.scores[0] > this.scores[1];
    this.hud.message(win ? 'VICTORY' : this.scores[0] === this.scores[1] ? 'DRAW' : 'DEFEAT', '', !win, 3);
  }

  _updateEnding(dt) {
    this.endT += dt;
    const t = this.endT;
    const k = Math.min(1, t / 1.6);
    const e = k * k * (3 - 2 * k);
    const target = _s1.set(0, 26, 44);
    this.camera.position.copy(this.endFrom.pos).lerp(target, e);
    const look = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().lookAt(target, new THREE.Vector3(0, 6, -40), new THREE.Vector3(0, 1, 0)));
    this.camera.quaternion.copy(this.endFrom.quat).slerp(look, e);
    if (this.camera.fov !== this.settings.fov) { this.camera.fov = this.settings.fov; this.camera.updateProjectionMatrix(); }
    if (t > 1.8 && !this.nuked) {
      this.nuked = true;
      this.fx.detonation(new THREE.Vector3(0, 0, -115));
      this.hud.flash(1.4);
      this.audio.nuke();
      this.fx.shake = 1.0;
      this.skyFlash = 1;
    }
    if (t > 4.6 && !this.shockHit) {
      this.shockHit = true;
      this.fx.shake = 1.4;
      this.hud.flash(0.5);
    }
    if (t > 7.5) {
      this.state = 'ended';
      this._showEnd();
    }
  }

  _showEnd() {
    if (document.pointerLockElement) document.exitPointerLock();
    const [a, b] = this.scores;
    const res = $('end-result');
    res.textContent = a > b ? 'VICTORY' : a < b ? 'DEFEAT' : 'DRAW';
    res.className = a >= b ? 'win' : 'lose';
    $('end-score').innerHTML = `<span style="color:var(--cyan)">${a}</span> — <span style="color:var(--orange)">${b}</span>`;
    const P = this.player;
    $('end-stats').textContent = `KILLS ${P.kills} · DEATHS ${P.deaths} · SCORE ${P.score} · BEST STREAK ${this.bestStreak}`;
    $('endscreen').classList.remove('hidden');
  }

  // ------------------------------------------------------------------ main loop
  _frame() {
    const dt = Math.min(0.05, this.clock.getDelta());
    this._step(dt);
    this._render(dt);
  }

  _step(dt) {
    if (this.state === 'playing') this._updatePlaying(dt);
    else if (this.state === 'menu') this._updateMenu(dt);
    else if (this.state === 'debug') { this.time += dt; this._updateWorld(dt); }
    else if (this.state === 'ending' || this.state === 'ended') {
      this.time += dt;
      for (const b of this.bots) b.update(dt, true);
      if (this.state === 'ending') this._updateEnding(dt);
      this._updateWorld(dt);
    }
  }

  _updateMenu(dt) {
    this.time += dt;
    this.menuT += dt;
    // attract-mode firefight behind the menu
    for (const b of this.bots) {
      b.update(dt, false);
      if (!b.alive && this.time > b.respawnAt) this._spawn(b);
    }
    this._separate();
    this._updateWorld(dt);
    const a = this.menuT * 0.06;
    this.camera.position.set(Math.sin(a) * 30, 11 + Math.sin(this.menuT * 0.2) * 2, Math.cos(a) * 30);
    this.camera.lookAt(0, 2, 0);
    if (this.camera.fov !== 70) { this.camera.fov = 70; this.camera.updateProjectionMatrix(); }
  }

  _updatePlaying(dt) {
    this.time += dt;
    this.timeLeft -= dt;
    if (this.timeLeft <= 0) { this.timeLeft = 0; this.endMatch(); }
    const input = this._readInput();
    const P = this.player;

    if (P.alive) {
      if (this.deathCam) { this.deathCam = null; this.hud.death(false); }
      P.update(dt, input, this.weapons);
      if (input.uav) this._useStreak('uav');
      if (input.orbital) this._useStreak('orbital');
    } else if (this.time > P.respawnAt) {
      this._spawn(P);
      this.deathCam = null;
      this.hud.death(false);
    }
    this.weapons.update(dt, P.alive ? input : { ...input, fire: false, ads: false, reload: false, grenade: false, melee: false, switchTo: -1 }, P);

    for (const b of this.bots) {
      b.update(dt, false);
      if (!b.alive && this.time > b.respawnAt) this._spawn(b);
    }
    this._separate();
    this._updateWorld(dt);

    // fire damage + proximity audio
    let fireProx = 0;
    for (const e of this.fx.emitters) {
      const dCam = this.camera.position.distanceTo(e.pos);
      fireProx = Math.max(fireProx, 1 - dCam / 14);
      if (!e.damageRadius) continue;
      for (const c of this.chars) {
        if (!c.alive) continue;
        const dx = c.pos.x - e.pos.x, dz = c.pos.z - e.pos.z;
        if (dx * dx + dz * dz < e.damageRadius * e.damageRadius && c.pos.y < e.pos.y + 1.2 && c.pos.y + c.height > e.pos.y - 0.3) {
          c.burnT = 0.4;
          const owner = e.owner && e.owner.team !== c.team ? e.owner : null;
          this.applyDamage(c, 38 * dt, owner, { weapon: 'FIRE' });
        }
      }
    }
    this.audio.setFireProximity(Math.max(0, fireProx));

    // grenade warning
    let warn = null, wd = 8;
    for (const g of this.grenades) {
      const d = g.pos.distanceTo(P.pos);
      if (d < wd && P.alive) {
        wd = d;
        const dx = g.pos.x - P.pos.x, dz = g.pos.z - P.pos.z;
        const f = P.forward(_s1), r = P.right(_s2);
        warn = Math.atan2(dx * r.x + dz * r.z, dx * f.x + dz * f.z);
      }
    }
    this.hud.grenadeWarning(warn);

    // HUD
    this.hud.setScores(this.scores[0], this.scores[1], SCORE_LIMIT, this.timeLeft);
    this.hud.setPlayer(P);
    this.hud.setWeapon(this.weapons.def, this.weapons.st, this.weapons.nades, this.weapons.slot, this.weapons.reloadT > 0);
    const uav = this.time < this.uavUntil;
    if (!uav && this.env.drone.visible) this.env.drone.visible = false;
    this.hud.setStreaks(P.streak, this.available, uav);
    this.hud.scoreboard(this.scoreboardHeld, this.chars, P, SCORE_LIMIT);
    if (!P.alive) {
      this.hud.death(true, this.deathCam && this.deathCam.killer ? this.deathCam.killer.name : null, null, P.respawnAt - this.time);
    }
    // crosshair + enemy name under crosshair
    let onEnemy = null;
    if (P.alive) {
      const dir = _s1.set(0, 0, -1).applyQuaternion(this.camera.quaternion);
      const wh = this.world.raycast(this.camera.position, dir, 120, true);
      const maxD = wh ? wh.dist : 120;
      for (const c of this.chars) {
        if (c === P || !c.alive) continue;
        const h = c.rayHit(this.camera.position, dir, maxD);
        if (h) { onEnemy = c; break; }
      }
    }
    const spread = this.weapons.spread(P);
    const pxPerRad = (this.renderer.domElement.clientHeight / 2) / Math.tan(THREE.MathUtils.degToRad(this.camera.fov) / 2);
    const scoped = this.weapons.def.scope && this.weapons.adsT > 0.9;
    this.hud.setCrosshair(Math.tan(THREE.MathUtils.degToRad(spread)) * pxPerRad, P.alive && this.weapons.adsT < 0.5 && !P.sprinting && !scoped, onEnemy && onEnemy.team !== P.team);
    this.hud.setTargetName(onEnemy && onEnemy.team !== P.team ? onEnemy.name : '');
    this.hud.update(dt);
    this.hud.drawMinimap(P, this.chars, uav, this.pings, this.time);
  }

  _separate() {
    const cs = this.chars;
    for (let i = 0; i < cs.length; i++) {
      const a = cs[i];
      if (!a.alive) continue;
      for (let j = i + 1; j < cs.length; j++) {
        const b = cs[j];
        if (!b.alive || Math.abs(a.pos.y - b.pos.y) > 1.5) continue;
        const dx = b.pos.x - a.pos.x, dz = b.pos.z - a.pos.z;
        const d2 = dx * dx + dz * dz;
        if (d2 < 0.49 && d2 > 1e-6) {
          const d = Math.sqrt(d2), push = (0.7 - d) * 0.5;
          const nx = dx / d, nz = dz / d;
          if (!this.world.overlaps(a.pos.x - nx * push - 0.35, a.pos.y + 0.1, a.pos.z - nz * push - 0.35, a.pos.x - nx * push + 0.35, a.pos.y + 1.6, a.pos.z - nz * push + 0.35)) {
            a.pos.x -= nx * push; a.pos.z -= nz * push;
          }
          if (!this.world.overlaps(b.pos.x + nx * push - 0.35, b.pos.y + 0.1, b.pos.z + nz * push - 0.35, b.pos.x + nx * push + 0.35, b.pos.y + 1.6, b.pos.z + nz * push + 0.35)) {
            b.pos.x += nx * push; b.pos.z += nz * push;
          }
        }
      }
    }
  }

  _updateWorld(dt) {
    this.grenades = this.grenades.filter((g) => { g.update(dt); return g.alive; });
    this._updateExplosives(dt);
    this._updateStrikes(dt);
    this.fx.update(dt, this.camera.position);
    this.env.update(dt, this.time);
    this.holoT += dt;
    this.map.population = this.chars.filter((c) => c.alive).length;
    if (this.holoT > 0.15) {
      this.holoT = 0;
      for (const h of this.map.holos) h.draw(this.time);
      for (const h of this.map.holos) if (h.mesh) h.mesh.material.opacity = 0.85 + Math.random() * 0.15;
    }
    if (this.skyFlash > 0) {
      this.skyFlash = Math.max(0, this.skyFlash - dt * 0.35);
      this.env.skyMat.uniforms.uFlash.value = this.skyFlash * this.skyFlash;
    }
  }

  _render(dt) {
    const P = this.player;
    if (this.state === 'playing' || this.state === 'paused') {
      if (P.alive) {
        const def = this.weapons.def;
        const zoom = THREE.MathUtils.lerp(1, def.zoom, easeInOut(this.weapons.adsT));
        const fov = THREE.MathUtils.radToDeg(2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(this.settings.fov) / 2) / zoom));
        if (Math.abs(this.camera.fov - fov) > 0.01) { this.camera.fov = fov; this.camera.updateProjectionMatrix(); }
        P.eyePos(this.camera.position);
        const bob = P.onGround && P.speed2d > 1 && P.slideT <= 0 ? Math.sin(P.bobT * 2) * 0.025 * Math.min(1, P.speed2d / 6) * (1 - this.weapons.adsT * 0.8) : 0;
        this.camera.position.y += bob - P.landDip;
        this.camPunch = (this.camPunch || 0) * Math.pow(0.001, dt);
        const sh = this.fx.shake * this.fx.shake;
        const t = this.time;
        this.camera.rotation.set(
          P.pitch + this.weapons.punch + this.camPunch + sh * 0.04 * Math.sin(t * 43),
          P.yaw + sh * 0.04 * Math.sin(t * 37 + 1),
          P.roll + sh * 0.03 * Math.sin(t * 29 + 2),
        );
      } else if (this.deathCam) {
        // death cam: rise up and look at the killer
        this.deathCam.t += dt;
        const k = Math.min(1, this.deathCam.t / 1.2);
        const base = P.pos.clone().setY(P.pos.y + 0.4 + k * 3.5);
        this.camera.position.lerp(base, Math.min(1, dt * 3));
        const look = this.deathCam.killer && this.deathCam.killer.alive ? this.deathCam.killer.chestPos(new THREE.Vector3()) : P.pos.clone();
        const m = new THREE.Matrix4().lookAt(this.camera.position, look, new THREE.Vector3(0, 1, 0));
        this.camera.quaternion.slerp(new THREE.Quaternion().setFromRotationMatrix(m), Math.min(1, dt * 4));
      }
    } else if (this.state === 'ending' || this.state === 'ended') {
      const sh = this.fx.shake * this.fx.shake;
      this.camera.rotation.z += sh * 0.02 * Math.sin(this.time * 40);
    }
    this.vmCamera.position.copy(this.camera.position);
    this.vmCamera.quaternion.copy(this.camera.quaternion);
    this.vmCamera.updateMatrixWorld();
    this.vmPass.enabled = (this.state === 'playing' || this.state === 'paused') && P.alive;
    this.fx.setCamera(this.camera, this.renderer.domElement.height);
    this.audio.listener = { x: this.camera.position.x, y: this.camera.position.y, z: this.camera.position.z, yaw: this.state === 'playing' ? P.yaw : 0 };
    this.composer.render(dt);
  }

  _resize() {
    const w = window.innerWidth, h = window.innerHeight;
    this.renderer.setSize(w, h);
    this.composer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.vmCamera.aspect = w / h;
    this.vmCamera.updateProjectionMatrix();
    this.bloom.setSize(w / 2, h / 2);
  }
}

function easeInOut(t) { return t * t * (3 - 2 * t); }
const _s1 = new THREE.Vector3(), _s2 = new THREE.Vector3(), _s3 = new THREE.Vector3();

function boot() {
  try {
    new Game();
  } catch (e) {
    console.error(e);
    const sub = document.getElementById('ld-sub');
    if (sub) sub.textContent = `Failed to start: ${e.message}. A WebGL2-capable browser is required.`;
  }
}
// Let the loading screen paint before the heavy generation work
requestAnimationFrame(() => setTimeout(boot, 30));
