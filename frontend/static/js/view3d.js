/* 구조물 3D 뷰 — three.js.
   층별 상태등급을 색으로, 계측 채널을 구체로 표시하고 실시간 값에 반응시킨다.
   OrbitControls 의존을 피하려고 최소 궤도 조작을 직접 구현했다. */

const V3D = {
  ready: false,
  scene: null,
  camera: null,
  renderer: null,
  markers: new Map(),
  root: null,
  target: null,
  angle: { theta: 0.85, phi: 1.05, radius: 5.6 },
};

const V3D_STATUS_COLOR = { normal: 0x22c55e, warn: 0xeab308, critical: 0xef4444 };
const V3D_GRADE_COLOR = {
  A: 0x1f6f3f, B: 0x4d7c1a, C: 0x8a6d10, D: 0x9a4a12, E: 0x8f2222,
};

function v3dInit(container) {
  if (V3D.ready) return;
  const w = container.clientWidth || 800;
  const h = container.clientHeight || 460;

  V3D.scene = new THREE.Scene();
  V3D.scene.background = new THREE.Color(0x080b11);
  V3D.scene.fog = new THREE.Fog(0x080b11, 8, 20);

  V3D.camera = new THREE.PerspectiveCamera(42, w / h, 0.1, 100);
  V3D.renderer = new THREE.WebGLRenderer({ antialias: true });
  V3D.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  V3D.renderer.setSize(w, h);
  container.innerHTML = "";
  container.appendChild(V3D.renderer.domElement);

  V3D.scene.add(new THREE.AmbientLight(0x9fb4d0, 0.75));
  const key = new THREE.DirectionalLight(0xffffff, 0.9);
  key.position.set(4, 8, 5);
  V3D.scene.add(key);
  const rim = new THREE.DirectionalLight(0x38bdf8, 0.4);
  rim.position.set(-5, 2, -4);
  V3D.scene.add(rim);

  // 지반 격자
  const grid = new THREE.GridHelper(12, 24, 0x2a3348, 0x1a2130);
  grid.position.y = -0.02;
  V3D.scene.add(grid);

  V3D.root = new THREE.Group();
  V3D.scene.add(V3D.root);
  V3D.target = new THREE.Vector3(0, 1.1, 0);

  _v3dBindOrbit(container);
  _v3dLoop();

  new ResizeObserver(() => {
    const cw = container.clientWidth, ch = container.clientHeight;
    if (!cw || !ch) return;
    V3D.camera.aspect = cw / ch;
    V3D.camera.updateProjectionMatrix();
    V3D.renderer.setSize(cw, ch);
  }).observe(container);

  V3D.ready = true;
}

function _v3dBindOrbit(container) {
  let dragging = false, lastX = 0, lastY = 0;
  container.addEventListener("pointerdown", (e) => {
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    container.setPointerCapture(e.pointerId);
  });
  container.addEventListener("pointerup", (e) => {
    dragging = false;
    try { container.releasePointerCapture(e.pointerId); } catch (_) {}
  });
  container.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    V3D.angle.theta -= (e.clientX - lastX) * 0.006;
    V3D.angle.phi = Math.max(
      0.18, Math.min(1.5, V3D.angle.phi - (e.clientY - lastY) * 0.005)
    );
    lastX = e.clientX; lastY = e.clientY;
  });
  container.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      V3D.angle.radius = Math.max(2.6, Math.min(14, V3D.angle.radius + e.deltaY * 0.003));
    },
    { passive: false }
  );
}

function _v3dLoop() {
  requestAnimationFrame(_v3dLoop);
  if (!V3D.renderer) return;
  const { theta, phi, radius } = V3D.angle;
  V3D.camera.position.set(
    V3D.target.x + radius * Math.cos(phi) * Math.sin(theta),
    V3D.target.y + radius * Math.sin(phi),
    V3D.target.z + radius * Math.cos(phi) * Math.cos(theta)
  );
  V3D.camera.lookAt(V3D.target);
  V3D.renderer.render(V3D.scene, V3D.camera);
}

/** 건축물 형상을 다시 만든다 (층수·등급 반영). */
function v3dBuild(building, memberGrades, channels) {
  const container = document.getElementById("v3dCanvas");
  v3dInit(container);
  while (V3D.root.children.length) V3D.root.remove(V3D.root.children[0]);
  V3D.markers.clear();

  const floors = Math.max(2, Math.min(building.floors_above || 5, 16));
  const floorH = 0.24;
  const wide = 1.9, deep = 1.5;
  const total = floors * floorH;
  V3D.target.set(0, total * 0.5, 0);
  V3D.angle.radius = Math.max(3.6, total * 2.1);

  // 최악 등급을 아래층부터 배치해 열화가 하부에 집중된 모습을 만든다
  const grades = (memberGrades || []).map((m) => (m.grade || "a").toUpperCase());

  for (let i = 0; i < floors; i++) {
    const g = grades.length ? grades[Math.min(i, grades.length - 1)] : "A";
    const mat = new THREE.MeshStandardMaterial({
      color: V3D_GRADE_COLOR[g] ?? 0x334155,
      roughness: 0.78,
      metalness: 0.05,
      transparent: true,
      opacity: 0.92,
    });
    const slab = new THREE.Mesh(
      new THREE.BoxGeometry(wide, floorH * 0.86, deep),
      mat
    );
    slab.position.y = i * floorH + floorH / 2;
    V3D.root.add(slab);

    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(slab.geometry),
      new THREE.LineBasicMaterial({ color: 0x0b0e14, transparent: true, opacity: 0.6 })
    );
    edges.position.copy(slab.position);
    V3D.root.add(edges);
  }

  // 기초
  const base = new THREE.Mesh(
    new THREE.BoxGeometry(wide * 1.18, 0.1, deep * 1.18),
    new THREE.MeshStandardMaterial({ color: 0x1e2637, roughness: 0.95 })
  );
  base.position.y = -0.05;
  V3D.root.add(base);

  // 계측 채널 마커
  (channels || []).forEach((c) => {
    const [x, y, z] = c.position || [0, 0.5, 0];
    const mesh = new THREE.Mesh(
      new THREE.SphereGeometry(0.055, 20, 16),
      new THREE.MeshStandardMaterial({
        color: V3D_STATUS_COLOR[c.status] || 0x22c55e,
        emissive: V3D_STATUS_COLOR[c.status] || 0x22c55e,
        emissiveIntensity: 0.55,
        roughness: 0.35,
      })
    );
    mesh.position.set(x * wide * 0.62, Math.max(0.08, y) * total, z * deep * 0.62);
    mesh.userData.code = c.code;
    V3D.root.add(mesh);
    V3D.markers.set(c.code, mesh);
  });
}

/** 실시간 tick으로 마커 색·크기를 갱신한다. */
function v3dUpdate(statuses, stresses) {
  V3D.markers.forEach((mesh, code) => {
    const st = statuses?.[code] || "normal";
    const color = V3D_STATUS_COLOR[st] || 0x22c55e;
    mesh.material.color.setHex(color);
    mesh.material.emissive.setHex(color);
    const s = 1 + 1.6 * Math.min(1, Math.max(0, stresses?.[`ch:${code}`] ?? 0) * 2.2);
    mesh.scale.setScalar(s);
  });
}
