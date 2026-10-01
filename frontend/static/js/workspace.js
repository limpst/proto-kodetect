/* 제품 워크플로우 화면 — 사진그룹(STEP 03) · 도면·위치(STEP 04) · 보고서(STEP 05)
   QuickGuide의 조작 흐름을 그대로 옮긴다. */

const WS = {
  inspectionId: null,
  groups: [],
  photos: [],
  selected: new Set(),
  activeGroup: undefined,   // undefined=미선택, null=미분류, number=그룹id
  drawings: [],
  activeDrawing: null,
  spots: [],
  armed: false,             // 위치 찍기 대기 상태
  zoom: 1,                  // 도면 확대 배율 — 실금 위치를 정확히 찍으려면 확대가 필요하다
  activeSpot: null,         // 선택한 핀 id
  dragging: null,           // 끌고 있는 핀 id
  grab: null,               // 끌기 시작 지점과 핀 중심의 간격
};

/* ─── 공통 ──────────────────────────────────────────────── */
function memberOptions(selected = "") {
  return App.members
    .map(
      (c) =>
        `<option value="${c.code}" ${c.code === selected ? "selected" : ""}>${esc(
          c.label
        )}</option>`
    )
    .join("");
}

function inspectionOptions() {
  return App.inspections
    .map(
      (i) =>
        `<option value="${i.id}">${fmtDate(i.inspected_at)} · ${esc(
          i.kind_label
        )}</option>`
    )
    .join("");
}

const STATE_BADGE = {
  analyzed: '<span class="badge ok">분석 완료</span>',
  pending: '<span class="badge mute">분석 전</span>',
  needs_scale: '<span class="badge warn">정보 부족</span>',
  failed: '<span class="badge crit">실패</span>',
};

/* ─── STEP 03 · 사진 그룹 ───────────────────────────────── */
async function loadGroups() {
  const sel = el("grInsp");
  if (!sel.options.length) {
    sel.innerHTML = inspectionOptions();
    el("grMember").innerHTML = memberOptions("column");
    sel.addEventListener("change", () => {
      WS.inspectionId = Number(sel.value);
      WS.activeGroup = undefined;
      refreshGroups().catch(console.error);
    });
  }
  WS.inspectionId = Number(sel.value);
  await refreshGroups();
}

async function refreshGroups() {
  WS.groups = await api(`/api/groups?inspection_id=${WS.inspectionId}`);
  renderGroupList();
  fillGroupSelects();
  if (WS.activeGroup !== undefined) await loadGroupPhotos(WS.activeGroup);
}

function renderGroupList() {
  const total = WS.groups.reduce((a, g) => a + g.photo_count, 0);
  el("grList").innerHTML =
    `<div class="note" style="margin-bottom:10px">사진 ${int(total)}장 · 그룹 ${int(
      WS.groups.length - 1
    )}개</div>` +
    WS.groups
      .map((g) => {
        const active = WS.activeGroup === g.id ? " active" : "";
        const un = g.id === null;
        return `<div class="grp-card${active}${un ? " un" : ""}" data-gid="${
          g.id ?? ""
        }">
        <div class="grp-top">
          <b>${esc(g.name)}</b>
          ${un ? "" : `<span class="badge mute">${esc(g.member_label)}</span>`}
          ${
            un
              ? ""
              : `<button class="grp-del" data-del="${g.id}" title="그룹 삭제">×</button>`
          }
        </div>
        <div class="grp-nums">
          <span>사진 <b class="num">${int(g.photo_count)}</b></span>
          <span>분석 <b class="num">${int(g.analyzed_count)}</b></span>
          <span>결함 <b class="num">${int(g.defect_count)}</b></span>
        </div>
      </div>`;
      })
      .join("");

  el("grList")
    .querySelectorAll(".grp-card")
    .forEach((n) =>
      n.addEventListener("click", (e) => {
        if (e.target.dataset.del) return;
        const raw = n.dataset.gid;
        loadGroupPhotos(raw === "" ? null : Number(raw)).catch(console.error);
      })
    );

  el("grList")
    .querySelectorAll("[data-del]")
    .forEach((n) =>
      n.addEventListener("click", async (e) => {
        e.stopPropagation();
        const g = WS.groups.find((x) => x.id === Number(n.dataset.del));
        // 사진은 미분류로 되돌아간다 — 사라지지 않는다는 점을 먼저 알린다
        if (!confirm(`'${g.name}' 그룹을 삭제합니다.\n사진 ${g.photo_count}장은 미분류로 이동합니다.`))
          return;
        await api(`/api/groups/${n.dataset.del}`, { method: "DELETE" });
        if (WS.activeGroup === Number(n.dataset.del)) WS.activeGroup = undefined;
        await refreshGroups();
      })
    );
}

function fillGroupSelects() {
  const named = WS.groups.filter((g) => g.id !== null);
  const opts = named.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
  const move = el("grMoveTo");
  if (move) move.innerHTML = `<option value="">미분류로</option>` + opts;
  const sp = el("spGroup");
  if (sp) sp.innerHTML = `<option value="">연결 안 함</option>` + opts;
}

async function loadGroupPhotos(gid) {
  WS.activeGroup = gid;
  WS.selected.clear();
  const q =
    gid === null
      ? `unassigned=true`
      : `group_id=${gid}`;
  WS.photos = await api(`/api/photos?inspection_id=${WS.inspectionId}&${q}`);
  renderGroupList();

  const g = WS.groups.find((x) => x.id === gid);
  el("grPhotoHint").textContent = `${g ? g.name : "—"} · ${WS.photos.length}장`;
  el("grMoveBar").style.display = WS.photos.length ? "flex" : "none";

  el("grPhotos").innerHTML = WS.photos.length
    ? WS.photos
        .map(
          (p) => `<div class="ph-card" data-pid="${p.id}">
        <img src="${p.overlay_url || p.url}" alt="${esc(p.filename)}" loading="lazy" />
        <div class="ph-meta">
          ${STATE_BADGE[p.analysis_state] || ""}
          <span class="num">결함 ${int(p.defect_count)}</span>
        </div>
        <div class="ph-name">${esc(p.filename.slice(0, 22))}</div>
        ${
          p.gsd_mm_per_px
            ? `<div class="ph-gsd num">${num(p.gsd_mm_per_px, 3)} mm/px</div>`
            : `<div class="ph-gsd warn-t">스케일 없음</div>`
        }
      </div>`
        )
        .join("")
    : '<div class="empty">사진이 없습니다</div>';

  el("grPhotos")
    .querySelectorAll(".ph-card")
    .forEach((n) =>
      n.addEventListener("click", () => {
        const id = Number(n.dataset.pid);
        if (WS.selected.has(id)) {
          WS.selected.delete(id);
          n.classList.remove("sel");
        } else {
          WS.selected.add(id);
          n.classList.add("sel");
        }
        el("grSelCount").textContent = `선택 ${WS.selected.size}장`;
      })
    );
  el("grSelCount").textContent = "선택 0장";
}

async function createGroup() {
  const name = el("grName").value.trim();
  if (!name) {
    el("grStatus").innerHTML = '<div class="alert">그룹 이름을 입력하십시오.</div>';
    return;
  }
  await api("/api/groups", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      inspection_id: WS.inspectionId,
      name,
      member_code: el("grMember").value,
    }),
  });
  el("grName").value = "";
  el("grStatus").innerHTML = `<div class="alert info">그룹 '${esc(name)}' 을 만들었습니다.</div>`;
  await refreshGroups();
}

async function moveSelected() {
  if (!WS.selected.size) return;
  const raw = el("grMoveTo").value;
  const r = await api("/api/groups/assign", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      photo_ids: [...WS.selected],
      group_id: raw === "" ? null : Number(raw),
    }),
  });
  el("grStatus").innerHTML = `<div class="alert info">사진 ${r.moved}장을 이동했습니다.</div>`;
  await refreshGroups();
  await loadGroupPhotos(WS.activeGroup);
}

async function runGroupDemo() {
  el("grStatus").innerHTML =
    '<div class="alert info"><span class="spin"></span> 합성 표본 3장 생성·분석 중…</div>';
  try {
    const ids = [];
    for (let i = 0; i < 3; i++) {
      const seed = Math.floor(Math.random() * 100000);
      const d = await api(
        `/api/detect/demo?inspection_id=${WS.inspectionId}&seed=${seed}` +
          `&member_code=${encodeURIComponent(el("grMember").value)}`,
        { method: "POST" }
      );
      ids.push(d.photo_id);
    }
    el("grStatus").innerHTML =
      `<div class="alert info">3장 분석 완료. 미분류에 담겼습니다 — 그룹으로 옮기십시오.</div>`;
    await refreshGroups();
    await loadGroupPhotos(null);
  } catch (e) {
    el("grStatus").innerHTML = `<div class="alert critical">실패: ${esc(e.message)}</div>`;
  }
}

/* ─── STEP 04 · 도면 · 위치 ─────────────────────────────── */

/* 도면 좌표계
   핀의 x·y 는 도면 원본 픽셀 기준이며 원점은 좌상단이다. 화면 표시는 확대·축소에
   따라 바뀌지만 저장값은 늘 원본 px 이라, 나중에 도면 파일을 교체해도 좌표가 흔들리지 않는다.
   축척(mm_per_px)이 있으면 같은 좌표를 실치수로도 보여 준다 — 물량 산출의 근거가 된다. */

/** 도면 px → 실치수 문자열. 축척이 없으면 null. */
function mmText(d, x, y) {
  if (!d || !d.mm_per_px) return null;
  const mx = x * d.mm_per_px;
  const my = y * d.mm_per_px;
  // 1 m 이상이면 m 로, 아니면 mm 로 — 현장에서 읽기 쉬운 단위를 고른다
  return mx >= 1000 || my >= 1000
    ? `${num(mx / 1000, 2)} m, ${num(my / 1000, 2)} m`
    : `${num(mx, 0)} mm, ${num(my, 0)} mm`;
}

/** 확대 배율 적용 후 라벨 갱신. */
function applyZoom(z) {
  const d = WS.activeDrawing;
  if (!d) return;
  WS.zoom = Math.min(8, Math.max(0.25, z));
  const wrap = el("dwCanvasWrap");
  const cv = el("dwCanvas");
  if (cv) {
    // 배율 1 = 카드 폭에 맞춤. 그 이상은 가로 스크롤로 훑는다.
    cv.style.width = `${WS.zoom * 100}%`;
    wrap.classList.toggle("zoomed", WS.zoom > 1);
  }
  el("dwZoomLabel").textContent = `${Math.round(WS.zoom * 100)}%`;
}

async function loadDrawings() {
  if (!el("spMember").options.length) {
    el("spMember").innerHTML = memberOptions("column");
  }
  // 사진 그룹 화면을 거치지 않고 바로 들어오면 WS.groups 가 비어 있어
  // 핀에 연결할 그룹을 고를 수 없다. 여기서 한 번 채운다.
  if (!WS.groups.length) {
    if (WS.inspectionId == null) {
      const first = App.inspections && App.inspections[0];
      if (first) WS.inspectionId = first.id;
    }
    if (WS.inspectionId != null) {
      try {
        WS.groups = await api(`/api/groups?inspection_id=${WS.inspectionId}`);
      } catch (e) {
        console.error("그룹 목록을 불러오지 못했습니다", e);
      }
    }
  }

  WS.drawings = await api(`/api/drawings?building_id=${App.buildingId}`);
  fillGroupSelects();

  renderTable(
    el("dwTable"),
    [
      { h: "도면", render: (d) => esc(d.name) },
      {
        h: "형식",
        render: (d) =>
          d.file_kind === "blank"
            ? '<span class="badge mute">빈 도면</span>'
            : `<span class="badge info">${esc(d.file_kind.toUpperCase())}</span>`,
      },
      { h: "위치", cls: "num", render: (d) => int(d.spot_count) },
      { h: "사진", cls: "num", render: (d) => int(d.photo_count) },
      { h: "손상", cls: "num", render: (d) => int(d.defect_count) },
      {
        h: "",
        render: (d) => `<button data-open="${d.id}">열기</button>`,
      },
    ],
    WS.drawings,
    "도면이 없습니다. 위에서 추가하십시오."
  );

  el("dwTable")
    .querySelectorAll("[data-open]")
    .forEach((n) =>
      n.addEventListener("click", () => openDrawing(Number(n.dataset.open)))
    );

  if (WS.drawings.length && !WS.activeDrawing) openDrawing(WS.drawings[0].id);
}

async function createDrawing() {
  const name = el("dwName").value.trim();
  if (!name) {
    el("dwStatus").innerHTML = '<div class="alert">도면 이름을 입력하십시오.</div>';
    return;
  }
  const fd = new FormData();
  fd.append("building_id", App.buildingId);
  fd.append("name", name);
  const f = el("dwFile").files[0];
  if (f) fd.append("file", f);
  const mm = parseFloat(el("dwMm").value);
  if (Number.isFinite(mm) && mm > 0) fd.append("mm_per_px", mm);

  try {
    const d = await api("/api/drawings", { method: "POST", body: fd });
    el("dwName").value = "";
    el("dwFile").value = "";
    el("dwStatus").innerHTML =
      `<div class="alert info">'${esc(d.name)}' 추가 (${
        d.file_kind === "blank" ? "빈 도면 — 위치를 먼저 찍고 나중에 파일로 교체할 수 있습니다" : d.file_kind.toUpperCase()
      })</div>`;
    WS.activeDrawing = null;
    await loadDrawings();
  } catch (e) {
    el("dwStatus").innerHTML = `<div class="alert critical">${esc(e.message)}</div>`;
  }
}

async function openDrawing(id) {
  WS.activeDrawing = WS.drawings.find((d) => d.id === id);
  WS.spots = await api(`/api/drawings/${id}/spots`);
  if (!WS.spots.some((s) => s.id === WS.activeSpot)) WS.activeSpot = null;
  renderCanvas();
  renderSpotTable();
  renderSpotDetail();
}

/** 선택한 핀의 정밀 조정 패널. 클릭으로는 못 맞추는 좌표를 수치로 보정한다. */
function renderSpotDetail() {
  const box = el("spDetail");
  const s = WS.spots.find((x) => x.id === WS.activeSpot);
  if (!s) {
    box.hidden = true;
    return;
  }
  const d = WS.activeDrawing;
  box.hidden = false;
  el("spdNo").textContent = `#${s.number}`;
  el("spdName").textContent = s.group_name || "연결 안 함";
  el("spdX").value = Math.round(s.x);
  el("spdY").value = Math.round(s.y);
  const mm = mmText(d, s.x, s.y);
  el("spdMm").textContent = mm ? `실치수 ${mm}` : "축척이 설정되지 않아 실치수를 낼 수 없습니다";

  // 그룹 선택지는 상단 '연결할 사진 그룹' 과 같은 목록을 쓴다
  const src = el("spGroup");
  const sel = el("spdGroup");
  if (src && sel) {
    sel.innerHTML = src.innerHTML;
    sel.value = s.group_id == null ? "" : String(s.group_id);
  }
}

/** 좌표를 서버에 반영. 드래그와 수치 입력이 같은 경로를 쓴다. */
async function moveSpot(id, x, y) {
  const d = WS.activeDrawing;
  const [w, h] = d.size;
  const nx = Math.round(Math.min(w, Math.max(0, x)));
  const ny = Math.round(Math.min(h, Math.max(0, y)));
  await api(`/api/spots/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ x: nx, y: ny }),
  });
  const s = WS.spots.find((v) => v.id === id);
  if (s) { s.x = nx; s.y = ny; }
}

function renderCanvas() {
  const d = WS.activeDrawing;
  if (!d) return;
  const [w, h] = d.size;
  const bg =
    d.url && ["jpg", "png"].includes(d.file_kind)
      ? `background-image:url('${d.url}');background-size:100% 100%;`
      : "";

  el("dwCanvasHint").textContent =
    `${d.name} · ${w}×${h}px` +
    (d.mm_per_px ? ` · ${num(d.mm_per_px, 2)} mm/px` : " · 축척 미설정") +
    (WS.armed ? " · 캔버스를 클릭해 위치를 찍으십시오" : "");

  // 축척 입력란에 현재값을 채워 둔다 — 비어 있으면 '설정된 적 없음'과 구분이 안 된다
  const se = el("dwScaleEdit");
  if (se) se.value = d.mm_per_px ? d.mm_per_px : "";

  el("dwCanvasWrap").innerHTML = `
    <div class="dwg-canvas${WS.armed ? " armed" : ""}" id="dwCanvas"
         style="aspect-ratio:${w}/${h};${bg}">
      ${
        d.file_kind === "blank"
          ? '<div class="dwg-blank">빈 도면 — 위치를 찍고 나중에 파일로 교체할 수 있습니다</div>'
          : ""
      }
      ${WS.spots
        .map(
          (s) => `<div class="pin${s.id === WS.activeSpot ? " active" : ""}" data-sid="${s.id}"
            style="left:${(s.x / w) * 100}%;top:${(s.y / h) * 100}%"
            title="#${s.number} · ${esc(s.group_name || "연결 안 함")} · ${Math.round(s.x)}, ${Math.round(s.y)} px">
            <i>${s.number}</i>
            <span>${esc(s.group_name || "—")} · ${int(s.photo_count)}장</span>
          </div>`
        )
        .join("")}
    </div>`;

  const cv = el("dwCanvas");

  /** 화면 좌표 → 도면 원본 px. 확대 배율과 무관하게 같은 값이 나온다. */
  const toDrawing = (e) => {
    const r = cv.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * w, ((e.clientY - r.top) / r.height) * h];
  };

  // 커서 위치를 실시간으로 읽어 준다 — 핀을 찍기 전에 좌표를 확인할 수 있다
  cv.addEventListener("mousemove", (e) => {
    const [x, y] = toDrawing(e);
    if (x < 0 || y < 0 || x > w || y > h) return;
    el("roPx").textContent = `${Math.round(x)}, ${Math.round(y)} px`;
    const mm = mmText(d, x, y);
    el("roMm").textContent = mm || "축척 미설정";
    if (WS.dragging !== null && WS.grab) {
      // 잡은 지점과 핀 중심의 간격을 유지한다. 안 그러면 원 가장자리를 잡았을 때
      // 핀이 커서로 튀어 '선택만 하려던 클릭'이 이동이 되어 버린다.
      const pin = cv.querySelector(`.pin[data-sid="${WS.dragging}"]`);
      if (pin) {
        pin.style.left = `${((WS.grab.px + x - WS.grab.sx) / w) * 100}%`;
        pin.style.top = `${((WS.grab.py + y - WS.grab.sy) / h) * 100}%`;
      }
    }
  });
  cv.addEventListener("mouseleave", () => {
    el("roPx").textContent = "—";
    el("roMm").textContent = d.mm_per_px ? "—" : "축척 미설정";
  });

  // 핀 선택 · 끌어 옮기기
  cv.querySelectorAll(".pin").forEach((pin) => {
    const sid = Number(pin.dataset.sid);
    pin.addEventListener("mousedown", (e) => {
      e.stopPropagation();           // 캔버스 클릭(새 핀 생성)과 섞이지 않게
      const s = WS.spots.find((v) => v.id === sid);
      if (!s) return;
      const [sx, sy] = toDrawing(e);
      WS.activeSpot = sid;
      WS.dragging = sid;
      WS.grab = { sx, sy, px: s.x, py: s.y };   // 잡은 지점과 핀 중심의 간격
      pin.classList.add("dragging");
      renderSpotDetail();
      cv.querySelectorAll(".pin").forEach((p) => p.classList.toggle("active", p === pin));
    });
  });

  window.addEventListener("mouseup", async function onUp(e) {
    if (WS.dragging === null) return;
    const id = WS.dragging;
    const grab = WS.grab;
    WS.dragging = null;
    WS.grab = null;
    cv.querySelectorAll(".pin").forEach((p) => p.classList.remove("dragging"));
    const [x, y] = toDrawing(e);
    const s = WS.spots.find((v) => v.id === id);
    if (!s || !grab) { renderSpotDetail(); return; }
    // 커서가 2px 미만으로 움직였으면 '선택만 한 클릭'이다 — 좌표를 건드리지 않는다
    const dx = x - grab.sx;
    const dy = y - grab.sy;
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) { renderSpotDetail(); return; }
    try {
      await moveSpot(id, grab.px + dx, grab.py + dy);
      await openDrawing(d.id);
    } catch (err) {
      el("dwStatus").innerHTML = `<div class="alert critical">위치 이동 실패: ${esc(err.message)}</div>`;
      await openDrawing(d.id);
    }
  });

  // 빈 곳 클릭 — 위치 찍기 대기 상태일 때만 새 핀을 만든다
  cv.addEventListener("click", async (e) => {
    if (!WS.armed) return;
    const [x, y] = toDrawing(e);
    const gid = el("spGroup").value;
    await api("/api/spots", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        drawing_id: d.id,
        x: Math.round(x),
        y: Math.round(y),
        group_id: gid === "" ? null : Number(gid),
        member_code: el("spMember").value,
        direction: el("spDir").value,
      }),
    });
    WS.armed = false;
    el("spArm").classList.remove("primary");
    await openDrawing(d.id);
    await loadDrawings();
  });

  applyZoom(WS.zoom);
}

function renderSpotTable() {
  renderTable(
    el("spTable"),
    [
      { h: "#", cls: "num", render: (s) => int(s.number) },
      { h: "그룹", render: (s) => esc(s.group_name || "—") },
      { h: "부재", render: (s) => esc(s.member_label) },
      { h: "방향", render: (s) => esc(s.direction || "—") },
      // 도면 위치정보 — 저장값은 늘 원본 px 이고, 축척이 있으면 실치수를 함께 보여 준다
      {
        h: "좌표 (px)",
        cls: "num",
        render: (s) => `${Math.round(s.x)}, ${Math.round(s.y)}`,
      },
      {
        h: "실치수",
        cls: "num",
        render: (s) => esc(mmText(WS.activeDrawing, s.x, s.y) || "—"),
      },
      { h: "사진", cls: "num", render: (s) => int(s.photo_count) },
      { h: "손상", cls: "num", render: (s) => int(s.defect_count) },
      {
        h: "",
        render: (s) =>
          `<button data-pick="${s.id}">선택</button> <button data-rm="${s.id}">삭제</button>`,
      },
    ],
    WS.spots,
    "위치가 없습니다"
  );

  el("spTable")
    .querySelectorAll("[data-pick]")
    .forEach((n) =>
      n.addEventListener("click", () => {
        WS.activeSpot = Number(n.dataset.pick);
        renderCanvas();
        renderSpotDetail();
      })
    );

  el("spTable")
    .querySelectorAll("[data-rm]")
    .forEach((n) =>
      n.addEventListener("click", async () => {
        await api(`/api/spots/${n.dataset.rm}`, { method: "DELETE" });
        if (WS.activeSpot === Number(n.dataset.rm)) WS.activeSpot = null;
        await openDrawing(WS.activeDrawing.id);
        await loadDrawings();
      })
    );
}

/* ─── STEP 05 · 보고서 출력 ─────────────────────────────── */
async function loadDeliver() {
  const sel = el("dlInsp");
  if (!sel.options.length) {
    sel.innerHTML = inspectionOptions();
    sel.addEventListener("change", () => refreshPreview().catch(console.error));
    el("dlScope").addEventListener("change", () => refreshPreview().catch(console.error));
  }
  await refreshPreview();
}

async function refreshPreview() {
  const iid = Number(el("dlInsp").value);
  const scope = el("dlScope").value;
  const pv = await api(`/api/reports/preview?inspection_id=${iid}&scope=${scope}`);

  // 범위 선택 목록 — 도면 기준이면 도면, 그룹 기준이면 그룹
  const items =
    scope === "drawing"
      ? pv.drawings.map(
          (d) =>
            `<label class="pick-row"><input type="checkbox" class="scope-pick" value="${d.id}" checked />
             <span>${esc(d.name)}</span><span class="num">위치 ${int(d.spot_count)}</span></label>`
        )
      : pv.groups
          .filter((g) => g.id !== null)
          .map(
            (g) =>
              `<label class="pick-row"><input type="checkbox" class="scope-pick" value="${g.id}" checked />
               <span>${esc(g.name)}</span><span class="num">사진 ${int(
                g.photo_count
              )} · 결함 ${int(g.defect_count)}</span></label>`
          );

  el("dlScopeList").innerHTML = items.length
    ? items.join("")
    : `<div class="empty">${
        scope === "drawing" ? "도면이 없습니다" : "사진 그룹이 없습니다"
      }</div>`;

  const byType = Object.entries(pv.defect_by_type)
    .map(([k, v]) => `${esc(k)} <b class="num">${int(v)}</b>`)
    .join(" · ");

  el("dlPreview").innerHTML = `
    <div class="kpi-grid">
      ${kpi("검출 결함", `<span class="num">${int(pv.defect_total)}</span>`, "건")}
      ${kpi("직접 입력", `<span class="num">${int(pv.manual_count)}</span>`, "건",
            "사람이 그린 손상")}
      ${kpi("물량 산출 불가", `<span class="num">${int(pv.quantity_unavailable)}</span>`,
            "건", "스케일 없음", pv.quantity_unavailable ? "warn" : "")}
    </div>
    <div class="note" style="margin-top:12px">${byType || "결함이 없습니다"}</div>
    ${pv.warnings.map((w) => `<div class="alert">${esc(w)}</div>`).join("")}
    <div class="alert info" style="margin-top:10px">
      AI 분석 결과는 참고용입니다. 최종 보고서의 손상 수치와 내용은
      출력 전 반드시 검토하십시오.
    </div>`;
}

async function buildReport() {
  const kinds = [...document.querySelectorAll("#dlKinds input:checked")].map(
    (n) => n.value
  );
  if (!kinds.length) {
    el("dlStatus").innerHTML =
      '<div class="alert">결과 파일을 최소 1개 선택하십시오.</div>';
    return;
  }
  const iid = Number(el("dlInsp").value);
  const scope = el("dlScope").value;
  const picked = [...document.querySelectorAll(".scope-pick:checked")].map((n) =>
    Number(n.value)
  );

  el("dlStatus").innerHTML =
    '<div class="alert info"><span class="spin"></span> 보고서 생성 중…</div>';
  try {
    const r = await fetch("/api/reports/build", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        inspection_id: iid,
        scope,
        drawing_ids: scope === "drawing" ? picked : [],
        group_ids: scope === "group" ? picked : [],
        kinds,
      }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${r.status}`);
    }
    const n = r.headers.get("X-Report-Defects");
    const blob = await r.blob();

    // 파일명은 Content-Disposition 의 RFC 5987 filename* 에서 꺼낸다
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/);
    const filename = m ? decodeURIComponent(m[1]) : "KO-Detect_report.zip";

    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);

    el("dlStatus").innerHTML =
      `<div class="alert info">다운로드 완료 — ${esc(filename)}
       (${Math.round(blob.size / 1024)}KB · 결함 ${esc(n || "?")}건)</div>`;
  } catch (e) {
    el("dlStatus").innerHTML = `<div class="alert critical">${esc(e.message)}</div>`;
  }
}

/* ─── 등록 ──────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  el("grCreate")?.addEventListener("click", () => createGroup().catch(console.error));
  el("grDemo")?.addEventListener("click", () => runGroupDemo().catch(console.error));
  el("grMove")?.addEventListener("click", () => moveSelected().catch(console.error));
  el("dwCreate")?.addEventListener("click", () => createDrawing().catch(console.error));
  el("spArm")?.addEventListener("click", (e) => {
    WS.armed = !WS.armed;
    e.currentTarget.classList.toggle("primary", WS.armed);
    renderCanvas();
  });

  // 확대·축소 — 실금 위치를 px 단위로 맞추려면 확대가 필요하다
  el("dwZoomIn")?.addEventListener("click", () => applyZoom(WS.zoom * 1.5));
  el("dwZoomOut")?.addEventListener("click", () => applyZoom(WS.zoom / 1.5));
  el("dwZoomFit")?.addEventListener("click", () => applyZoom(1));

  // 축척 사후 수정 — 핀 좌표는 원본 px 라 축척을 바꿔도 위치는 그대로다
  el("dwScaleSave")?.addEventListener("click", async () => {
    const d = WS.activeDrawing;
    if (!d) return;
    const mm = parseFloat(el("dwScaleEdit").value);
    if (!Number.isFinite(mm) || mm <= 0) {
      el("dwStatus").innerHTML = '<div class="alert">축척은 0보다 큰 값이어야 합니다.</div>';
      return;
    }
    try {
      await api(`/api/drawings/${d.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ mm_per_px: mm }),
      });
      el("dwStatus").innerHTML =
        `<div class="alert info">축척을 ${num(mm, 2)} mm/px 로 저장했습니다. 실치수가 갱신됩니다.</div>`;
      await loadDrawings();
      await openDrawing(d.id);
    } catch (e) {
      el("dwStatus").innerHTML = `<div class="alert critical">축척 저장 실패: ${esc(e.message)}</div>`;
    }
  });

  // 핀의 사진 그룹 재지정
  el("spdGroupApply")?.addEventListener("click", async () => {
    if (WS.activeSpot === null) return;
    const v = el("spdGroup").value;
    try {
      await api(`/api/spots/${WS.activeSpot}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ group_id: v === "" ? null : Number(v) }),
      });
      await openDrawing(WS.activeDrawing.id);
      await loadDrawings();
    } catch (e) {
      el("dwStatus").innerHTML = `<div class="alert critical">그룹 적용 실패: ${esc(e.message)}</div>`;
    }
  });

  // 선택한 핀의 좌표를 수치로 보정
  el("spdApply")?.addEventListener("click", async () => {
    if (WS.activeSpot === null) return;
    const x = parseFloat(el("spdX").value);
    const y = parseFloat(el("spdY").value);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    try {
      await moveSpot(WS.activeSpot, x, y);
      await openDrawing(WS.activeDrawing.id);
    } catch (err) {
      el("dwStatus").innerHTML = `<div class="alert critical">좌표 적용 실패: ${esc(err.message)}</div>`;
    }
  });
  el("spdClose")?.addEventListener("click", () => {
    WS.activeSpot = null;
    renderCanvas();
    renderSpotDetail();
  });
  el("dlBuild")?.addEventListener("click", () => buildReport().catch(console.error));
});
