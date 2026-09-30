// Sky, lighting, distant megacity skyline and flying traffic.
import * as THREE from 'three';
import * as TX from './textures.js';

const SUN_DIR = new THREE.Vector3(-0.75, 0.28, 0.45).normalize();

function skyMaterial() {
  return new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    fog: false,
    uniforms: {
      uSun: { value: SUN_DIR.clone() },
      uTime: { value: 0 },
      uFlash: { value: 0 },
    },
    vertexShader: /* glsl */`
      varying vec3 vDir;
      void main() {
        vDir = normalize(position);
        vec4 p = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        gl_Position = p.xyww;
      }`,
    fragmentShader: /* glsl */`
      varying vec3 vDir;
      uniform vec3 uSun;
      uniform float uTime;
      uniform float uFlash;
      float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
      float noise(vec2 p) {
        vec2 i = floor(p), f = fract(p);
        f = f * f * (3.0 - 2.0 * f);
        return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
      }
      float fbm(vec2 p) { float v = 0.0, a = 0.5; for (int i = 0; i < 5; i++) { v += a * noise(p); p *= 2.03; a *= 0.5; } return v; }
      void main() {
        vec3 d = normalize(vDir);
        float h = d.y;
        vec3 horizon = vec3(1.25, 0.42, 0.18);
        vec3 mid = vec3(0.42, 0.10, 0.38);
        vec3 top = vec3(0.02, 0.03, 0.10);
        vec3 col = mix(horizon, mid, smoothstep(0.0, 0.22, h));
        col = mix(col, top, smoothstep(0.18, 0.75, h));
        col = mix(col, vec3(0.16, 0.08, 0.10), smoothstep(0.0, -0.15, h));
        float sd = max(dot(d, uSun), 0.0);
        col += vec3(1.6, 0.7, 0.3) * pow(sd, 8.0) * 0.8;
        col += vec3(4.0, 2.4, 1.2) * smoothstep(0.9975, 0.9990, sd);
        // streaky dusk clouds
        vec2 cp = d.xz / max(h + 0.08, 0.02);
        float c = fbm(cp * vec2(0.35, 1.4) + vec2(uTime * 0.004, 0.0));
        float cm = smoothstep(0.52, 0.8, c) * smoothstep(0.02, 0.12, h) * (1.0 - smoothstep(0.35, 0.7, h));
        vec3 cloudCol = mix(vec3(0.9, 0.35, 0.3), vec3(1.8, 0.9, 0.5), pow(sd, 3.0));
        col = mix(col, cloudCol, cm * 0.75);
        // stars
        vec2 sp = d.xz / (h + 1.0) * 220.0;
        float st = step(0.9965, hash(floor(sp))) * smoothstep(0.35, 0.8, h);
        col += vec3(st) * (0.6 + 0.4 * sin(uTime * 3.0 + hash(floor(sp)) * 40.0));
        col += vec3(3.0, 2.6, 2.2) * uFlash;
        gl_FragColor = vec4(col, 1.0);
      }`,
  });
}

export function buildEnvironment(scene, renderer, quality) {
  // --- Sky dome ---
  const skyMat = skyMaterial();
  const sky = new THREE.Mesh(new THREE.SphereGeometry(600, 32, 16), skyMat);
  sky.frustumCulled = false;
  sky.renderOrder = -10;
  scene.add(sky);

  scene.fog = new THREE.FogExp2(new THREE.Color(0.2, 0.09, 0.14), 0.0042);

  // --- Environment map from a copy of the sky (reflections on metal/glass) ---
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envScene = new THREE.Scene();
  envScene.add(new THREE.Mesh(new THREE.SphereGeometry(50, 32, 16), skyMaterial()));
  const envRT = pmrem.fromScene(envScene, 0.02);
  scene.environment = envRT.texture;
  scene.environmentIntensity = 0.55;

  // --- Lights ---
  const hemi = new THREE.HemisphereLight(new THREE.Color('#7f8fff'), new THREE.Color('#40241c'), 0.9);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(new THREE.Color('#ffb27a'), 2.6);
  sun.position.copy(SUN_DIR).multiplyScalar(80);
  sun.target.position.set(0, 0, 0);
  sun.castShadow = quality !== 'low';
  const ss = quality === 'high' ? 4096 : 2048;
  sun.shadow.mapSize.set(ss, ss);
  const sc = sun.shadow.camera;
  sc.left = -48; sc.right = 48; sc.top = 42; sc.bottom = -42; sc.near = 10; sc.far = 200;
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.03;
  scene.add(sun);
  scene.add(sun.target);
  // cool rim light from the opposite side (city glow)
  const rim = new THREE.DirectionalLight(new THREE.Color('#5a6cff'), 0.6);
  rim.position.set(40, 30, -40);
  scene.add(rim);

  // --- Skyline: instanced towers with emissive windows ---
  const winTex = TX.skylineWindows();
  winTex.wrapS = winTex.wrapT = THREE.RepeatWrapping;
  const towerMat = new THREE.MeshStandardMaterial({
    color: '#11131c', roughness: 0.6, metalness: 0.4,
    emissive: new THREE.Color(1.6, 1.4, 1.5), emissiveMap: winTex,
  });
  const towerGeo = new THREE.BoxGeometry(1, 1, 1);
  towerGeo.translate(0, 0.5, 0);
  const N = 140;
  const towers = new THREE.InstancedMesh(towerGeo, towerMat, N);
  const m = new THREE.Matrix4();
  const q = new THREE.Quaternion();
  const beacons = [];
  let k = 0;
  for (let i = 0; i < N; i++) {
    const a = TX.rand() * Math.PI * 2;
    // Keep the sunset side (west) lower so the sun stays visible
    const west = Math.max(0, -Math.cos(a - Math.atan2(SUN_DIR.z, SUN_DIR.x) + Math.PI));
    const r = 150 + TX.rand() * 170;
    const w = 10 + TX.rand() * 22;
    const h = (30 + TX.rand() * 130) * (1 - west * 0.7);
    q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), TX.rand() * Math.PI);
    m.compose(new THREE.Vector3(Math.cos(a) * r, -2, Math.sin(a) * r), q, new THREE.Vector3(w, h, w * (0.6 + TX.rand() * 0.8)));
    towers.setMatrixAt(k++, m);
    if (h > 90) beacons.push(new THREE.Vector3(Math.cos(a) * r, h, Math.sin(a) * r));
  }
  towers.frustumCulled = false;
  scene.add(towers);

  // Neon crowns on the tallest towers
  const crownMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(2.5, 0.3, 2.0), fog: false });
  const crownMat2 = new THREE.MeshBasicMaterial({ color: new THREE.Color(0.3, 2.2, 3.0), fog: false });
  const beaconMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(4, 0.2, 0.1), fog: false });
  const crownGeo = new THREE.TorusGeometry(6, 0.35, 6, 24);
  crownGeo.rotateX(Math.PI / 2);
  const crownsA = new THREE.InstancedMesh(crownGeo, crownMat, beacons.length);
  const crownsB = new THREE.InstancedMesh(crownGeo, crownMat2, beacons.length);
  const beaconInst = new THREE.InstancedMesh(new THREE.SphereGeometry(1.1, 8, 6), beaconMat, beacons.length);
  let na = 0, nb = 0;
  const im = new THREE.Matrix4();
  beacons.forEach((b, i) => {
    im.makeTranslation(b.x, b.y - 6, b.z);
    if (i % 2) crownsA.setMatrixAt(na++, im); else crownsB.setMatrixAt(nb++, im);
    im.makeTranslation(b.x, b.y + 3, b.z);
    beaconInst.setMatrixAt(i, im);
  });
  crownsA.count = na;
  crownsB.count = nb;
  for (const m of [crownsA, crownsB, beaconInst]) { m.frustumCulled = false; scene.add(m); }
  const beaconMeshes = [beaconInst];

  // Mountains ring
  const pts = 90;
  const mtnGeoPos = [];
  for (let i = 0; i <= pts; i++) {
    const a = (i / pts) * Math.PI * 2;
    const h = 30 + Math.sin(a * 3) * 18 + Math.sin(a * 7.3) * 10 + TX.rand() * 14;
    mtnGeoPos.push([Math.cos(a) * 480, h, Math.sin(a) * 480]);
  }
  const mg = new THREE.BufferGeometry();
  const verts = [];
  for (let i = 0; i < pts; i++) {
    const [x0, h0, z0] = mtnGeoPos[i], [x1, h1, z1] = mtnGeoPos[i + 1];
    verts.push(x0, -5, z0, x1, -5, z1, x1, h1, z1, x0, -5, z0, x1, h1, z1, x0, h0, z0);
  }
  mg.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
  const mtnMesh = new THREE.Mesh(mg, new THREE.MeshBasicMaterial({ color: new THREE.Color(0.1, 0.035, 0.07), side: THREE.DoubleSide, fog: false }));
  scene.add(mtnMesh);

  // --- Flying traffic ---
  const carGeo = new THREE.CapsuleGeometry(0.8, 2.6, 4, 8);
  carGeo.rotateZ(Math.PI / 2);
  const carMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(3, 2.6, 2.2), fog: false });
  const carMat2 = new THREE.MeshBasicMaterial({ color: new THREE.Color(3.5, 0.3, 0.2), fog: false });
  const lanes = [];
  const traffic = new THREE.InstancedMesh(carGeo, carMat, 36);
  const traffic2 = new THREE.InstancedMesh(carGeo, carMat2, 36);
  traffic.frustumCulled = traffic2.frustumCulled = false;
  for (let i = 0; i < 36; i++) {
    lanes.push({
      r: 110 + (i % 6) * 22, y: 38 + (i % 5) * 9, a: TX.rand() * Math.PI * 2,
      speed: (0.04 + TX.rand() * 0.05) * (i % 2 ? 1 : -1),
    });
  }
  scene.add(traffic, traffic2);

  // A cargo drone slowly circling the town (also the UAV visual)
  const drone = new THREE.Group();
  const dBody = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.4, 1.0), new THREE.MeshStandardMaterial({ color: '#23262d', metalness: 0.8, roughness: 0.3 }));
  drone.add(dBody);
  const dLight = new THREE.Mesh(new THREE.SphereGeometry(0.18, 8, 6), beaconMat);
  dLight.position.y = -0.3;
  drone.add(dLight);
  for (const [x, z] of [[-1.3, 0.7], [1.3, 0.7], [-1.3, -0.7], [1.3, -0.7]]) {
    const rotor = new THREE.Mesh(new THREE.TorusGeometry(0.45, 0.05, 6, 16), crownMat2);
    rotor.rotation.x = Math.PI / 2;
    rotor.position.set(x, 0.1, z);
    drone.add(rotor);
  }
  drone.visible = false;
  scene.add(drone);

  const tmpM = new THREE.Matrix4();
  const tmpQ = new THREE.Quaternion();
  const tmpS = new THREE.Vector3(1, 1, 1);
  const tmpP = new THREE.Vector3();
  const up = new THREE.Vector3(0, 1, 0);

  function update(dt, t) {
    skyMat.uniforms.uTime.value = t;
    for (let i = 0; i < lanes.length; i++) {
      const L = lanes[i];
      L.a += L.speed * dt;
      tmpP.set(Math.cos(L.a) * L.r, L.y, Math.sin(L.a) * L.r);
      tmpQ.setFromAxisAngle(up, -L.a + (L.speed > 0 ? 0 : Math.PI));
      tmpM.compose(tmpP, tmpQ, tmpS);
      traffic.setMatrixAt(i, tmpM);
      tmpM.compose(tmpP.set(Math.cos(L.a - L.speed * 1.2) * L.r, L.y, Math.sin(L.a - L.speed * 1.2) * L.r), tmpQ, tmpS);
      traffic2.setMatrixAt(i, tmpM);
    }
    traffic.instanceMatrix.needsUpdate = true;
    traffic2.instanceMatrix.needsUpdate = true;
    const blink = Math.sin(t * 3) > 0.6;
    beaconMeshes.forEach((b) => { b.visible = blink; });
    if (drone.visible) {
      const a = t * 0.18;
      drone.position.set(Math.cos(a) * 26, 30, Math.sin(a) * 22);
      drone.rotation.y = -a;
      dLight.visible = Math.sin(t * 8) > 0;
    }
  }

  return { sky, skyMat, sun, hemi, update, drone, sunDir: SUN_DIR };
}
