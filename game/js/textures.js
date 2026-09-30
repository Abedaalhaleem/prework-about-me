// Procedural canvas textures — everything is generated at runtime, no image assets.
import * as THREE from 'three';

let seed = 1337;
export function rand() {
  seed = (seed * 16807) % 2147483647;
  return (seed - 1) / 2147483646;
}

function canvas(w, h = w) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  return [c, c.getContext('2d')];
}

function toTexture(c, { repeat = true, srgb = true, aniso = 8 } = {}) {
  const t = new THREE.CanvasTexture(c);
  if (repeat) t.wrapS = t.wrapT = THREE.RepeatWrapping;
  if (srgb) t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = aniso;
  return t;
}

function noise(ctx, w, h, amount, alpha = 0.08, dark = true) {
  for (let i = 0; i < amount; i++) {
    const v = dark ? Math.floor(rand() * 60) : 180 + Math.floor(rand() * 75);
    ctx.fillStyle = `rgba(${v},${v},${v},${alpha * rand()})`;
    const s = 1 + rand() * 3;
    ctx.fillRect(rand() * w, rand() * h, s, s);
  }
}

export function asphalt() {
  const [c, x] = canvas(512);
  x.fillStyle = '#26282d';
  x.fillRect(0, 0, 512, 512);
  noise(x, 512, 512, 9000, 0.35, true);
  noise(x, 512, 512, 5000, 0.12, false);
  // oil stains / patches
  for (let i = 0; i < 6; i++) {
    x.fillStyle = 'rgba(10,10,14,0.18)';
    x.beginPath();
    x.ellipse(rand() * 512, rand() * 512, 20 + rand() * 50, 12 + rand() * 30, rand() * 3, 0, Math.PI * 2);
    x.fill();
  }
  // cracks
  x.strokeStyle = 'rgba(8,8,10,0.55)';
  x.lineWidth = 1.2;
  for (let i = 0; i < 7; i++) {
    let px = rand() * 512, py = rand() * 512;
    x.beginPath();
    x.moveTo(px, py);
    for (let k = 0; k < 8; k++) {
      px += (rand() - 0.5) * 50;
      py += (rand() - 0.5) * 50;
      x.lineTo(px, py);
    }
    x.stroke();
  }
  return toTexture(c);
}

export function concrete(tile = true) {
  const [c, x] = canvas(512);
  x.fillStyle = '#8f9296';
  x.fillRect(0, 0, 512, 512);
  noise(x, 512, 512, 12000, 0.18, true);
  noise(x, 512, 512, 6000, 0.12, false);
  if (tile) {
    x.strokeStyle = 'rgba(40,42,46,0.55)';
    x.lineWidth = 3;
    for (let i = 0; i <= 512; i += 256) {
      x.beginPath(); x.moveTo(i, 0); x.lineTo(i, 512); x.stroke();
      x.beginPath(); x.moveTo(0, i); x.lineTo(512, i); x.stroke();
    }
  }
  return toTexture(c);
}

export function grass() {
  const [c, x] = canvas(512);
  x.fillStyle = '#2f5a2a';
  x.fillRect(0, 0, 512, 512);
  for (let i = 0; i < 26000; i++) {
    const g = 70 + Math.floor(rand() * 90);
    const r = 30 + Math.floor(rand() * 40);
    x.fillStyle = `rgba(${r},${g},${Math.floor(r * 0.6)},${0.25 + rand() * 0.4})`;
    x.fillRect(rand() * 512, rand() * 512, 1 + rand() * 1.5, 2 + rand() * 4);
  }
  // mowing stripes
  for (let i = 0; i < 512; i += 64) {
    x.fillStyle = i % 128 === 0 ? 'rgba(255,255,255,0.035)' : 'rgba(0,0,0,0.05)';
    x.fillRect(i, 0, 64, 512);
  }
  return toTexture(c);
}

// Futuristic prefab siding panels (near-white; tinted by material.color)
export function panels() {
  const [c, x] = canvas(512);
  x.fillStyle = '#e9e9ea';
  x.fillRect(0, 0, 512, 512);
  noise(x, 512, 512, 5000, 0.05, true);
  // panel seams
  x.strokeStyle = 'rgba(60,64,70,0.55)';
  x.lineWidth = 3;
  for (let y = 0; y <= 512; y += 128) {
    x.beginPath(); x.moveTo(0, y); x.lineTo(512, y); x.stroke();
  }
  for (let row = 0; row < 4; row++) {
    const off = row % 2 ? 128 : 0;
    for (let vx = off; vx <= 512; vx += 256) {
      x.beginPath(); x.moveTo(vx, row * 128); x.lineTo(vx, row * 128 + 128); x.stroke();
    }
  }
  // highlights under seams
  x.strokeStyle = 'rgba(255,255,255,0.7)';
  x.lineWidth = 1;
  for (let y = 2; y <= 512; y += 128) {
    x.beginPath(); x.moveTo(0, y); x.lineTo(512, y); x.stroke();
  }
  // rivets
  x.fillStyle = 'rgba(80,84,90,0.6)';
  for (let y = 8; y < 512; y += 128) {
    for (let vx = 10; vx < 512; vx += 42) x.fillRect(vx, y, 3, 3);
  }
  // grime gradient at bottom of every panel
  for (let y = 0; y < 512; y += 128) {
    const g = x.createLinearGradient(0, y + 80, 0, y + 128);
    g.addColorStop(0, 'rgba(60,50,40,0)');
    g.addColorStop(1, 'rgba(60,50,40,0.12)');
    x.fillStyle = g;
    x.fillRect(0, y + 80, 512, 48);
  }
  return toTexture(c);
}

export function metal() {
  const [c, x] = canvas(256);
  x.fillStyle = '#3a3e45';
  x.fillRect(0, 0, 256, 256);
  for (let i = 0; i < 900; i++) {
    const v = 40 + Math.floor(rand() * 60);
    x.strokeStyle = `rgba(${v},${v + 4},${v + 10},0.25)`;
    const y = rand() * 256;
    x.beginPath(); x.moveTo(0, y); x.lineTo(256, y + (rand() - 0.5) * 3); x.stroke();
  }
  noise(x, 256, 256, 1500, 0.2, true);
  return toTexture(c);
}

export function darkTiles() {
  const [c, x] = canvas(256);
  x.fillStyle = '#2b2e34';
  x.fillRect(0, 0, 256, 256);
  noise(x, 256, 256, 3000, 0.1, false);
  x.strokeStyle = 'rgba(0,0,0,0.6)';
  x.lineWidth = 2;
  for (let i = 0; i <= 256; i += 64) {
    x.beginPath(); x.moveTo(i, 0); x.lineTo(i, 256); x.stroke();
    x.beginPath(); x.moveTo(0, i); x.lineTo(256, i); x.stroke();
  }
  return toTexture(c);
}

export function container(color = '#2f6f8f') {
  const [c, x] = canvas(256);
  x.fillStyle = color;
  x.fillRect(0, 0, 256, 256);
  for (let i = 0; i < 256; i += 16) {
    x.fillStyle = 'rgba(0,0,0,0.28)';
    x.fillRect(i, 0, 5, 256);
    x.fillStyle = 'rgba(255,255,255,0.12)';
    x.fillRect(i + 6, 0, 2, 256);
  }
  noise(x, 256, 256, 2500, 0.25, true);
  // rust streaks
  for (let i = 0; i < 20; i++) {
    x.fillStyle = 'rgba(90,50,20,0.15)';
    x.fillRect(rand() * 256, 0, 2 + rand() * 4, rand() * 120);
  }
  return toTexture(c);
}

export function hazard() {
  const [c, x] = canvas(128);
  x.fillStyle = '#d8d8d4';
  x.fillRect(0, 0, 128, 128);
  x.fillStyle = '#e8541c';
  for (let i = -128; i < 256; i += 48) {
    x.beginPath();
    x.moveTo(i, 0); x.lineTo(i + 24, 0); x.lineTo(i + 24 + 128, 128); x.lineTo(i + 128, 128);
    x.fill();
  }
  noise(x, 128, 128, 800, 0.2, true);
  return toTexture(c);
}

export function solar() {
  const [c, x] = canvas(256);
  x.fillStyle = '#0f1a33';
  x.fillRect(0, 0, 256, 256);
  x.strokeStyle = '#6f86b8';
  x.lineWidth = 2;
  for (let i = 0; i <= 256; i += 32) {
    x.beginPath(); x.moveTo(i, 0); x.lineTo(i, 256); x.stroke();
    x.beginPath(); x.moveTo(0, i); x.lineTo(256, i); x.stroke();
  }
  const g = x.createLinearGradient(0, 0, 256, 256);
  g.addColorStop(0, 'rgba(120,160,255,0.25)');
  g.addColorStop(0.5, 'rgba(0,0,0,0)');
  g.addColorStop(1, 'rgba(120,160,255,0.15)');
  x.fillStyle = g;
  x.fillRect(0, 0, 256, 256);
  return toTexture(c);
}

// Emissive window grid used for the distant skyline
export function skylineWindows() {
  const [c, x] = canvas(256, 512);
  x.fillStyle = '#05060c';
  x.fillRect(0, 0, 256, 512);
  const palette = ['#ffd38a', '#8fe8ff', '#ff8ad8', '#ffffff', '#ffb35c'];
  for (let y = 6; y < 512; y += 12) {
    for (let xx = 6; xx < 256; xx += 10) {
      if (rand() < 0.38) {
        x.fillStyle = palette[Math.floor(rand() * palette.length)];
        x.globalAlpha = 0.35 + rand() * 0.65;
        x.fillRect(xx, y, 6, 7);
      }
    }
  }
  x.globalAlpha = 1;
  return toTexture(c);
}

// ---------- Effect sprites ----------
export function softDot() {
  const [c, x] = canvas(64);
  const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.25, 'rgba(255,255,255,0.8)');
  g.addColorStop(0.6, 'rgba(255,255,255,0.2)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  x.fillStyle = g;
  x.fillRect(0, 0, 64, 64);
  return toTexture(c, { repeat: false, srgb: false });
}

export function flame() {
  const [c, x] = canvas(128);
  // irregular flame blob built from overlapping soft circles
  for (let i = 0; i < 40; i++) {
    const cx = 64 + (rand() - 0.5) * 50;
    const cy = 70 + (rand() - 0.5) * 50;
    const r = 12 + rand() * 26;
    const g = x.createRadialGradient(cx, cy, 0, cx, cy, r);
    g.addColorStop(0, 'rgba(255,255,255,0.35)');
    g.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = g;
    x.beginPath(); x.arc(cx, cy, r, 0, Math.PI * 2); x.fill();
  }
  // mask to a circle so edges fade
  x.globalCompositeOperation = 'destination-in';
  const m = x.createRadialGradient(64, 64, 10, 64, 64, 64);
  m.addColorStop(0, 'rgba(0,0,0,1)');
  m.addColorStop(1, 'rgba(0,0,0,0)');
  x.fillStyle = m;
  x.fillRect(0, 0, 128, 128);
  return toTexture(c, { repeat: false, srgb: false });
}

// Upright teardrop flame tongue (particles using it are kept upright)
export function flameTongue() {
  const [c, x] = canvas(128);
  const layer = (w, top, alpha, jitter) => {
    x.beginPath();
    x.moveTo(64 - w, 104);
    x.bezierCurveTo(64 - w * 1.1, 70, 64 - w * 0.35 + jitter, 40, 64 + jitter * 0.5, top);
    x.bezierCurveTo(64 + w * 0.35 + jitter, 40, 64 + w * 1.1, 70, 64 + w, 104);
    x.bezierCurveTo(64 + w, 124, 64 - w, 124, 64 - w, 104);
    x.closePath();
    x.fillStyle = `rgba(255,255,255,${alpha})`;
    x.fill();
  };
  x.filter = 'blur(6px)';
  for (let i = 0; i < 7; i++) layer(34 - i * 4, 8 + i * 9, 0.18, (rand() - 0.5) * 14);
  x.filter = 'blur(3px)';
  layer(12, 52, 0.35, 0);
  x.filter = 'none';
  // break up with dark noise so it flickers organically
  x.globalCompositeOperation = 'destination-out';
  for (let i = 0; i < 160; i++) {
    x.fillStyle = `rgba(0,0,0,${0.05 + rand() * 0.12})`;
    x.beginPath();
    x.arc(rand() * 128, rand() * 90, 3 + rand() * 8, 0, Math.PI * 2);
    x.fill();
  }
  return toTexture(c, { repeat: false, srgb: false });
}

export function smoke() {
  const [c, x] = canvas(128);
  for (let i = 0; i < 70; i++) {
    const cx = 64 + (rand() - 0.5) * 60;
    const cy = 64 + (rand() - 0.5) * 60;
    const r = 8 + rand() * 30;
    const g = x.createRadialGradient(cx, cy, 0, cx, cy, r);
    const v = 200 + Math.floor(rand() * 55);
    g.addColorStop(0, `rgba(${v},${v},${v},0.22)`);
    g.addColorStop(1, `rgba(${v},${v},${v},0)`);
    x.fillStyle = g;
    x.beginPath(); x.arc(cx, cy, r, 0, Math.PI * 2); x.fill();
  }
  x.globalCompositeOperation = 'destination-in';
  const m = x.createRadialGradient(64, 64, 16, 64, 64, 64);
  m.addColorStop(0, 'rgba(0,0,0,1)');
  m.addColorStop(1, 'rgba(0,0,0,0)');
  x.fillStyle = m;
  x.fillRect(0, 0, 128, 128);
  return toTexture(c, { repeat: false, srgb: false });
}

export function muzzleFlash() {
  const [c, x] = canvas(128);
  x.translate(64, 64);
  for (let i = 0; i < 7; i++) {
    x.rotate((Math.PI * 2) / 7 + rand() * 0.3);
    const g = x.createLinearGradient(0, 0, 0, 60);
    g.addColorStop(0, 'rgba(255,255,255,1)');
    g.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = g;
    x.beginPath();
    x.moveTo(-7, 0); x.lineTo(0, 30 + rand() * 32); x.lineTo(7, 0);
    x.fill();
  }
  x.setTransform(1, 0, 0, 1, 0, 0);
  const g = x.createRadialGradient(64, 64, 0, 64, 64, 30);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  x.fillStyle = g;
  x.fillRect(0, 0, 128, 128);
  return toTexture(c, { repeat: false, srgb: false });
}

export function bulletHole() {
  const [c, x] = canvas(64);
  const g = x.createRadialGradient(32, 32, 0, 32, 32, 30);
  g.addColorStop(0, 'rgba(0,0,0,1)');
  g.addColorStop(0.18, 'rgba(10,8,6,0.95)');
  g.addColorStop(0.35, 'rgba(40,34,30,0.6)');
  g.addColorStop(1, 'rgba(40,34,30,0)');
  x.fillStyle = g;
  x.fillRect(0, 0, 64, 64);
  x.strokeStyle = 'rgba(0,0,0,0.5)';
  for (let i = 0; i < 6; i++) {
    const a = rand() * Math.PI * 2;
    x.beginPath(); x.moveTo(32, 32); x.lineTo(32 + Math.cos(a) * 18, 32 + Math.sin(a) * 18); x.stroke();
  }
  return toTexture(c, { repeat: false });
}

export function scorch() {
  const [c, x] = canvas(256);
  for (let i = 0; i < 60; i++) {
    const cx = 128 + (rand() - 0.5) * 110;
    const cy = 128 + (rand() - 0.5) * 110;
    const r = 20 + rand() * 60;
    const g = x.createRadialGradient(cx, cy, 0, cx, cy, r);
    g.addColorStop(0, 'rgba(5,4,3,0.35)');
    g.addColorStop(1, 'rgba(5,4,3,0)');
    x.fillStyle = g;
    x.beginPath(); x.arc(cx, cy, r, 0, Math.PI * 2); x.fill();
  }
  x.globalCompositeOperation = 'destination-in';
  const m = x.createRadialGradient(128, 128, 40, 128, 128, 128);
  m.addColorStop(0, 'rgba(0,0,0,1)');
  m.addColorStop(1, 'rgba(0,0,0,0)');
  x.fillStyle = m;
  x.fillRect(0, 0, 256, 256);
  return toTexture(c, { repeat: false });
}

export function reticle(color = '#ff3030') {
  const [c, x] = canvas(128);
  x.strokeStyle = color;
  x.fillStyle = color;
  x.lineWidth = 5;
  x.beginPath(); x.arc(64, 64, 40, 0, Math.PI * 2); x.stroke();
  x.beginPath(); x.arc(64, 64, 6, 0, Math.PI * 2); x.fill();
  for (const [a, b, cc, d] of [[64, 4, 64, 20], [64, 108, 64, 124], [4, 64, 20, 64], [108, 64, 124, 64]]) {
    x.beginPath(); x.moveTo(a, b); x.lineTo(cc, d); x.stroke();
  }
  return toTexture(c, { repeat: false });
}

export function ring() {
  const [c, x] = canvas(256);
  const g = x.createRadialGradient(128, 128, 60, 128, 128, 128);
  g.addColorStop(0, 'rgba(255,255,255,0)');
  g.addColorStop(0.75, 'rgba(255,255,255,0.9)');
  g.addColorStop(0.85, 'rgba(255,255,255,0.4)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  x.fillStyle = g;
  x.fillRect(0, 0, 256, 256);
  return toTexture(c, { repeat: false, srgb: false });
}

// Holographic sign with scanlines; returns {texture, draw(time)} so it can animate.
export function holoSign(lines, color = '#35f0ff', w = 512, h = 256) {
  const [c, x] = canvas(w, h);
  const tex = toTexture(c, { repeat: false });
  function draw(t = 0) {
    x.clearRect(0, 0, w, h);
    x.fillStyle = 'rgba(0,20,30,0.25)';
    x.fillRect(0, 0, w, h);
    x.strokeStyle = color;
    x.lineWidth = 4;
    x.strokeRect(6, 6, w - 12, h - 12);
    x.fillStyle = color;
    x.textAlign = 'center';
    x.textBaseline = 'middle';
    x.shadowColor = color;
    x.shadowBlur = 16;
    const lh = h / (lines.length + 0.5);
    lines.forEach((ln, i) => {
      const text = typeof ln === 'function' ? ln(t) : ln;
      const size = i === 0 ? lh * 0.62 : lh * 0.42;
      x.font = `700 ${size}px Orbitron, "Rajdhani", Arial, sans-serif`;
      x.fillText(text, w / 2, lh * (i + 0.75));
    });
    x.shadowBlur = 0;
    // scanlines
    x.fillStyle = 'rgba(0,0,0,0.35)';
    for (let y = (t * 40) % 4; y < h; y += 4) x.fillRect(0, y, w, 1.5);
    // glitch band
    const gy = ((t * 70) % (h + 60)) - 30;
    x.fillStyle = 'rgba(255,255,255,0.08)';
    x.fillRect(0, gy, w, 18);
    tex.needsUpdate = true;
  }
  draw(0);
  return { texture: tex, draw };
}

export function nameTag(text, color) {
  const [c, x] = canvas(256, 64);
  x.font = '700 30px Rajdhani, Arial, sans-serif';
  x.textAlign = 'center';
  x.textBaseline = 'middle';
  x.shadowColor = 'rgba(0,0,0,0.9)';
  x.shadowBlur = 6;
  x.fillStyle = color;
  x.fillText(text, 128, 26);
  // chevron
  x.beginPath();
  x.moveTo(116, 48); x.lineTo(128, 60); x.lineTo(140, 48);
  x.lineWidth = 4;
  x.strokeStyle = color;
  x.stroke();
  return toTexture(c, { repeat: false });
}
