/* 사진 상세 — 치수 측정 · 원근 보정 · 손상 직접 그리기
 *
 * 테스트 실행계획서 18항 "분석 결과 검토·보정" 을 화면으로 만든 것.
 *
 * 왜 모달인가
 * -----------
 * 이 작업은 영상 분석 결과에서도, 검출 결과 브라우저에서도, 도면 위치 상세에서도
 * 들어온다. 화면을 따로 두면 돌아갈 곳을 잃는다. 모달이면 하던 자리를 유지한 채
 * 고치고 닫으면 그만이다.
 *
 * 좌표계
 * ------
 * 클릭 좌표는 화면 픽셀이지만 서버는 원본 픽셀을 기대한다. 이미지가 화면에
 * 축소되어 표시되므로 naturalWidth 기준으로 되돌려 보낸다. 이 환산을 빠뜨리면
 * 스케일이 배율만큼 틀어지고, 그 오차가 그대로 균열폭 오차가 된다.
 */

const PH = {
  data: null,
  tool: "view",        // view | scale | rectify | draw
  pts: [],             // 현재 도구가 찍은 점 (원본 좌표)
  busy: false,
};

const PH_TOOLS = {
  view: { label: "보기", need: 0, hint: "결함을 클릭하면 상세가 표시됩니다." },
  scale: {
    label: "치수 측정", need: 2,
    hint: "길이를 아는 기준물(크랙스케일·줄자)의 양 끝 두 점을 찍으십시오.",
  },
  rectify: {
    label: "원근 보정", need: 4,
    hint: "직사각형인 것을 아는 영역(창틀·타일·패널)의 네 모서리를 찍으십시오.",
  },
  draw: {
    label: "손상 그리기", need: -1,
    hint: "균열을 따라 점을 이어 찍고 [저장]을 누르십시오. 두 점 이상 필요합니다.",
  },
};

/* ─── 열기/닫기 ─────────────────────────────────────────── */
async function phOpen(photoId) {
  PH.tool = "view";
  PH.pts = [];
  try {
    PH.data = await api(`/api/photos/${photoId}`);
  } catch (e) {
    alert("사진을 불러오지 못했습니다: " + e.message);
    return;
  }
  phEnsureModal();
  el("phModal").classList.add("open");
  phRender();
}

function phClose() {
  el("phModal")?.classList.remove("open");
  PH.data = null;
  PH.pts = [];
}

function phEnsureModal() {
  if (el("phModal")) return;
  const wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.id = "phModal";
  wrap.innerHTML = `
    <div class="modal-box">
      <div class="modal-head">
        <h2 id="phTitle">사진 상세</h2>
        <div class="modal-acts" id="phActs"></div>
        <button class="ghost" id="phClose">닫기</button>
      </div>
      <div class="modal-body">
        <div class="ph-left">
          <div class="ph-tools" id="phTools"></div>
          <div class="note" id="phHint" style="margin:8px 0 10px"></div>
          <div class="ph-canvas" id="phCanvas"></div>
          <div id="phToolPanel" style="margin-top:12px"></div>
        </div>
        <div class="ph-right">
          <div id="phMeta"></div>
          <h2 style="margin-top:16px">결함 목록 <span class="hint" id="phDefCount"></span></h2>
          <div class="table-wrap table-scroll"><table id="phDefects"></table></div>
        </div>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  el("phClose").addEventListener("click", phClose);
  wrap.addEventListener("click", (e) => {
    if (e.target === wrap) phClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && wrap.classList.contains("open")) phClose();
  });
}

/* ─── 렌더 ──────────────────────────────────────────────── */
function phRender() {
  const d = PH.data;
  if (!d) return;

  el("phTitle").textContent = `사진 #${d.id} · ${d.member_code}`;
  el("phActs").innerHTML = `
    <button class="ghost" id="phReanalyze">다시 분석</button>
    ${d.rectified ? '<button class="ghost" id="phUndoRect">보정 취소</button>' : ""}`;
  el("phReanalyze").addEventListener("click", phReanalyze);
  el("phUndoRect")?.addEventListener("click", phUndoRectify);

  el("phTools").innerHTML = Object.entries(PH_TOOLS)
    .map(
      ([k, t]) =>
        `<button class="${k === PH.tool ? "primary" : "ghost"}" data-tool="${k}">${t.label}</button>`
    )
    .join("");
  el("phTools")
    .querySelectorAll("[data-tool]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        PH.tool = b.dataset.tool;
        PH.pts = [];
        phRender();
      })
    );
  el("phHint").textContent = PH_TOOLS[PH.tool].hint;

  phRenderCanvas();
  phRenderToolPanel();
  phRenderMeta();
  phRenderDefects();
}

function phRenderCanvas() {
  const d = PH.data;
  const [w, h] = d.size;
  const marks = d.defects
    .map((x) => {
      const color = { a: "#22c55e", b: "#84cc16", c: "#eab308", d: "#f97316", e: "#ef4444" }[x.grade] || "#38bdf8";
      const pts = (x.polyline || []).map((p) => p.join(",")).join(" ");
      const dash = x.source === "manual" ? ' stroke-dasharray="7 4"' : "";
      return pts
        ? `<polyline class="cr" points="${pts}" stroke="${color}"${dash} />`
        : "";
    })
    .join("");

  const picks = PH.pts
    .map(
      (p, i) =>
        `<circle cx="${p[0]}" cy="${p[1]}" r="${Math.max(4, w / 220)}" class="pk" />
         <text x="${p[0] + w / 90}" y="${p[1] - w / 140}" class="pkt"
               font-size="${w / 55}">${i + 1}</text>`
    )
    .join("");
  const guide =
    PH.tool === "scale" && PH.pts.length === 2
      ? `<line x1="${PH.pts[0][0]}" y1="${PH.pts[0][1]}" x2="${PH.pts[1][0]}"
               y2="${PH.pts[1][1]}" class="pkline" />`
      : PH.tool === "rectify" && PH.pts.length >= 2
      ? `<polygon points="${PH.pts.map((p) => p.join(",")).join(" ")}" class="pkpoly" />`
      : PH.tool === "draw" && PH.pts.length >= 2
      ? `<polyline points="${PH.pts.map((p) => p.join(",")).join(" ")}" class="pkline" />`
      : "";

  el("phCanvas").innerHTML = `
    <div class="shot" id="phShot" style="cursor:${PH.tool === "view" ? "default" : "crosshair"}">
      <img id="phImg" src="${d.image_url}?t=${Date.now()}" alt="" />
      <svg class="marks" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        ${marks}${guide}${picks}
      </svg>
    </div>`;

  if (PH.tool === "view") return;
  el("phShot").addEventListener("click", (e) => {
    const img = el("phImg");
    const r = img.getBoundingClientRect();
    // 화면 좌표 → 원본 픽셀. 이 환산을 빠뜨리면 스케일이 배율만큼 틀어진다.
    const x = ((e.clientX - r.left) / r.width) * d.size[0];
    const y = ((e.clientY - r.top) / r.height) * d.size[1];
    const need = PH_TOOLS[PH.tool].need;
    if (need > 0 && PH.pts.length >= need) PH.pts = [];
    PH.pts.push([Math.round(x), Math.round(y)]);
    phRenderCanvas();
    phRenderToolPanel();
  });
}

function phRenderToolPanel() {
  const node = el("phToolPanel");
  const need = PH_TOOLS[PH.tool].need;
  const n = PH.pts.length;

  if (PH.tool === "view") {
    node.innerHTML = "";
    return;
  }
  const counter = `<span class="note">점 ${n}${need > 0 ? ` / ${need}` : ""}개</span>`;

  if (PH.tool === "scale") {
    node.innerHTML = `
      <div class="field-row">
        <div class="field">
          <label for="phRealMm">기준물 실제 길이 (mm)</label>
          <input id="phRealMm" type="number" step="0.1" placeholder="예: 150" />
        </div>
        <button class="primary" id="phApplyScale" ${n === 2 ? "" : "disabled"}>스케일 확정</button>
        <button class="ghost" id="phClearPts">점 지우기</button>
        ${counter}
      </div>
      <div class="note">기준물이 균열과 <b>같은 평면</b>에 있어야 정확합니다. 카메라 쪽으로
        당겨 든 자는 실제보다 크게 찍혀 균열폭이 과소평가됩니다.</div>`;
    el("phApplyScale").addEventListener("click", phApplyScale);
  } else if (PH.tool === "rectify") {
    node.innerHTML = `
      <div class="field-row">
        <div class="field">
          <label for="phRw">실제 가로 (mm)</label>
          <input id="phRw" type="number" step="1" placeholder="선택" />
        </div>
        <div class="field">
          <label for="phRh">실제 세로 (mm)</label>
          <input id="phRh" type="number" step="1" placeholder="선택" />
        </div>
        <button class="primary" id="phApplyRect" ${n === 4 ? "" : "disabled"}>보정 실행</button>
        <button class="ghost" id="phClearPts">점 지우기</button>
        ${counter}
      </div>
      <div class="note">실제 치수를 함께 넣으면 <b>스케일도 같이 확정</b>됩니다.
        평면 가정이 전제이므로 굴곡면에서는 국부 오차가 남습니다.</div>`;
    el("phApplyRect").addEventListener("click", phApplyRectify);
  } else {
    node.innerHTML = `
      <div class="field-row">
        <div class="field">
          <label for="phDType">유형</label>
          <select id="phDType">
            <option value="crack">균열</option>
            <option value="spalling">박리·박락</option>
            <option value="rebar_exposure">철근노출</option>
            <option value="leakage">누수</option>
            <option value="efflorescence">백태</option>
            <option value="segregation">재료분리</option>
            <option value="damage">손상</option>
          </select>
        </div>
        <div class="field">
          <label for="phDWidth">폭 (mm)</label>
          <input id="phDWidth" type="number" step="0.01" placeholder="균열만" />
        </div>
        <div class="field">
          <label for="phDNote">메모</label>
          <input id="phDNote" type="text" placeholder="예: AI 누락분" />
        </div>
        <button class="primary" id="phSaveDefect" ${n >= 2 ? "" : "disabled"}>저장</button>
        <button class="ghost" id="phClearPts">점 지우기</button>
        ${counter}
      </div>
      <div class="note">직접 그린 손상은 <b>재분석해도 보존</b>됩니다.</div>`;
    el("phSaveDefect").addEventListener("click", phSaveDefect);
  }
  el("phClearPts")?.addEventListener("click", () => {
    PH.pts = [];
    phRenderCanvas();
    phRenderToolPanel();
  });
}

function phRenderMeta() {
  const d = PH.data;
  const stateMap = {
    analyzed: ["ok", "분석 완료"],
    needs_scale: ["warn", "정보 부족"],
    pending: ["mute", "분석 전"],
    failed: ["crit", "실패"],
  };
  const [cls, label] = stateMap[d.analysis_state] || ["mute", d.analysis_state];

  el("phMeta").innerHTML = `
    <div class="chips">
      ${chip("분석 상태", `<span class="badge ${cls}">${label}</span>`)}
      ${chip("스케일", `<span class="num">${d.mm_per_px ? num(d.mm_per_px, 4) : "—"}</span>`, "mm/px",
             d.mm_per_px ? "" : "warn")}
      ${chip("선명도", `<span class="num">${d.sharpness == null ? "—" : num(d.sharpness, 0)}</span>`, "",
             d.sharpness != null && d.sharpness < 45 ? "warn" : "")}
      ${chip("크기", `<span class="num">${d.size[0]}×${d.size[1]}</span>`, "px")}
      ${d.rectified ? chip("보정", '<span class="badge info">원근 보정됨</span>') : ""}
    </div>
    ${d.analysis_note ? `<div class="alert" style="margin-top:10px">${esc(d.analysis_note)}</div>` : ""}`;
}

function phRenderDefects() {
  const d = PH.data;
  el("phDefCount").textContent =
    `자동 ${d.defects.filter((x) => x.source !== "manual").length} · ` +
    `수동 ${d.defects.filter((x) => x.source === "manual").length}`;
  renderTable(
    el("phDefects"),
    [
      { h: "유형", render: (x) => esc(x.type_label) },
      { h: "등급", render: (x) => gradeBadge(x.grade) },
      { h: "폭(mm)", cls: "num", render: (x) => num(x.width_mm, 3) },
      { h: "길이(mm)", cls: "num", render: (x) => num(x.length_mm, 0) },
      {
        h: "출처",
        render: (x) =>
          x.source === "manual"
            ? '<span class="badge info">수동</span>'
            : '<span class="badge mute">AI</span>',
      },
    ],
    d.defects,
    "결함이 없습니다"
  );
}

/* ─── 동작 ─────────────────────────────────────────────── */
async function phCall(fn, label) {
  if (PH.busy) return;
  PH.busy = true;
  const hint = el("phHint");
  const prev = hint.textContent;
  hint.innerHTML = `<span class="spin"></span> ${label} 처리 중…`;
  try {
    const out = await fn();
    PH.data = await api(`/api/photos/${PH.data.id}`);
    PH.pts = [];
    phRender();
    if (out?.note) el("phHint").textContent = out.note;
    if (typeof phOnChange === "function") phOnChange();
    return out;
  } catch (e) {
    hint.innerHTML = `<span style="color:var(--crit)">${label} 실패: ${esc(e.message)}</span>`;
  } finally {
    PH.busy = false;
    if (hint.textContent === "") hint.textContent = prev;
  }
}

function phApplyScale() {
  const mm = parseFloat(el("phRealMm").value);
  if (!mm || mm <= 0) {
    el("phHint").innerHTML =
      '<span style="color:var(--bad)">기준물의 실제 길이를 mm로 입력하십시오.</span>';
    return;
  }
  const [p1, p2] = PH.pts;
  phCall(
    () =>
      api(`/api/photos/${PH.data.id}/scale`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ p1, p2, real_mm: mm, reanalyze: true }),
      }),
    "치수 측정"
  );
}

function phApplyRectify() {
  const rw = parseFloat(el("phRw").value) || null;
  const rh = parseFloat(el("phRh").value) || null;
  phCall(
    () =>
      api(`/api/photos/${PH.data.id}/rectify`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          quad: PH.pts, real_width_mm: rw, real_height_mm: rh, reanalyze: true,
        }),
      }),
    "원근 보정"
  );
}

function phSaveDefect() {
  const body = {
    photo_id: PH.data.id,
    defect_type: el("phDType").value,
    polyline: PH.pts,
    width_mm: parseFloat(el("phDWidth").value) || null,
    note: el("phDNote").value || "",
  };
  phCall(
    () =>
      api("/api/defects/manual", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }),
    "손상 저장"
  );
}

function phReanalyze() {
  phCall(
    () =>
      api(`/api/photos/${PH.data.id}/reanalyze`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ sensitivity: 1.0 }),
      }),
    "재분석"
  );
}

function phUndoRectify() {
  phCall(
    () => api(`/api/photos/${PH.data.id}/rectify`, { method: "DELETE" }),
    "보정 취소"
  );
}

/* 다른 화면이 갱신을 원하면 이 훅을 덮어쓴다 */
let phOnChange = null;
