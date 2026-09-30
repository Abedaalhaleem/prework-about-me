// Fully procedural Web Audio sound effects (no audio files).
export class Audio {
  constructor() {
    this.ctx = null;
    this.volume = 0.7;
    this.listener = { x: 0, y: 0, z: 0, yaw: 0 };
  }

  init() {
    if (this.ctx) {
      if (this.ctx.state === 'suspended') this.ctx.resume();
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = (this.ctx = new AC());
    this.master = ctx.createGain();
    this.master.gain.value = this.volume;
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -14;
    comp.ratio.value = 6;
    this.master.connect(comp).connect(ctx.destination);

    // White noise buffer
    const len = ctx.sampleRate * 2;
    this.noise = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = this.noise.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;

    // Reverb impulse (outdoor slap / tail)
    const ir = ctx.createBuffer(2, ctx.sampleRate * 1.8, ctx.sampleRate);
    for (let c = 0; c < 2; c++) {
      const ch = ir.getChannelData(c);
      for (let i = 0; i < ch.length; i++) ch[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / ch.length, 3.2);
    }
    this.reverb = ctx.createConvolver();
    this.reverb.buffer = ir;
    this.reverbGain = ctx.createGain();
    this.reverbGain.gain.value = 0.35;
    this.reverb.connect(this.reverbGain).connect(this.master);

    this._startAmbience();
  }

  setVolume(v) {
    this.volume = v;
    if (this.master) this.master.gain.value = v;
  }

  // Distance attenuation + stereo pan relative to the listener
  _spatial(pos) {
    if (!pos) return { gain: 1, pan: 0, dist: 0 };
    const L = this.listener;
    const dx = pos.x - L.x, dz = pos.z - L.z, dy = pos.y - L.y;
    const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
    // listener right vector for yaw (camera looks down -z at yaw 0)
    const rx = Math.cos(L.yaw), rz = -Math.sin(L.yaw);
    const pan = dist > 0.01 ? Math.max(-1, Math.min(1, (dx * rx + dz * rz) / dist)) : 0;
    const gain = 1 / (1 + dist * 0.09);
    return { gain, pan, dist };
  }

  _out(pos, gainMul = 1, reverbSend = 0.3) {
    const ctx = this.ctx;
    const sp = this._spatial(pos);
    const g = ctx.createGain();
    g.gain.value = sp.gain * gainMul;
    const p = ctx.createStereoPanner ? ctx.createStereoPanner() : null;
    if (p) {
      p.pan.value = sp.pan * 0.85;
      g.connect(p).connect(this.master);
    } else g.connect(this.master);
    if (reverbSend > 0) {
      const r = ctx.createGain();
      r.gain.value = reverbSend * (0.4 + Math.min(1, sp.dist / 25));
      g.connect(r).connect(this.reverb);
    }
    return { node: g, sp };
  }

  _noise(dest, t, dur, type, freq, q, gain, attack = 0.001, rateVar = 0) {
    const ctx = this.ctx;
    const src = ctx.createBufferSource();
    src.buffer = this.noise;
    src.playbackRate.value = 1 + (Math.random() - 0.5) * rateVar;
    const f = ctx.createBiquadFilter();
    f.type = type;
    f.frequency.value = freq;
    f.Q.value = q;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    src.connect(f).connect(g).connect(dest);
    src.start(t, Math.random() * 1.5);
    src.stop(t + dur + 0.05);
    return f;
  }

  _tone(dest, t, dur, type, f0, f1, gain) {
    const ctx = this.ctx;
    const o = ctx.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(f0, t);
    o.frequency.exponentialRampToValueAtTime(Math.max(1, f1), t + dur);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(dest);
    o.start(t);
    o.stop(t + dur + 0.05);
  }

  shot(kind, pos = null) {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const far = pos ? this._spatial(pos).dist : 0;
    const { node } = this._out(pos, pos ? 0.9 : 1, 0.35);
    // distant shots lose their highs
    const lp = this.ctx.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.value = Math.max(900, 14000 - far * 280);
    lp.connect(node);
    if (kind === 'sniper') {
      this._noise(lp, t, 0.5, 'lowpass', 3200, 0.7, 1.0);
      this._noise(lp, t, 0.12, 'highpass', 2500, 0.5, 0.6);
      this._tone(lp, t, 0.35, 'sine', 140, 38, 1.0);
      this._tone(lp, t, 0.08, 'sawtooth', 1800, 300, 0.12);
    } else if (kind === 'smg') {
      this._noise(lp, t, 0.12, 'bandpass', 1900, 0.8, 0.7, 0.001, 0.2);
      this._noise(lp, t, 0.05, 'highpass', 4000, 0.5, 0.35);
      this._tone(lp, t, 0.08, 'triangle', 220, 70, 0.5);
    } else if (kind === 'bot') {
      this._noise(lp, t, 0.14, 'bandpass', 1400, 0.7, 0.8, 0.001, 0.2);
      this._tone(lp, t, 0.1, 'triangle', 180, 60, 0.45);
    } else {
      // assault rifle (player)
      this._noise(lp, t, 0.2, 'bandpass', 1300, 0.6, 0.9, 0.001, 0.15);
      this._noise(lp, t, 0.06, 'highpass', 3500, 0.5, 0.4);
      this._tone(lp, t, 0.12, 'sine', 160, 45, 0.9);
      this._tone(lp, t, 0.03, 'square', 900, 400, 0.05);
    }
  }

  explosion(pos, big = 1) {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node, sp } = this._out(pos, 1.6 * big, 0.6);
    const lp = this.ctx.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.setValueAtTime(Math.max(600, 5000 - sp.dist * 60), t);
    lp.frequency.exponentialRampToValueAtTime(180, t + 1.4);
    lp.connect(node);
    this._noise(lp, t, 1.8 * big, 'lowpass', 2400, 0.5, 1.0, 0.002);
    this._tone(lp, t, 0.9, 'sine', 90, 25, 1.2);
    this._tone(lp, t + 0.02, 0.5, 'triangle', 60, 20, 0.7);
    // debris rattle
    for (let i = 0; i < 6; i++) this._noise(lp, t + 0.25 + Math.random() * 0.8, 0.06, 'bandpass', 2500 + Math.random() * 3000, 3, 0.12);
  }

  nuke() {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 1.8, 0.8);
    const lp = this.ctx.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.setValueAtTime(3000, t);
    lp.frequency.exponentialRampToValueAtTime(90, t + 6);
    lp.connect(node);
    this._noise(lp, t, 7, 'lowpass', 1500, 0.4, 1.0, 0.05);
    this._tone(lp, t, 6, 'sine', 55, 18, 1.4);
    this._tone(lp, t + 1.2, 4, 'sawtooth', 40, 20, 0.3);
  }

  hitmarker(kill = false, head = false) {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.5, 0);
    this._tone(node, t, 0.05, 'square', head ? 2600 : 1900, head ? 2400 : 1700, 0.25);
    this._noise(node, t, 0.03, 'highpass', 6000, 0.5, 0.3);
    if (kill) this._tone(node, t + 0.03, 0.18, 'sine', 900, 600, 0.35);
  }

  bulletImpact(pos, surface) {
    if (!this.ctx || Math.random() < 0.4) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(pos, 0.35, 0.1);
    if (surface === 'metal' || surface === 'robot') this._tone(node, t, 0.12, 'triangle', 2400 + Math.random() * 1500, 900, 0.2);
    else this._noise(node, t, 0.06, 'bandpass', 1800, 1.2, 0.35);
  }

  whiz(pos) {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(pos, 0.5, 0);
    const f = this._noise(node, t, 0.18, 'bandpass', 3000, 6, 0.4, 0.04);
    f.frequency.exponentialRampToValueAtTime(1200, t + 0.18);
  }

  reload(stage) {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.5, 0.05);
    if (stage === 0) { this._noise(node, t, 0.08, 'bandpass', 1200, 4, 0.5); this._tone(node, t, 0.06, 'square', 500, 300, 0.05); }
    else if (stage === 1) { this._noise(node, t, 0.1, 'bandpass', 900, 3, 0.6); this._tone(node, t + 0.03, 0.06, 'square', 700, 900, 0.08); }
    else { this._noise(node, t, 0.05, 'highpass', 3000, 2, 0.6); this._noise(node, t + 0.09, 0.05, 'bandpass', 2000, 4, 0.5); }
  }

  dryFire() {
    if (!this.ctx) return;
    const { node } = this._out(null, 0.4, 0);
    this._tone(node, this.ctx.currentTime, 0.03, 'square', 1500, 1200, 0.1);
  }

  footstep(pos, loud = 1) {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(pos, 0.18 * loud, 0);
    this._noise(node, t, 0.07, 'lowpass', 700 + Math.random() * 300, 0.8, 0.8);
  }

  thrust() {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.5, 0.1);
    const f = this._noise(node, t, 0.45, 'bandpass', 500, 1.5, 0.8, 0.02);
    f.frequency.exponentialRampToValueAtTime(2200, t + 0.35);
    this._tone(node, t, 0.3, 'sawtooth', 120, 260, 0.08);
  }

  slide() {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.35, 0);
    this._noise(node, t, 0.6, 'bandpass', 900, 0.8, 0.5, 0.03);
  }

  melee() {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.5, 0);
    const f = this._noise(node, t, 0.2, 'bandpass', 800, 2, 0.6, 0.03);
    f.frequency.exponentialRampToValueAtTime(3000, t + 0.18);
  }

  hurt() {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.5, 0);
    this._tone(node, t, 0.15, 'sine', 180, 90, 0.5);
    this._noise(node, t, 0.1, 'lowpass', 400, 0.7, 0.5);
  }

  grenadeBounce(pos) {
    if (!this.ctx) return;
    const { node } = this._out(pos, 0.4, 0.1);
    this._tone(node, this.ctx.currentTime, 0.06, 'triangle', 1400 + Math.random() * 400, 900, 0.3);
  }

  pin() {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.4, 0);
    this._tone(node, t, 0.05, 'square', 3000, 2500, 0.08);
    this._noise(node, t + 0.12, 0.1, 'bandpass', 1500, 2, 0.4);
  }

  ui(kind = 'click') {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const { node } = this._out(null, 0.35, 0);
    if (kind === 'streak') {
      [660, 880, 1320].forEach((f, i) => this._tone(node, t + i * 0.09, 0.25, 'square', f, f, 0.08));
    } else if (kind === 'medal') {
      this._tone(node, t, 0.2, 'triangle', 1200, 1600, 0.2);
    } else if (kind === 'warning') {
      for (let i = 0; i < 3; i++) this._tone(node, t + i * 0.25, 0.15, 'square', 880, 880, 0.08);
    } else {
      this._tone(node, t, 0.05, 'square', 900, 1200, 0.06);
    }
  }

  // Looping fire crackle + city ambience
  _startAmbience() {
    const ctx = this.ctx;
    const mk = (type, freq, q, gain) => {
      const src = ctx.createBufferSource();
      src.buffer = this.noise;
      src.loop = true;
      const f = ctx.createBiquadFilter();
      f.type = type;
      f.frequency.value = freq;
      f.Q.value = q;
      const g = ctx.createGain();
      g.gain.value = gain;
      src.connect(f).connect(g).connect(this.master);
      src.start();
      return g;
    };
    this.windGain = mk('lowpass', 380, 0.4, 0.06);
    this.fireGain = mk('bandpass', 1600, 0.6, 0.0);
    this.fireLevel = 0;
  }

  setFireProximity(level) {
    if (!this.fireGain) return;
    this.fireLevel += (level - this.fireLevel) * 0.1;
    // crackle: random amplitude jitter
    const crackle = Math.random() < 0.3 ? 1.6 : 0.7;
    this.fireGain.gain.setTargetAtTime(this.fireLevel * 0.22 * crackle, this.ctx.currentTime, 0.02);
  }

  announce(text) {
    try {
      if (!('speechSynthesis' in window) || !this.voiceEnabled) return;
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.05;
      u.pitch = 0.6;
      u.volume = Math.min(1, this.volume * 1.2);
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(u);
    } catch { /* speech is optional */ }
  }
}
