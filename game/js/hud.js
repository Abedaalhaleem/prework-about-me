// DOM heads-up display + rotating minimap.
import { BOUNDS } from './map.js';

const $ = (id) => document.getElementById(id);

export class HUD {
  constructor() {
    this.el = {
      hud: $('hud'), killfeed: $('killfeed'), crosshair: $('crosshair'), hitmarker: $('hitmarker'),
      targetName: $('target-name'), popups: $('popups'), medals: $('medals'), centerMsg: $('center-msg'),
      dmg: $('dmg-indicators'), grenade: $('grenade-indicator'), interact: $('interact'),
      scoreAlly: $('score-ally'), scoreEnemy: $('score-enemy'), barAlly: $('bar-ally'), barEnemy: $('bar-enemy'), timer: $('timer'),
      psScore: $('ps-score'), psStreak: $('ps-streak'), thrust: $('thrust-fill'), health: $('health-fill'),
      weaponName: $('weapon-name'), weaponType: $('weapon-type'), ammoMag: $('ammo-mag'), ammoRes: $('ammo-res'),
      nades: $('nade-count'), slots: document.querySelectorAll('#weapon-slots span'), reloadHint: $('reload-hint'),
      vignette: $('health-vignette'), burn: $('burn-overlay'), scope: $('scope-overlay'), flash: $('flash-overlay'),
      scoreboard: $('scoreboard'), sbAlly: $('sb-ally'), sbEnemy: $('sb-enemy'), sbLimit: $('sb-limit'),
      streakUav: $('streak-uav'), streakOrb: $('streak-orbital'), mmUav: $('mm-uav'),
      death: $('death-screen'), dsKiller: $('ds-killer'), dsWeapon: $('ds-weapon'), dsTimer: $('ds-timer'),
    };
    this.mm = $('minimap');
    this.mmCtx = this.mm.getContext('2d');
    this.hitT = 0;
    this.msgT = 0;
    this.flashV = 0;
    this._last = {};
  }

  show(v) { this.el.hud.classList.toggle('hidden', !v); }

  // Pre-render the static top-down map once
  buildMinimap(rects) {
    const S = 4; // px per meter
    const W = BOUNDS.x * 2 * S, H = BOUNDS.z * 2 * S;
    const c = document.createElement('canvas');
    c.width = W; c.height = H;
    const x = c.getContext('2d');
    x.fillStyle = '#15241c';
    x.fillRect(0, 0, W, H);
    // street + sidewalks
    x.fillStyle = '#2a2d33';
    x.fillRect(0, (BOUNDS.z - 5.5) * S, W, 11 * S);
    x.fillStyle = '#4a4e56';
    x.fillRect(0, (BOUNDS.z - 7.5) * S, W, 2 * S);
    x.fillRect(0, (BOUNDS.z + 5.5) * S, W, 2 * S);
    x.strokeStyle = 'rgba(60,232,255,0.4)';
    x.setLineDash([8, 8]);
    x.beginPath(); x.moveTo(0, BOUNDS.z * S); x.lineTo(W, BOUNDS.z * S); x.stroke();
    x.setLineDash([]);
    const sorted = [...rects].sort((a, b) => a.h - b.h);
    for (const r of sorted) {
      const px = (r.x0 + BOUNDS.x) * S, pz = (r.z0 + BOUNDS.z) * S;
      const w = (r.x1 - r.x0) * S, h = (r.z1 - r.z0) * S;
      const l = Math.min(1, r.h / 6);
      if (r.floor) { x.fillStyle = 'rgba(120,140,160,0.25)'; }
      else x.fillStyle = `rgb(${Math.floor(70 + l * 120)},${Math.floor(80 + l * 125)},${Math.floor(95 + l * 125)})`;
      x.fillRect(px, pz, Math.max(1.5, w), Math.max(1.5, h));
    }
    this.mmStatic = c;
    this.mmScale = S;
  }

  drawMinimap(player, chars, uavActive, pings, t) {
    const ctx = this.mmCtx, W = this.mm.width, H = this.mm.height;
    const S = this.mmScale;
    const zoom = 1.25;
    ctx.save();
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#081018';
    ctx.fillRect(0, 0, W, H);
    ctx.translate(W / 2, H / 2);
    ctx.rotate(player.yaw);
    ctx.scale(zoom, zoom);
    ctx.translate(-(player.pos.x + BOUNDS.x) * S, -(player.pos.z + BOUNDS.z) * S);
    ctx.drawImage(this.mmStatic, 0, 0);
    const dot = (p, color, r = 4, glow = true) => {
      ctx.fillStyle = color;
      if (glow) { ctx.shadowColor = color; ctx.shadowBlur = 8; }
      ctx.beginPath();
      ctx.arc((p.x + BOUNDS.x) * S, (p.z + BOUNDS.z) * S, r / zoom * 1.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    };
    for (const c of chars) {
      if (!c.alive || c === player) continue;
      if (c.team === player.team) dot(c.pos, '#3ce8ff', 4);
      else if (uavActive || (pings.get(c) || 0) > t) dot(c.pos, '#ff3b30', 4.5);
    }
    ctx.restore();
    // UAV sweep line
    if (uavActive) {
      const a = (t * 2) % (Math.PI * 2);
      const g = ctx.createConicGradient ? ctx.createConicGradient(a, W / 2, H / 2) : null;
      if (g) {
        g.addColorStop(0, 'rgba(255,59,48,0.25)');
        g.addColorStop(0.15, 'rgba(255,59,48,0)');
        g.addColorStop(1, 'rgba(255,59,48,0)');
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, W, H);
      }
    }
    // player arrow (always pointing up)
    ctx.save();
    ctx.translate(W / 2, H / 2);
    ctx.fillStyle = '#ffe86b';
    ctx.shadowColor = '#ffe86b';
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.moveTo(0, -9); ctx.lineTo(6, 7); ctx.lineTo(0, 3); ctx.lineTo(-6, 7);
    ctx.closePath();
    ctx.fill();
    // view cone
    ctx.shadowBlur = 0;
    ctx.fillStyle = 'rgba(255,232,107,0.08)';
    ctx.beginPath();
    ctx.moveTo(0, 0); ctx.lineTo(-60, -110); ctx.lineTo(60, -110);
    ctx.fill();
    ctx.restore();
    // compass N
    ctx.save();
    ctx.translate(W / 2, H / 2);
    ctx.rotate(player.yaw);
    ctx.fillStyle = 'rgba(255,255,255,0.8)';
    ctx.font = '700 12px Orbitron, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('N', 0, -H / 2 + 16);
    ctx.restore();
  }

  setScores(ally, enemy, limit, timeLeft) {
    this._set('scoreAlly', ally);
    this._set('scoreEnemy', enemy);
    this.el.barAlly.style.width = `${Math.min(100, (ally / limit) * 100)}%`;
    this.el.barEnemy.style.width = `${Math.min(100, (enemy / limit) * 100)}%`;
    const tl = Math.max(0, Math.ceil(timeLeft));
    this._set('timer', `${String(Math.floor(tl / 60)).padStart(2, '0')}:${String(tl % 60).padStart(2, '0')}`);
  }

  _set(key, val) {
    if (this._last[key] === val) return;
    this._last[key] = val;
    this.el[key].textContent = val;
  }

  setPlayer(p) {
    this._set('psScore', p.score);
    this._set('psStreak', p.streak);
    this.el.thrust.style.width = `${Math.round(p.thrustFuel * 100)}%`;
    this.el.health.style.width = `${Math.max(0, p.health)}%`;
    this.el.health.classList.toggle('low', p.health < 40);
    const dmg = 1 - p.health / 100;
    this.el.vignette.style.opacity = Math.min(1, dmg * dmg * 1.6).toFixed(3);
    this.el.burn.style.opacity = Math.min(1, p.burnT * 2).toFixed(3);
  }

  setWeapon(def, st, nades, slot, reloading) {
    this._set('weaponName', def.name);
    this._set('weaponType', def.type);
    this._set('ammoMag', st.ammo);
    this._set('ammoRes', st.reserve);
    this._set('nades', nades);
    this.el.ammoMag.classList.toggle('low', st.ammo <= Math.ceil(def.mag * 0.25));
    this.el.slots.forEach((s, i) => s.classList.toggle('on', i === slot));
    const needReload = st.ammo <= Math.ceil(def.mag * 0.25) && st.reserve > 0 && !reloading;
    this.el.reloadHint.classList.toggle('hidden', !needReload);
    if (st.ammo === 0 && st.reserve === 0) {
      this.el.reloadHint.classList.remove('hidden');
      this.el.reloadHint.innerHTML = 'NO AMMO';
    } else if (this.el.reloadHint.innerHTML !== 'PRESS <b>R</b> TO RELOAD') {
      this.el.reloadHint.innerHTML = 'PRESS <b>R</b> TO RELOAD';
    }
  }

  setCrosshair(spreadPx, visible, onEnemy) {
    const ch = this.el.crosshair;
    ch.style.display = visible ? '' : 'none';
    ch.classList.toggle('enemy', !!onEnemy);
    const g = Math.round(6 + spreadPx);
    const kids = ch.children;
    kids[0].style.top = `${-g - 10}px`;
    kids[1].style.top = `${g}px`;
    kids[2].style.left = `${-g - 10}px`;
    kids[3].style.left = `${g}px`;
  }

  setTargetName(name) { this._set('targetName', name || ''); }

  hit(kill, head) {
    const h = this.el.hitmarker;
    h.classList.toggle('kill', kill);
    h.classList.toggle('head', head && !kill);
    this.hitT = kill ? 0.45 : 0.22;
    h.style.opacity = 1;
    h.style.transform = `scale(${kill ? 1.35 : 1})`;
  }

  popup(text) {
    const d = document.createElement('div');
    d.className = 'popup';
    d.textContent = text;
    d.style.top = `${-this.el.popups.children.length * 26}px`;
    this.el.popups.appendChild(d);
    setTimeout(() => d.remove(), 1100);
  }

  medal(title, sub = '') {
    const d = document.createElement('div');
    d.className = 'medal';
    d.innerHTML = `${title}${sub ? `<small>${sub}</small>` : ''}`;
    this.el.medals.appendChild(d);
    while (this.el.medals.children.length > 3) this.el.medals.firstChild.remove();
    setTimeout(() => d.remove(), 2200);
  }

  message(title, sub = '', warn = false, dur = 2.5) {
    const m = this.el.centerMsg;
    m.innerHTML = `${title}${sub ? `<small>${sub}</small>` : ''}`;
    m.classList.toggle('warn', warn);
    m.classList.add('show');
    this.msgT = dur;
  }

  killfeed(killer, victim, weapon, head, playerTeam, playerRef) {
    const d = document.createElement('div');
    d.className = 'kf';
    const cls = (c) => (c === playerRef ? 'me' : c.team === playerTeam ? 'a' : 'e');
    d.innerHTML = killer
      ? `<span class="${cls(killer)}">${killer.name}</span><span class="w">[${weapon}]</span>${head ? '<span class="hs">✦</span>' : ''}<span class="${cls(victim)}">${victim.name}</span>`
      : `<span class="${cls(victim)}">${victim.name}</span><span class="w">[${weapon}]</span>`;
    this.el.killfeed.appendChild(d);
    while (this.el.killfeed.children.length > 6) this.el.killfeed.firstChild.remove();
    setTimeout(() => d.remove(), 6000);
  }

  damageFrom(angle) {
    const d = document.createElement('div');
    d.className = 'dmg';
    d.style.transform = `rotate(${angle}rad)`;
    this.el.dmg.appendChild(d);
    setTimeout(() => d.remove(), 1600);
  }

  grenadeWarning(angle) {
    const g = this.el.grenade;
    if (angle === null) { g.classList.add('hidden'); return; }
    g.classList.remove('hidden');
    g.style.transform = `rotate(${angle}rad) translateY(-110px)`;
    g.firstElementChild.style.transform = `rotate(${-angle}rad)`;
  }

  setStreaks(streak, available, uavActive) {
    const set = (el, need, key) => {
      el.classList.toggle('ready', available.includes(key));
      el.classList.toggle('active', key === 'uav' && uavActive);
      el.style.opacity = available.includes(key) || (key === 'uav' && uavActive) ? '' : String(0.35 + Math.min(1, streak / need) * 0.35);
    };
    set(this.el.streakUav, 3, 'uav');
    set(this.el.streakOrb, 5, 'orbital');
    this.el.mmUav.classList.toggle('hidden', !uavActive);
  }

  setScope(on) { this.el.scope.classList.toggle('hidden', !on); }

  flash(v) { this.flashV = Math.max(this.flashV, v); }

  scoreboard(show, chars, player, limit) {
    this.el.scoreboard.classList.toggle('hidden', !show);
    if (!show) return;
    this.el.sbLimit.textContent = `· FIRST TO ${limit}`;
    const rows = (team) => chars.filter((c) => c.team === team).sort((a, b) => b.score - a.score)
      .map((c) => `<tr class="${c === player ? 'me' : ''} ${c.alive ? '' : 'dead'}"><td>${c.name}</td><td>${c.score}</td><td>${c.kills}</td><td>${c.deaths}</td></tr>`).join('');
    this.el.sbAlly.innerHTML = rows(0);
    this.el.sbEnemy.innerHTML = rows(1);
  }

  death(show, killer, weapon, t) {
    this.el.death.classList.toggle('hidden', !show);
    if (!show) return;
    this.el.dsKiller.textContent = killer || 'THE ENVIRONMENT';
    this.el.dsWeapon.textContent = weapon ? `WITH ${weapon}` : '';
    this.el.dsTimer.textContent = Math.max(0, Math.ceil(t));
  }

  update(dt) {
    if (this.hitT > 0) {
      this.hitT -= dt;
      if (this.hitT <= 0) this.el.hitmarker.style.opacity = 0;
    }
    if (this.msgT > 0) {
      this.msgT -= dt;
      if (this.msgT <= 0) this.el.centerMsg.classList.remove('show');
    }
    if (this.flashV > 0.001) {
      this.flashV *= Math.pow(0.25, dt);
      this.el.flash.style.opacity = Math.min(1, this.flashV).toFixed(3);
    } else if (this.flashV !== 0) {
      this.flashV = 0;
      this.el.flash.style.opacity = 0;
    }
  }
}
