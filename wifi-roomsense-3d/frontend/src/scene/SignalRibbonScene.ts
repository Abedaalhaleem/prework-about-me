/**
 * 3-D CHART of this computer's Wi-Fi signal strength over time.
 *
 * x = time (left: 2 minutes ago, right: now), height = signal strength (dBm),
 * colour = strength (red weak, green strong). It is a chart of one measured
 * number, NOT a picture of the room and not a position. Missing readings break
 * the ribbon; nothing is interpolated. The render loop only redraws.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { RIBBON, type RibbonPoint, dbmToHeight, strengthColor } from '../lib/ribbon';

function label(text: string, cls = ''): CSS2DObject {
  const el = document.createElement('div');
  el.className = `scene-label ${cls}`;
  el.textContent = text;
  return new CSS2DObject(el);
}

function disposeGroup(group: THREE.Group): void {
  for (const child of [...group.children]) {
    group.remove(child);
    const mesh = child as THREE.Mesh;
    mesh.geometry?.dispose();
    const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
    else mat?.dispose();
    if (child instanceof CSS2DObject) child.element.remove();
  }
}

export class SignalRibbonScene {
  private readonly renderer: THREE.WebGLRenderer;
  private readonly labelRenderer: CSS2DRenderer;
  private readonly scene = new THREE.Scene();
  private readonly camera = new THREE.PerspectiveCamera(42, 1, 0.05, 200);
  private readonly controls: OrbitControls;
  private readonly dataGroup = new THREE.Group();
  private rafId: number | null = null;
  private dirty = true;
  private disposed = false;

  constructor(private readonly container: HTMLElement) {
    // May throw when WebGL is unavailable; the React wrapper catches and says so.
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x0b0f14, 1);
    this.renderer.domElement.className = 'scene-canvas';
    this.renderer.domElement.setAttribute('role', 'img');
    this.renderer.domElement.setAttribute(
      'aria-label',
      '3-D chart of this computer\'s Wi-Fi signal strength over time (drag to rotate, scroll to zoom)',
    );
    container.appendChild(this.renderer.domElement);
    this.labelRenderer = new CSS2DRenderer();
    this.labelRenderer.domElement.className = 'scene-labels';
    container.appendChild(this.labelRenderer.domElement);

    this.camera.position.set(-2.5, 5.5, 10.5);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, RIBBON.height / 2.5, 0);
    this.controls.enableDamping = true;
    this.controls.minDistance = 3;
    this.controls.maxDistance = 30;
    this.controls.addEventListener('change', () => {
      this.dirty = true;
    });

    this.scene.add(new THREE.HemisphereLight(0xdde6f5, 0x1a1f26, 1.5));
    const sun = new THREE.DirectionalLight(0xffffff, 1.1);
    sun.position.set(4, 10, 8);
    this.scene.add(sun);
    this.buildAxes();
    this.scene.add(this.dataGroup);

    const rect = container.getBoundingClientRect();
    this.resize(Math.max(1, rect.width), Math.max(1, rect.height));
    this.loop();
  }

  private buildAxes(): void {
    const width = RIBBON.xMax - RIBBON.xMin;
    const grid = new THREE.GridHelper(width, 12, 0x3a4656, 0x222b36);
    grid.position.set((RIBBON.xMin + RIBBON.xMax) / 2, 0, 0);
    grid.scale.set(1, 1, (RIBBON.depth * 3) / width);
    this.scene.add(grid);

    const axisMat = new THREE.LineBasicMaterial({ color: 0x5c6b7d });
    const back = -RIBBON.depth * 1.5;
    const pts = [
      new THREE.Vector3(RIBBON.xMin, 0, back),
      new THREE.Vector3(RIBBON.xMin, RIBBON.height, back),
    ];
    this.scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), axisMat));
    for (const dbm of [-90, -70, -50, -30]) {
      const y = dbmToHeight(dbm);
      const tick = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(RIBBON.xMin, y, back),
          new THREE.Vector3(RIBBON.xMax, y, back),
        ]),
        new THREE.LineBasicMaterial({ color: 0x27313d }),
      );
      this.scene.add(tick);
      const l = label(`${dbm} dBm`, 'scene-label--compass');
      l.position.set(RIBBON.xMin - 0.6, y, back);
      this.scene.add(l);
    }
    const times: [number, string][] = [
      [RIBBON.xMin, '2 min ago'],
      [(RIBBON.xMin + RIBBON.xMax) / 2, '1 min ago'],
      [RIBBON.xMax, 'now'],
    ];
    for (const [x, text] of times) {
      const l = label(text, 'scene-label--compass');
      l.position.set(x, -0.25, RIBBON.depth * 1.6);
      this.scene.add(l);
    }
    const title = label('height = signal strength (closer to 0 dBm is stronger)', 'scene-label--compass');
    title.position.set(RIBBON.xMin, RIBBON.height + 0.5, back);
    this.scene.add(title);
  }

  /** Replace the drawn data. rssi/noise are segments from lib/ribbon. */
  setData(rssi: RibbonPoint[][], noise: RibbonPoint[][]): void {
    disposeGroup(this.dataGroup);
    for (const seg of rssi) this.addRibbon(seg);
    for (const seg of noise) this.addNoiseLine(seg);
    // Label the newest reading only if it really is recent (within ~3 s of
    // "now"); after a failed read the label disappears instead of going stale.
    const last = rssi[rssi.length - 1]?.at(-1);
    const recentX = RIBBON.xMax - (RIBBON.xMax - RIBBON.xMin) * (3 / 120);
    if (last && last.x >= recentX) {
      const l = label(`latest: ${last.dbm} dBm`);
      l.position.set(last.x, last.y + 0.45, 0);
      this.dataGroup.add(l);
    }
    this.dirty = true;
  }

  private addRibbon(seg: RibbonPoint[]): void {
    // A lone reading becomes a short stub so it is visible without inventing neighbours.
    const pts =
      seg.length === 1 && seg[0]
        ? [{ ...seg[0], x: seg[0].x - 0.04 }, { ...seg[0], x: seg[0].x + 0.04 }]
        : seg;
    const z0 = -RIBBON.depth / 2;
    const z1 = RIBBON.depth / 2;
    const top: number[] = [];
    const wall: number[] = [];
    const topColors: number[] = [];
    const wallColors: number[] = [];
    for (const p of pts) {
      const [r, g, b] = strengthColor(p.dbm);
      top.push(p.x, p.y, z0, p.x, p.y, z1);
      topColors.push(r, g, b, r, g, b);
      wall.push(p.x, 0, z1, p.x, p.y, z1);
      wallColors.push(r * 0.35, g * 0.35, b * 0.35, r * 0.8, g * 0.8, b * 0.8);
    }
    const index: number[] = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const a = i * 2;
      index.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
    }
    const mk = (pos: number[], col: number[], opacity: number): THREE.Mesh => {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
      geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
      geo.setIndex(index);
      geo.computeVertexNormals();
      const mat = new THREE.MeshStandardMaterial({
        vertexColors: true,
        side: THREE.DoubleSide,
        roughness: 0.55,
        metalness: 0.05,
        transparent: opacity < 1,
        opacity,
      });
      return new THREE.Mesh(geo, mat);
    };
    this.dataGroup.add(mk(top, topColors, 1));
    this.dataGroup.add(mk(wall, wallColors, 0.55));
  }

  private addNoiseLine(seg: RibbonPoint[]): void {
    if (seg.length < 2) return;
    const z = -RIBBON.depth * 1.2;
    const geo = new THREE.BufferGeometry().setFromPoints(seg.map((p) => new THREE.Vector3(p.x, p.y, z)));
    this.dataGroup.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0x868e96 })));
  }

  resize(width: number, height: number): void {
    this.camera.aspect = width / Math.max(1, height);
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
    this.renderer.domElement.style.width = `${width}px`;
    this.renderer.domElement.style.height = `${height}px`;
    this.labelRenderer.setSize(width, height);
    this.dirty = true;
  }

  resetView(): void {
    this.camera.position.set(-2.5, 5.5, 10.5);
    this.controls.target.set(0, RIBBON.height / 2.5, 0);
    this.controls.update();
    this.dirty = true;
  }

  private loop = (): void => {
    if (this.disposed) return;
    this.rafId = requestAnimationFrame(this.loop);
    const moved = this.controls.update();
    if (this.dirty || moved) {
      this.dirty = false;
      this.renderer.render(this.scene, this.camera);
      this.labelRenderer.render(this.scene, this.camera);
    }
  };

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.controls.dispose();
    disposeGroup(this.dataGroup);
    this.scene.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      mesh.geometry?.dispose();
    });
    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.labelRenderer.domElement.remove();
    this.container.replaceChildren();
  }
}
