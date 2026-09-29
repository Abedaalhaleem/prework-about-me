/**
 * Imperative three.js scene for the room view.
 *
 * What is drawn, and what is deliberately NOT drawn:
 *  - Floor, 1 m grid, walls with door gaps, target-room outline, zones and
 *    sensor nodes come from the user's RoomGeometry (or the EXAMPLE one).
 *    Nothing here is reconstructed from Wi-Fi.
 *  - Activity is drawn per LINK only (TX -> RX line coloured by that link's
 *    state). There is no room-wide heatmap, no person, avatar or skeleton.
 *  - A zone is highlighted only when the caller passes an estimate that
 *    passed every gate (see lib/zone.ts). Its centre is a display anchor.
 *  - The render loop only redraws what the caller set. It never animates,
 *    smooths or extrapolates measurements.
 *  - Labels are decluttered in screen space after every redraw (see
 *    lib/declutter.ts): node labels first, then link labels, then the
 *    target-room, zone and compass labels, which are nudged, or hidden when
 *    there is no free spot. Node glyphs are kept uncovered, a link label
 *    only slides along its own line and a zone label never leaves its own
 *    zone, so a moved label cannot be read as naming something else. Moving
 *    a label never moves what it names.
 *
 * Coordinates: backend x east / y north / z up -> three.js (x, z, -y),
 * see lib/coords.ts.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { Line2 } from 'three/addons/lines/Line2.js';
import { LineGeometry } from 'three/addons/lines/LineGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';

import type { NodeRole, RoomGeometry, SensorNode, Vec2, Wall, Zone } from '../api/types';
import { polygonCentroid, roomBounds, toThree, wallLength, wallSpans } from '../lib/coords';
import { type LabelBox, type Offset, type Point, type Rect, declutterLabels } from '../lib/declutter';
import type { LinkDisplayState } from '../lib/freshness';
import { linkStyle } from '../lib/linkStyle';

export type ViewPreset = 'iso' | 'top';

export interface LinkVisual {
  linkId: string;
  state: LinkDisplayState;
  /** Short text for the in-scene label, e.g. "MOTION · 0.3 s". */
  label: string;
}

export interface ZoneHighlight {
  zoneId: string;
  label: string;
}

const COLORS = {
  background: 0x0b0f14,
  floor: 0x131a22,
  gridMajor: 0x3a4656,
  gridMinor: 0x273241,
  wall: 0x9aa7b8,
  wallTarget: 0xb8c6d9,
  doorFrame: 0xe0b050,
  targetOutline: 0xffd43b,
  zoneTarget: 0x748ffc,
  zoneOutside: 0x868e96,
  zoneEstimate: 0xf06595,
  tx: 0xc678dd,
  rx: 0xe9ecef,
  router: 0x69db7c,
  pole: 0x5c6878,
} as const;

const NODE_COLOR: Record<NodeRole, number> = { TX: COLORS.tx, RX: COLORS.rx, ROUTER: COLORS.router };

/** Half the on-screen extent of each node glyph, metres: node labels sit just above it. */
const NODE_GLYPH_RADIUS_M: Record<NodeRole, number> = { TX: 0.16, RX: 0.16, ROUTER: 0.21 };

/** Declutter order and whether a label may be hidden when there is no free spot. */
type LabelKind = 'estimate' | 'node' | 'link' | 'target' | 'zone' | 'compass';
const LABEL_RULES: Record<LabelKind, { priority: number; hideable: boolean }> = {
  estimate: { priority: 0, hideable: false }, // estimated zone + its "not measured" disclaimers
  node: { priority: 1, hideable: false },
  link: { priority: 2, hideable: false },
  target: { priority: 3, hideable: true },
  zone: { priority: 4, hideable: true },
  compass: { priority: 5, hideable: true },
};
/** Points along a link line (fraction of TX -> RX) a link label may slide to, nearest to the middle first. */
const LINK_LABEL_SLIDES = [0.45, 0.55, 0.4, 0.6, 0.35, 0.65, 0.3, 0.7, 0.25, 0.75, 0.2, 0.8, 0.15, 0.85];
const LABEL_GAP_PX = 3;
const CULLED_CLASS = 'scene-label--culled';

interface LabelMeta {
  kind: LabelKind;
  /** Node labels: glyph radius in metres; the label's home is just above the glyph on screen. */
  liftM?: number;
  /** Link labels: world end points, so the label can slide along its own line. */
  along?: [THREE.Vector3, THREE.Vector3];
  /** Zone labels: the zone outline at the label's height; a moved label stays inside it. */
  zonePoly?: THREE.Vector3[];
  /** Offset chosen in the previous declutter pass (null = was hidden / never placed). */
  prev: Offset | null;
}

interface LinkEntry {
  line: Line2;
  material: LineMaterial;
  labelEl: HTMLDivElement;
  state: LinkDisplayState | null;
  text: string;
}

function makeLabel(
  text: string,
  className: string,
  kind: LabelKind,
  extra: Omit<LabelMeta, 'kind' | 'prev'> = {},
): { obj: CSS2DObject; el: HTMLDivElement } {
  const el = document.createElement('div');
  el.className = `scene-label ${className}`;
  el.textContent = text;
  const obj = new CSS2DObject(el);
  const meta: LabelMeta = { kind, prev: null, ...extra };
  obj.userData.label = meta;
  return { obj, el };
}

function disposeObject(root: THREE.Object3D): void {
  root.traverse((obj) => {
    const mesh = obj as THREE.Mesh;
    if (mesh.geometry) mesh.geometry.dispose();
    const mat = (mesh as { material?: THREE.Material | THREE.Material[] }).material;
    if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
    else if (mat) mat.dispose();
  });
}

function clearGroup(group: THREE.Group): void {
  // Removing CSS2DObjects from the graph also removes their DOM elements
  // (CSS2DObject listens for 'removed').
  const children = [...group.children];
  children.forEach((c) => {
    disposeObject(c);
    group.remove(c);
    c.traverse((o) => {
      if (o instanceof CSS2DObject) o.element.remove();
    });
  });
}

/** THREE.Shape in the backend (x, y) plane; rotateX(-PI/2) maps it to (x, 0, -y). */
function shapeFrom(poly: readonly Vec2[]): THREE.Shape {
  return new THREE.Shape(poly.map((p) => new THREE.Vector2(p.x, p.y)));
}

function floorLoop(poly: readonly Vec2[], y: number): THREE.Vector3[] {
  return poly.map((p) => {
    const [x, , z] = toThree(p);
    return new THREE.Vector3(x, y, z);
  });
}

export class RoomScene {
  private readonly renderer: THREE.WebGLRenderer;
  private readonly labelRenderer: CSS2DRenderer;
  private readonly scene = new THREE.Scene();
  private readonly perspCamera: THREE.PerspectiveCamera;
  private readonly orthoCamera: THREE.OrthographicCamera;
  private camera: THREE.Camera;
  private controls: OrbitControls;
  private preset: ViewPreset = 'iso';

  private readonly roomGroup = new THREE.Group();
  private readonly linkGroup = new THREE.Group();
  private readonly highlightGroup = new THREE.Group();
  private wallMaterials: THREE.MeshStandardMaterial[] = [];
  private wallOpacity = 0.35;

  private room: RoomGeometry | null = null;
  private roomKey = '';
  private links = new Map<string, LinkEntry>();
  private lastVisuals: LinkVisual[] = [];
  private highlight: ZoneHighlight | null = null;
  private extrude = false;

  private center = new THREE.Vector3(0, 0, 0);
  private span = 6;
  private width = 1;
  private height = 1;
  private rafId: number | null = null;
  private dirty = true;
  private disposed = false;
  private renderCount = 0;
  private fpsWindowStart = performance.now();
  private onRenderRate: ((perSecond: number) => void) | null = null;

  private readonly container: HTMLElement;
  private labelObstacles: readonly HTMLElement[] = [];
  private onLabelsCulled: ((hidden: number) => void) | null = null;
  private culledCount = -1;
  private readonly viewProj = new THREE.Matrix4();
  private readonly tmpA = new THREE.Vector3();
  private readonly tmpB = new THREE.Vector3();
  private readonly tmpC = new THREE.Vector3();

  constructor(container: HTMLElement) {
    this.container = container;
    // May throw when WebGL is unavailable; RoomView catches and says so.
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(COLORS.background, 1);
    this.renderer.domElement.className = 'scene-canvas';
    this.renderer.domElement.setAttribute('aria-label', '3-D room model (drag to rotate, scroll to zoom, right-drag to pan)');
    this.renderer.domElement.setAttribute('role', 'img');
    container.appendChild(this.renderer.domElement);

    this.labelRenderer = new CSS2DRenderer();
    this.labelRenderer.domElement.className = 'scene-labels';
    container.appendChild(this.labelRenderer.domElement);

    this.perspCamera = new THREE.PerspectiveCamera(45, 1, 0.05, 500);
    this.orthoCamera = new THREE.OrthographicCamera(-5, 5, 5, -5, 0.05, 500);
    this.camera = this.perspCamera;
    this.controls = this.makeControls();

    this.scene.add(new THREE.HemisphereLight(0xdde6f5, 0x1a1f26, 1.4));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(6, 12, 8);
    this.scene.add(sun);
    this.scene.add(this.roomGroup, this.linkGroup, this.highlightGroup);

    const rect = container.getBoundingClientRect();
    this.resize(Math.max(1, rect.width), Math.max(1, rect.height));
    this.applyPreset('iso');
    this.loop();
  }

  /** Called with the number of redraws per second (display only). */
  setRenderRateListener(fn: ((perSecond: number) => void) | null): void {
    this.onRenderRate = fn;
  }

  /** Called with the number of labels hidden to avoid overlaps, whenever it changes. */
  setLabelCullListener(fn: ((hidden: number) => void) | null): void {
    this.onLabelsCulled = fn;
    this.culledCount = -1;
    this.dirty = true;
  }

  /**
   * Overlay elements drawn on top of the view (geometry label, footer notes).
   * Labels are kept off them. Call invalidateLabels() when they change size.
   */
  setLabelObstacles(elements: readonly HTMLElement[]): void {
    this.labelObstacles = elements;
    this.dirty = true;
  }

  /** Re-run label placement on the next frame. */
  invalidateLabels(): void {
    this.dirty = true;
  }

  // ---------------------------------------------------------------- setup

  private makeControls(): OrbitControls {
    const c = new OrbitControls(this.camera, this.renderer.domElement);
    c.enableDamping = true;
    c.dampingFactor = 0.12;
    c.screenSpacePanning = true;
    c.maxPolarAngle = Math.PI / 2 - 0.02; // never look from under the floor
    c.minDistance = 0.5;
    c.maxDistance = 200;
    c.addEventListener('change', () => {
      this.dirty = true;
    });
    return c;
  }

  private switchCamera(cam: THREE.Camera): void {
    if (this.camera === cam) return;
    const target = this.controls.target.clone();
    this.controls.dispose();
    this.camera = cam;
    this.controls = this.makeControls();
    this.controls.target.copy(target);
  }

  resize(width: number, height: number): void {
    if (this.disposed) return;
    this.width = Math.max(1, Math.floor(width));
    this.height = Math.max(1, Math.floor(height));
    this.renderer.setSize(this.width, this.height, false);
    this.renderer.domElement.style.width = `${this.width}px`;
    this.renderer.domElement.style.height = `${this.height}px`;
    this.labelRenderer.setSize(this.width, this.height);
    this.perspCamera.aspect = this.width / this.height;
    this.perspCamera.updateProjectionMatrix();
    this.fitOrtho();
    this.links.forEach((l) => l.material.resolution.set(this.width, this.height));
    this.highlightGroup.traverse((o) => {
      if (o instanceof Line2) (o.material as LineMaterial).resolution.set(this.width, this.height);
    });
    this.dirty = true;
  }

  private fitOrtho(): void {
    const aspect = this.width / this.height;
    const half = (this.span / 2) * 1.15;
    const halfH = Math.max(half, half / aspect);
    const halfW = halfH * aspect;
    this.orthoCamera.left = -halfW;
    this.orthoCamera.right = halfW;
    this.orthoCamera.top = halfH;
    this.orthoCamera.bottom = -halfH;
    this.orthoCamera.updateProjectionMatrix();
  }

  // ---------------------------------------------------------------- views

  setViewPreset(preset: ViewPreset): void {
    this.applyPreset(preset);
  }

  resetView(): void {
    this.applyPreset(this.preset);
  }

  private applyPreset(preset: ViewPreset): void {
    this.preset = preset;
    const c = this.center;
    const d = this.span * 1.25 + 1;
    if (preset === 'top') {
      // Straight down with north (-Z) at the top of the screen: a tiny +Z
      // offset makes lookAt() resolve screen-up to -Z without changing
      // camera.up (which OrbitControls uses as its orbit axis). Rotation is
      // disabled so the plan view stays a plan view; pan/zoom still work.
      this.orthoCamera.up.set(0, 1, 0);
      this.orthoCamera.position.set(c.x, 60, c.z + 1e-3);
      this.orthoCamera.zoom = 1;
      this.fitOrtho();
      this.switchCamera(this.orthoCamera);
      this.controls.target.set(c.x, 0, c.z);
      this.controls.enableRotate = false;
    } else {
      this.perspCamera.up.set(0, 1, 0);
      // From the south-east (backend +x, -y), looking down at ~35 degrees.
      this.perspCamera.position.set(c.x + d * 0.75, d * 0.8, c.z + d * 0.85);
      this.switchCamera(this.perspCamera);
      this.controls.target.set(c.x, 0.4, c.z);
      this.controls.enableRotate = true;
    }
    this.camera.lookAt(this.controls.target);
    this.controls.update();
    this.dirty = true;
  }

  // ---------------------------------------------------------------- room

  setRoom(room: RoomGeometry | null): void {
    const key = room ? JSON.stringify(room) : '';
    if (key === this.roomKey) return;
    const firstRoom = this.roomKey === '';
    this.roomKey = key;
    this.room = room;
    clearGroup(this.roomGroup);
    this.wallMaterials = [];
    if (room) this.buildRoom(room);
    this.fitOrtho();
    this.rebuildLinks();
    this.rebuildHighlight();
    if (firstRoom) this.applyPreset(this.preset);
    this.dirty = true;
  }

  private buildRoom(room: RoomGeometry): void {
    const b = roomBounds(room);
    const pad = 0.75;
    const minX = b.minX - pad;
    const maxX = b.maxX + pad;
    const minY = b.minY - pad;
    const maxY = b.maxY + pad;
    const w = maxX - minX;
    const d = maxY - minY;
    const [cx, , cz] = toThree({ x: (minX + maxX) / 2, y: (minY + maxY) / 2 });
    this.center.set(cx, 0, cz);
    this.span = Math.max(w, d, room.height_m, 2);

    // Floor
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d),
      new THREE.MeshStandardMaterial({ color: COLORS.floor, roughness: 0.95, metalness: 0 }),
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(cx, 0, cz);
    this.roomGroup.add(floor);

    // 1 m grid; an even size centred on whole metres puts lines on integer coordinates.
    let size = Math.ceil(Math.max(w, d)) + 2;
    if (size % 2 === 1) size += 1;
    const grid = new THREE.GridHelper(size, size, COLORS.gridMajor, COLORS.gridMinor);
    grid.position.set(Math.round(cx), 0.002, Math.round(cz));
    this.roomGroup.add(grid);

    // North marker at the far (north) edge.
    const [nx, , nz] = toThree({ x: (minX + maxX) / 2, y: maxY });
    const north = makeLabel('N ↑ (grid 1 m)', 'scene-label--compass', 'compass');
    north.obj.position.set(nx, 0.05, nz);
    this.roomGroup.add(north.obj);

    room.walls.forEach((wall) => this.buildWall(wall, room));
    this.buildTargetOutline(room.target_room_polygon);
    room.zones.forEach((z) => this.buildZone(z));
    room.nodes.forEach((n) => this.buildNode(n));
  }

  private buildWall(wall: Wall, room: RoomGeometry): void {
    const len = wallLength(wall);
    if (len < 1e-3) return;
    const dx = wall.end.x - wall.start.x;
    const dy = wall.end.y - wall.start.y;
    const angle = Math.atan2(dy, dx); // rotation about three.js +Y (see coords.ts)
    const mat = new THREE.MeshStandardMaterial({
      color: wall.is_target_room_boundary ? COLORS.wallTarget : COLORS.wall,
      transparent: true,
      opacity: this.wallOpacity,
      depthWrite: this.wallOpacity > 0.95,
      roughness: 0.8,
      side: THREE.DoubleSide,
    });
    this.wallMaterials.push(mat);
    const spans = wallSpans(wall, room.doors);
    const addPiece = (from: number, to: number, y0: number, y1: number): void => {
      if (to - from < 1e-3 || y1 - y0 < 1e-3) return;
      const mid = (from + to) / 2;
      const px = wall.start.x + (dx * mid) / len;
      const py = wall.start.y + (dy * mid) / len;
      const [x, , z] = toThree({ x: px, y: py });
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(to - from, y1 - y0, wall.thickness_m), mat);
      mesh.position.set(x, (y0 + y1) / 2, z);
      mesh.rotation.y = angle;
      this.roomGroup.add(mesh);
    };
    spans.solid.forEach(([a, bb]) => addPiece(a, bb, 0, wall.height_m));
    spans.openings.forEach((o) => {
      // Lintel above the door, then an amber frame so the gap reads as a door.
      addPiece(o.from, o.to, o.height, wall.height_m);
      const p = (dist: number, h: number): THREE.Vector3 => {
        const [x, , z] = toThree({ x: wall.start.x + (dx * dist) / len, y: wall.start.y + (dy * dist) / len });
        return new THREE.Vector3(x, h, z);
      };
      const frame = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([p(o.from, 0), p(o.from, o.height), p(o.to, o.height), p(o.to, 0)]),
        new THREE.LineBasicMaterial({ color: COLORS.doorFrame }),
      );
      this.roomGroup.add(frame);
    });
  }

  private buildTargetOutline(poly: readonly Vec2[]): void {
    if (poly.length < 3) return;
    const pts = floorLoop(poly, 0.02);
    const loop = new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: COLORS.targetOutline }),
    );
    this.roomGroup.add(loop);
    const first = pts[0];
    if (first) {
      const label = makeLabel('Target room', 'scene-label--target', 'target');
      label.obj.position.set(first.x, 0.1, first.z);
      this.roomGroup.add(label.obj);
    }
  }

  private buildZone(zone: Zone): void {
    if (zone.polygon.length < 3) return;
    const outside = zone.kind === 'OUTSIDE_TARGET_ROOM';
    const color = outside ? COLORS.zoneOutside : COLORS.zoneTarget;
    const geom = new THREE.ShapeGeometry(shapeFrom(zone.polygon));
    geom.rotateX(-Math.PI / 2);
    const mesh = new THREE.Mesh(
      geom,
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: outside ? 0.07 : 0.12, depthWrite: false, side: THREE.DoubleSide }),
    );
    mesh.position.y = 0.006;
    this.roomGroup.add(mesh);
    const outline = new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(floorLoop(zone.polygon, 0.012)),
      new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.7 }),
    );
    this.roomGroup.add(outline);
    const c = polygonCentroid(zone.polygon);
    if (c) {
      const [x, , z] = toThree(c);
      const label = makeLabel(outside ? `${zone.label} (outside target room)` : zone.label, 'scene-label--zone', 'zone', {
        zonePoly: floorLoop(zone.polygon, 0.05),
      });
      label.obj.position.set(x, 0.05, z);
      this.roomGroup.add(label.obj);
    }
  }

  private buildNode(node: SensorNode): void {
    const color = NODE_COLOR[node.role] ?? COLORS.rx;
    const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.4, metalness: 0.1, emissive: color, emissiveIntensity: 0.15 });
    let geom: THREE.BufferGeometry;
    switch (node.role) {
      case 'TX':
        geom = new THREE.ConeGeometry(0.13, 0.3, 4); // pyramid
        break;
      case 'ROUTER':
        geom = new THREE.CylinderGeometry(0.2, 0.2, 0.07, 24); // puck
        break;
      default:
        geom = new THREE.BoxGeometry(0.22, 0.22, 0.22); // cube
    }
    const mesh = new THREE.Mesh(geom, mat);
    const [x, y, z] = toThree(node.position);
    mesh.position.set(x, y, z);
    this.roomGroup.add(mesh);

    // A thin pole to the floor makes the user-measured mounting height readable.
    if (y > 0.05) {
      const pole = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(x, 0, z), new THREE.Vector3(x, y, z)]),
        new THREE.LineDashedMaterial({ color: COLORS.pole, dashSize: 0.08, gapSize: 0.06 }),
      );
      pole.computeLineDistances();
      this.roomGroup.add(pole);
    }
    const where = node.inside_target_room === null ? '' : node.inside_target_room ? ' · inside' : ' · outside';
    // Anchored at the node itself; the declutter pass puts the label just
    // above the glyph in screen space, so it never covers the glyph (also in
    // the top-down view, where a world-space lift would collapse to nothing).
    const label = makeLabel(`${node.role} ${node.label}${where}`, `scene-label--node scene-label--${node.role.toLowerCase()}`, 'node', {
      liftM: NODE_GLYPH_RADIUS_M[node.role] ?? 0.16,
    });
    label.obj.position.set(x, y, z);
    this.roomGroup.add(label.obj);
  }

  setWallOpacity(opacity: number): void {
    this.wallOpacity = Math.min(1, Math.max(0, opacity));
    this.wallMaterials.forEach((m) => {
      m.opacity = this.wallOpacity;
      m.depthWrite = this.wallOpacity > 0.95;
      m.visible = this.wallOpacity > 0.01;
      m.needsUpdate = true;
    });
    this.dirty = true;
  }

  // ---------------------------------------------------------------- links

  private rebuildLinks(): void {
    clearGroup(this.linkGroup);
    this.links.clear();
    const room = this.room;
    if (!room) return;
    const nodes = new Map(room.nodes.map((n) => [n.id, n]));
    room.links.forEach((def) => {
      const tx = nodes.get(def.transmitter_id);
      const rx = nodes.get(def.receiver_id);
      if (!tx || !rx) return;
      const a = toThree(tx.position);
      const b = toThree(rx.position);
      const geom = new LineGeometry();
      geom.setPositions([...a, ...b]);
      const material = new LineMaterial({
        color: 0xffffff,
        linewidth: 2,
        worldUnits: false,
        dashed: false,
        dashSize: 0.18,
        gapSize: 0.12,
        transparent: true,
        opacity: 1,
      });
      material.resolution.set(this.width, this.height);
      const line = new Line2(geom, material);
      line.computeLineDistances();
      this.linkGroup.add(line);
      const { obj, el } = makeLabel(def.link_id, 'scene-label--link', 'link', {
        along: [new THREE.Vector3(...a), new THREE.Vector3(...b)],
      });
      obj.position.set((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2);
      this.linkGroup.add(obj);
      this.links.set(def.link_id, { line, material, labelEl: el, state: null, text: '' });
    });
    this.applyLinkVisuals();
  }

  /** Update per-link colours. Links without a visual are drawn as NO_DATA. */
  setLinks(visuals: LinkVisual[]): void {
    this.lastVisuals = visuals;
    this.applyLinkVisuals();
  }

  private applyLinkVisuals(): void {
    const byId = new Map(this.lastVisuals.map((v) => [v.linkId, v]));
    this.links.forEach((entry, id) => {
      const v = byId.get(id);
      const state: LinkDisplayState = v?.state ?? 'NO_DATA';
      const text = `${id} · ${v?.label ?? 'NO DATA'}`;
      if (entry.state !== state) {
        const st = linkStyle(state);
        entry.material.color.setHex(st.color);
        entry.material.linewidth = st.widthPx;
        entry.material.dashed = st.dashed;
        entry.material.opacity = st.opacity;
        entry.material.needsUpdate = true;
        if (entry.state) entry.labelEl.classList.remove(`scene-label--state-${entry.state.toLowerCase()}`);
        entry.labelEl.classList.add(`scene-label--state-${state.toLowerCase()}`);
        entry.state = state;
        this.dirty = true;
      }
      if (entry.text !== text) {
        entry.labelEl.textContent = text;
        entry.text = text;
        this.dirty = true;
      }
    });
  }

  // ---------------------------------------------------------------- zone

  /** Pass null unless the estimate passed every gate (lib/zone.ts). */
  setZoneHighlight(highlight: ZoneHighlight | null, extrude: boolean): void {
    const same =
      extrude === this.extrude &&
      ((highlight === null && this.highlight === null) ||
        (highlight !== null && this.highlight !== null && highlight.zoneId === this.highlight.zoneId && highlight.label === this.highlight.label));
    if (same) return;
    this.highlight = highlight;
    this.extrude = extrude;
    this.rebuildHighlight();
  }

  private rebuildHighlight(): void {
    clearGroup(this.highlightGroup);
    this.dirty = true;
    const h = this.highlight;
    const zone = h && this.room ? this.room.zones.find((z) => z.id === h.zoneId) : undefined;
    if (!h || !zone || zone.polygon.length < 3) return;

    const geom = new THREE.ShapeGeometry(shapeFrom(zone.polygon));
    geom.rotateX(-Math.PI / 2);
    const fill = new THREE.Mesh(
      geom,
      new THREE.MeshBasicMaterial({ color: COLORS.zoneEstimate, transparent: true, opacity: 0.32, depthWrite: false, side: THREE.DoubleSide }),
    );
    fill.position.y = 0.015;
    this.highlightGroup.add(fill);

    const pts = floorLoop(zone.polygon, 0.02);
    const first = pts[0];
    if (first) pts.push(first.clone());
    const og = new LineGeometry();
    og.setPositions(pts.flatMap((p) => [p.x, p.y, p.z]));
    const om = new LineMaterial({ color: COLORS.zoneEstimate, linewidth: 4, worldUnits: false });
    om.resolution.set(this.width, this.height);
    this.highlightGroup.add(new Line2(og, om));

    const c = polygonCentroid(zone.polygon);
    if (c) {
      const [x, , z] = toThree(c);
      const el = document.createElement('div');
      el.className = 'scene-label scene-label--estimate';
      const title = document.createElement('strong');
      title.textContent = `Estimated zone (experimental): ${h.label}`;
      const note = document.createElement('span');
      note.textContent = 'Zone centre is a display anchor, not a measured position.';
      el.append(title, note);
      const obj = new CSS2DObject(el);
      const labelY = this.extrude ? 1.35 : 0.3;
      const meta: LabelMeta = { kind: 'estimate', prev: null, zonePoly: floorLoop(zone.polygon, labelY) };
      obj.userData.label = meta;
      obj.position.set(x, labelY, z);
      this.highlightGroup.add(obj);
    }

    if (this.extrude) {
      // Decorative only: a fixed-height prism. The height is NOT measured.
      const eg = new THREE.ExtrudeGeometry(shapeFrom(zone.polygon), { depth: 1.2, bevelEnabled: false });
      eg.rotateX(-Math.PI / 2);
      const prism = new THREE.Mesh(
        eg,
        new THREE.MeshBasicMaterial({ color: COLORS.zoneEstimate, transparent: true, opacity: 0.12, depthWrite: false, side: THREE.DoubleSide }),
      );
      this.highlightGroup.add(prism);
      if (c) {
        const [x, , z] = toThree(c);
        const lbl = makeLabel('Visualization only — not measured height', 'scene-label--decorative', 'estimate');
        lbl.obj.position.set(x, 0.75, z);
        this.highlightGroup.add(lbl.obj);
      }
    }
  }

  // ---------------------------------------------------------------- loop

  private loop = (): void => {
    if (this.disposed) return;
    this.rafId = requestAnimationFrame(this.loop);
    // controls.update() applies damping; it returns true while the camera moves.
    const moved = this.controls.update();
    if (this.dirty || moved) {
      this.dirty = false;
      this.renderer.render(this.scene, this.camera);
      this.labelRenderer.render(this.scene, this.camera);
      this.declutter();
      this.renderCount++;
    }
    const now = performance.now();
    if (now - this.fpsWindowStart >= 1000) {
      this.onRenderRate?.((this.renderCount * 1000) / (now - this.fpsWindowStart));
      this.renderCount = 0;
      this.fpsWindowStart = now;
    }
  };

  // ---------------------------------------------------------------- labels

  /** Screen position (CSS px in the view) of a world point, or null behind the camera. */
  private project(world: THREE.Vector3, out: THREE.Vector3): { x: number; y: number } | null {
    out.copy(world).applyMatrix4(this.viewProj);
    if (!(out.z >= -1 && out.z <= 1)) return null;
    return { x: (out.x + 1) * (this.width / 2), y: (1 - out.y) * (this.height / 2) };
  }

  /** On-screen pixels per metre at a world point (works for both cameras). */
  private pxPerMetre(world: THREE.Vector3, at: { x: number; y: number }): number {
    const right = this.tmpB.set(1, 0, 0).applyQuaternion(this.camera.quaternion).add(world);
    const p = this.project(right, this.tmpB);
    return p ? Math.hypot(p.x - at.x, p.y - at.y) : 0;
  }

  private obstacleRects(): Rect[] {
    if (this.labelObstacles.length === 0) return [];
    const base = this.container.getBoundingClientRect();
    const out: Rect[] = [];
    for (const el of this.labelObstacles) {
      if (!el.isConnected) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      out.push({ left: r.left - base.left, top: r.top - base.top, right: r.right - base.left, bottom: r.bottom - base.top });
    }
    return out;
  }

  /**
   * Runs right after labelRenderer.render(): measures every visible label,
   * projects its anchor the same way CSS2DRenderer does, and overrides the
   * transform with the decluttered position (or hides the label).
   */
  private declutter(): void {
    this.viewProj.multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse);
    const items: { el: HTMLElement; meta: LabelMeta; box: LabelBox }[] = [];
    const glyphs: Rect[] = []; // node glyphs stay uncovered
    const world = new THREE.Vector3();
    this.scene.traverse((o) => {
      if (!(o instanceof CSS2DObject)) return;
      const meta = o.userData.label as LabelMeta | undefined;
      const el = o.element;
      // display:none = culled by CSS2DRenderer (behind the camera / hidden group).
      if (!meta || el.style.display === 'none') return;
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w <= 0 || h <= 0) return;
      world.setFromMatrixPosition(o.matrixWorld);
      const anchor = this.project(world, this.tmpA);
      if (!anchor) return;
      let { x, y } = anchor;
      if (meta.kind === 'node') {
        const r = this.pxPerMetre(world, anchor) * (meta.liftM ?? 0);
        glyphs.push({ left: x - r, top: y - r, right: x + r, bottom: y + r });
        y -= h / 2 + r + LABEL_GAP_PX + 1; // just above the glyph
      }
      let region: Point[] | undefined;
      const extra: Offset[] = [];
      if (meta.zonePoly) {
        region = [];
        for (const v of meta.zonePoly) {
          const p = this.project(v, this.tmpB);
          if (!p) {
            region = []; // outline partly behind the camera: home position only
            break;
          }
          region.push(p);
        }
        // More room inside the zone: points halfway from the anchor to each
        // edge midpoint and each corner, nearest first.
        const poly = meta.zonePoly;
        const inside: Offset[] = [];
        poly.forEach((a, i) => {
          const b = poly[(i + 1) % poly.length] as THREE.Vector3;
          for (const target of [this.tmpC.copy(a).add(b).multiplyScalar(0.5), a]) {
            const p = this.project(this.tmpB.copy(world).lerp(target, 0.5), this.tmpB);
            if (p) inside.push({ dx: p.x - x, dy: p.y - y });
          }
        });
        inside.sort((u, v) => Math.hypot(u.dx, u.dy) - Math.hypot(v.dx, v.dy));
        extra.push(...inside);
      }
      if (meta.along) {
        const [a, b] = meta.along;
        // Allowed area: a thin band around the line (away from the nodes), so a
        // moved link label is never read as belonging to a neighbouring line.
        const q0 = this.project(this.tmpB.copy(a).lerp(b, 0.12), this.tmpB);
        const q1 = this.project(this.tmpC.copy(a).lerp(b, 0.88), this.tmpC);
        const len = q0 && q1 ? Math.hypot(q1.x - q0.x, q1.y - q0.y) : 0;
        if (q0 && q1 && len > 1) {
          const ux = -(q1.y - q0.y) / len; // unit normal of the line on screen
          const uy = (q1.x - q0.x) / len;
          const half = h / 2 + 2;
          region = [
            { x: q0.x + ux * half, y: q0.y + uy * half },
            { x: q1.x + ux * half, y: q1.y + uy * half },
            { x: q1.x - ux * half, y: q1.y - uy * half },
            { x: q0.x - ux * half, y: q0.y - uy * half },
          ];
          // Slide along the line first; then sit just beside it (still touching it).
          const along: Point[] = [];
          for (const t of LINK_LABEL_SLIDES) {
            const p = this.project(this.tmpB.copy(a).lerp(b, t), this.tmpB);
            if (p) along.push(p);
          }
          along.forEach((p) => extra.push({ dx: p.x - x, dy: p.y - y }));
          for (const side of [1, -1]) {
            along.forEach((p) => extra.push({ dx: p.x + side * ux * (h / 2) - x, dy: p.y + side * uy * (h / 2) - y }));
          }
        } else {
          region = []; // line seen end-on: home position only
        }
      }
      const rule = LABEL_RULES[meta.kind];
      items.push({
        el,
        meta,
        box: {
          id: String(items.length),
          x,
          y,
          w,
          h,
          priority: rule.priority,
          hideable: rule.hideable,
          prev: meta.prev,
          extra,
          ...(region ? { region } : {}),
        },
      });
    });

    const placements = declutterLabels(
      items.map((i) => i.box),
      { width: this.width, height: this.height, gap: LABEL_GAP_PX, obstacles: [...this.obstacleRects(), ...glyphs] },
    );
    let hidden = 0;
    placements.forEach((pl, k) => {
      const item = items[k];
      if (!item) return;
      item.meta.prev = pl.hidden ? null : { dx: pl.dx, dy: pl.dy };
      item.el.classList.toggle(CULLED_CLASS, pl.hidden);
      if (pl.hidden) {
        hidden++;
        return;
      }
      const x = item.box.x + pl.dx;
      const y = item.box.y + pl.dy;
      item.el.style.transform = `translate(-50%, -50%) translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    });
    if (hidden !== this.culledCount) {
      this.culledCount = hidden;
      this.onLabelsCulled?.(hidden);
    }
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.controls.dispose();
    clearGroup(this.roomGroup);
    clearGroup(this.linkGroup);
    clearGroup(this.highlightGroup);
    disposeObject(this.scene);
    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.labelRenderer.domElement.remove();
    this.links.clear();
    this.onRenderRate = null;
    this.onLabelsCulled = null;
    this.labelObstacles = [];
  }
}
