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
async function loadDrawings() {
  if (!el("spMember").options.length) {
    el("spMember").innerHTML = memberOptions("column");
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
  renderCanvas();
  renderSpotTable();
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
    (WS.armed ? " · 캔버스를 클릭해 위치를 찍으십시오" : "");

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
          (s) => `<div class="pin" data-sid="${s.id}"
            style="left:${(s.x / w) * 100}%;top:${(s.y / h) * 100}%"
            title="${esc(s.group_name || "연결 안 함")}">
            <i>${s.number}</i>
            <span>${esc(s.group_name || "—")} · ${int(s.photo_count)}장</span>
          </div>`
        )
        .join("")}
    </div>`;

  el("dwCanvas").addEventListener("click", async (e) => {
    if (!WS.armed) return;
    const r = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * w;
    const y = ((e.clientY - r.top) / r.height) * h;
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
}

function renderSpotTable() {
  renderTable(
    el("spTable"),
    [
      { h: "#", cls: "num", render: (s) => int(s.number) },
      { h: "그룹", render: (s) => esc(s.group_name || "—") },
      { h: "부재", render: (s) => esc(s.member_label) },
      { h: "방향", render: (s) => esc(s.direction || "—") },
      { h: "사진", cls: "num", render: (s) => int(s.photo_count) },
      { h: "손상", cls: "num", render: (s) => int(s.defect_count) },
      { h: "", render: (s) => `<button data-rm="${s.id}">삭제</button>` },
    ],
    WS.spots,
    "위치가 없습니다"
  );
  el("spTable")
    .querySelectorAll("[data-rm]")
    .forEach((n) =>
      n.addEventListener("click", async () => {
        await api(`/api/spots/${n.dataset.rm}`, { method: "DELETE" });
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
  el("dlBuild")?.addEventListener("click", () => buildReport().catch(console.error));
});
