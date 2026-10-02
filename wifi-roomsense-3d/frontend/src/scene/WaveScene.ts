/**
 * "Wi-Fi waves (simulated)": a 3-D view of the room layout with a SIMULATED
 * coverage glow and expanding wave rings from the chosen transmitter.
 *
 * Everything drawn here except the optional "your Mac" marker text comes from
 * lib/propagation (room layout + assumed wall losses). Nothing is measured, no
 * people or objects are shown, and the animation is a picture of the model,
 * not of real wave fronts.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import type { RoomGeometry, Vec2, Wall } from '../api/types';
import { toThree } from '../lib/coords';
import { type PropagationParams, coverageRgba, gridRange, levelFraction, predictDbm, predictGrid } from '../lib/propagation';

const RING_COUNT = 6;
const RING_SEGMENTS = 128;
const RING_HEIGHTS = [0.15, 1.1];
const WAVE_SPEED = 0.18; // fraction of the max radius per second (display only)

function label(text: string, cls = ''): CSS2DObject {
  const el = document.createElement('div');
  el.className = `scene-label ${cls}`;
  el.textContent = text;
  return new CSS2DObject(el);
}

function clearGroup(group: THREE.Object3D): void {
  for (const child of [...group.children]) {
    clearGroup(child);
    group.remove(child);
    const mesh = child as THREE.Mesh;
    mesh.geometry?.dispose();
    const mat = mesh.material as (THREE.Material & { map?: THREE.Texture | null }) | THREE.Material[] | undefined;
    if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
    else if (mat) {
      mat.map?.dispose();
      mat.dispose();
    }
    if (child instanceof CSS2DObject) child.element.remove();
  }
}

interface Ring {
  line: THREE.LineLoop;
  colors: Float32Array;
  positions: Float32Array;
  height: number;
  phase0: number;
}

export interface LaptopMarker {
  position: Vec2;
  text: string;
}

export class WaveScene {
  private readonly renderer: THREE.WebGLRenderer;
  private readonly labelRenderer: CSS2DRenderer;
  private readonly scene = new THREE.Scene();
  private readonly camera = new THREE.PerspectiveCamera(45, 1, 0.05, 300);
  private readonly controls: OrbitControls;
  private readonly staticGroup = new THREE.Group();
  private readonly ringGroup = new THREE.Group();
  private rings: Ring[] = [];
  private walls: Wall[] = [];
  private doors: RoomGeometry['doors'] = [];
  private tx: Vec2 | null = null;
  private params: PropagationParams | null = null;
  private maxRadius = 6;
  private range = { lo: -90, hi: -35 };
  private rafId: number | null = null;
  private disposed = false;
  private animate = true;
  private readonly t0 = performance.now();
  private roomKey: string | null = null;
  private readonly home = { target: new THREE.Vector3(), camera: new THREE.Vector3(6, 8, 8) };

  constructor(private readonly container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x070c16, 1);
    this.renderer.domElement.className = 'scene-canvas';
    this.renderer.domElement.setAttribute('role', 'img');
    this.renderer.domElement.setAttribute('aria-label', 'Simulated Wi-Fi coverage and wave rings over the room layout');
    container.appendChild(this.renderer.domElement);
    this.labelRenderer = new CSS2DRenderer();
    this.labelRenderer.domElement.className = 'scene-labels';
    container.appendChild(this.labelRenderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI / 2.05;
    this.scene.add(new THREE.AmbientLight(0x9fb8ff, 0.6));
    this.scene.add(this.staticGroup, this.ringGroup);

    const rect = container.getBoundingClientRect();
    this.resize(Math.max(1, rect.width), Math.max(1, rect.height));
    this.loop();
  }

  setAnimate(on: boolean): void {
    this.animate = on;
    this.ringGroup.visible = on;
  }

  resetView(): void {
    this.controls.target.copy(this.home.target);
    this.camera.position.copy(this.home.camera);
    this.controls.update();
  }

  /** Rebuild the whole view for a room, transmitter position and model parameters. */
  setModel(room: RoomGeometry, tx: Vec2, txHeight: number, txLabel: string, params: PropagationParams,
           laptop: LaptopMarker | null): void {
    clearGroup(this.staticGroup);
    clearGroup(this.ringGroup);
    this.rings = [];
    this.walls = room.walls;
    this.doors = room.doors;
    this.tx = tx;
    this.params = params;

    // Floor grid around the layout.
    const span = Math.max(room.width_m, room.depth_m) + 4;
    const grid = new THREE.GridHelper(span, Math.round(span), 0x1d3557, 0x12203a);
    grid.position.set(room.width_m / 2, 0, -room.depth_m / 2);
    this.staticGroup.add(grid);

    // Glowing wireframe walls (the user's layout, not reconstructed).
    for (const w of room.walls) {
      const len = Math.hypot(w.end.x - w.start.x, w.end.y - w.start.y);
      if (len < 1e-6) continue;
      const geo = new THREE.BoxGeometry(len, w.height_m, Math.max(0.04, w.thickness_m));
      const mid = { x: (w.start.x + w.end.x) / 2, y: (w.start.y + w.end.y) / 2, z: w.height_m / 2 };
      const [mx, my, mz] = toThree(mid);
      const angle = Math.atan2(w.end.y - w.start.y, w.end.x - w.start.x);
      const face = new THREE.Mesh(
        geo,
        new THREE.MeshBasicMaterial({ color: 0x4dabf7, transparent: true, opacity: 0.07, depthWrite: false }),
      );
      const edges = new THREE.LineSegments(
        new THREE.EdgesGeometry(geo),
        new THREE.LineBasicMaterial({ color: 0x74c0fc, transparent: true, opacity: 0.8 }),
      );
      for (const o of [face, edges]) {
        o.position.set(mx, my, mz);
        o.rotation.y = angle; // three.js y-up rotation; floor y maps to -z
        this.staticGroup.add(o);
      }
    }
    for (const d of room.doors) {
      const w = room.walls.find((x) => x.id === d.wall_id);
      if (!w) continue;
      const len = Math.hypot(w.end.x - w.start.x, w.end.y - w.start.y) || 1;
      const ux = (w.end.x - w.start.x) / len;
      const uy = (w.end.y - w.start.y) / len;
      const a = { x: w.start.x + ux * d.offset_m, y: w.start.y + uy * d.offset_m };
      const b = { x: a.x + ux * d.width_m, y: a.y + uy * d.width_m };
      const pts = [
        new THREE.Vector3(...toThree({ ...a, z: 0 })),
        new THREE.Vector3(...toThree({ ...a, z: d.height_m })),
        new THREE.Vector3(...toThree({ ...b, z: d.height_m })),
        new THREE.Vector3(...toThree({ ...b, z: 0 })),
      ];
      this.staticGroup.add(
        new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: 0xffd43b })),
      );
    }

    // Simulated coverage glow on the floor.
    const g = predictGrid(room, tx, params);
    this.range = gridRange(g);
    const { lo, hi } = this.range;
    const canvas = document.createElement('canvas');
    canvas.width = g.nx;
    canvas.height = g.ny;
    const ctx = canvas.getContext('2d');
    if (ctx) {
      const img = ctx.createImageData(g.nx, g.ny);
      for (let j = 0; j < g.ny; j++) {
        for (let i = 0; i < g.nx; i++) {
          // Canvas row 0 is the top = largest y (north), so flip rows.
          const [r, gg, b, a] = coverageRgba(g.dbm[j * g.nx + i] ?? lo, lo, hi);
          const k = ((g.ny - 1 - j) * g.nx + i) * 4;
          img.data[k] = r;
          img.data[k + 1] = gg;
          img.data[k + 2] = b;
          img.data[k + 3] = a;
        }
      }
      ctx.putImageData(img, 0, 0);
      const tex = new THREE.CanvasTexture(canvas);
      tex.colorSpace = THREE.SRGBColorSpace;
      const w = g.nx * g.cell;
      const h = g.ny * g.cell;
      const plane = new THREE.Mesh(
        new THREE.PlaneGeometry(w, h),
        new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false }),
      );
      plane.rotation.x = -Math.PI / 2;
      const [px, , pz] = toThree({ x: g.x0 + w / 2, y: g.y0 + h / 2 });
      plane.position.set(px, 0.01, pz);
      this.staticGroup.add(plane);
      this.maxRadius = Math.hypot(w, h) / 2;
    }

    // Transmitter marker.
    const [tx3, ty3, tz3] = toThree({ ...tx, z: txHeight });
    const marker = new THREE.Mesh(
      new THREE.BoxGeometry(0.28, 0.08, 0.18),
      new THREE.MeshBasicMaterial({ color: 0xe7f5ff }),
    );
    marker.position.set(tx3, ty3, tz3);
    this.staticGroup.add(marker);
    const txl = label(`Wi-Fi source: ${txLabel}`);
    txl.position.set(tx3, ty3 + 0.35, tz3);
    this.staticGroup.add(txl);

    if (laptop) {
      const [lx, , lz] = toThree(laptop.position);
      const lm = new THREE.Mesh(new THREE.BoxGeometry(0.32, 0.03, 0.22), new THREE.MeshBasicMaterial({ color: 0xffd43b }));
      lm.position.set(lx, 0.75, lz);
      this.staticGroup.add(lm);
      const ll = label(laptop.text, 'scene-label--target');
      ll.position.set(lx, 1.1, lz);
      this.staticGroup.add(ll);
    }

    const banner = label('SIMULATED model of the layout, not a measurement', 'scene-label--decorative');
    banner.position.set(room.width_m / 2, room.height_m + 0.6, -room.depth_m / 2);
    this.staticGroup.add(banner);

    // Wave rings (display of the model).
    for (const h of RING_HEIGHTS) {
      for (let k = 0; k < RING_COUNT; k++) {
        const positions = new Float32Array(RING_SEGMENTS * 3);
        const colors = new Float32Array(RING_SEGMENTS * 3);
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
        geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
        const line = new THREE.LineLoop(
          geo,
          new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }),
        );
        this.ringGroup.add(line);
        this.rings.push({ line, colors, positions, height: h, phase0: k / RING_COUNT });
      }
    }

    const [cx, , cz] = toThree({ x: room.width_m / 2, y: room.depth_m / 2 });
    this.home.target.set(cx, 0.8, cz);
    this.home.camera.set(cx - span * 0.55, span * 0.75, cz + span * 0.8);
    // Keep the user's camera while they tweak settings; reframe only for a different room.
    const roomKey = `${room.geometry_id}:${room.width_m}:${room.depth_m}`;
    if (roomKey !== this.roomKey) {
      this.roomKey = roomKey;
      this.resetView();
    }
  }

  private updateRings(t: number): void {
    const tx = this.tx;
    const params = this.params;
    if (!tx || !params) return;
    const { lo, hi } = this.range;
    // Rings at the same phase share positions in plan and colours; only the height differs.
    const shared = new Map<number, { xz: Float32Array; rgb: Float32Array }>();
    for (const ring of this.rings) {
      let s = shared.get(ring.phase0);
      if (!s) {
        const phase = (ring.phase0 + t * WAVE_SPEED) % 1;
        const r = 0.3 + phase * this.maxRadius;
        const fade = 1 - phase;
        const xz = new Float32Array(RING_SEGMENTS * 2);
        const rgb = new Float32Array(RING_SEGMENTS * 3);
        for (let k = 0; k < RING_SEGMENTS; k++) {
          const a = (k / RING_SEGMENTS) * Math.PI * 2;
          const p = { x: tx.x + Math.cos(a) * r, y: tx.y + Math.sin(a) * r };
          const [x3, , z3] = toThree(p);
          xz[k * 2] = x3;
          xz[k * 2 + 1] = z3;
          // Brightness follows the model: weaker where distance and walls cut the predicted signal.
          const lvl = predictDbm(tx, p, this.walls, this.doors, params).dbm;
          const f = (0.15 + 0.85 * levelFraction(lvl, lo, hi)) * fade;
          rgb[k * 3] = 0.25 * f;
          rgb[k * 3 + 1] = 0.75 * f;
          rgb[k * 3 + 2] = 1.0 * f;
        }
        s = { xz, rgb };
        shared.set(ring.phase0, s);
      }
      for (let k = 0; k < RING_SEGMENTS; k++) {
        ring.positions[k * 3] = s.xz[k * 2] ?? 0;
        ring.positions[k * 3 + 1] = ring.height;
        ring.positions[k * 3 + 2] = s.xz[k * 2 + 1] ?? 0;
      }
      ring.colors.set(s.rgb);
      (ring.line.geometry.getAttribute('position') as THREE.BufferAttribute).needsUpdate = true;
      (ring.line.geometry.getAttribute('color') as THREE.BufferAttribute).needsUpdate = true;
    }
  }

  resize(width: number, height: number): void {
    this.camera.aspect = width / Math.max(1, height);
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
    this.renderer.domElement.style.width = `${width}px`;
    this.renderer.domElement.style.height = `${height}px`;
    this.labelRenderer.setSize(width, height);
  }

  private loop = (): void => {
    if (this.disposed) return;
    this.rafId = requestAnimationFrame(this.loop);
    this.controls.update();
    if (this.animate) this.updateRings((performance.now() - this.t0) / 1000);
    this.renderer.render(this.scene, this.camera);
    this.labelRenderer.render(this.scene, this.camera);
  };

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.controls.dispose();
    clearGroup(this.staticGroup);
    clearGroup(this.ringGroup);
    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.labelRenderer.domElement.remove();
    this.container.replaceChildren();
  }
}
