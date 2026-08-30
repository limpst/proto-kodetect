/* 진단 · 영상 분석 화면
 *
 * 설계 의도
 * ---------
 * 검출 결과를 "그림 한 장"으로 던지면 기술자가 검증할 수 없다. 무엇을 근거로
 * 그 폭이 나왔는지, 어느 균열이 어느 행인지, 촬영이 믿을 만했는지를 화면에서
 * 확인할 수 있어야 판정에 서명할 수 있다. 그래서 세 가지를 넣었다.
 *
 *  1) 원본 ↔ 검출 비교 슬라이더 — 검출이 실제 균열 위에 놓였는지 눈으로 대조
 *  2) 중심선 SVG 오버레이 — 표의 행과 영상의 균열을 양방향으로 잇는다
 *  3) 촬영 품질 게이지 — 선명도·스케일이 없으면 폭은 신뢰할 수 없다고 먼저 말한다
 */

const DT = {
  result: null,
  file: null,
  mode: "compare",     // compare | marks | overlay
  selected: null,      // 선택된 균열 index (1-based)
  split: 50,
};

const GRADE_HEX = { a: "#22c55e", b: "#84cc16", c: "#eab308", d: "#f97316", e: "#ef4444" };

const DT_STEPS = [
  ["사진 선택", (r, f) => (r || f ? "done" : "active")],
  ["스케일 산정", (r) => (!r ? "" : r.mm_per_px ? "done" : "fail")],
  ["균열 검출", (r) => (!r ? "" : "done")],
  ["폭 측정", (r) => (!r ? "" : r.mm_per_px ? "done" : "fail")],
  ["등급 판정", (r) => (!r ? "" : r.crack_count ? "done" : "")],
];

function dtRenderSteps() {
  const node = el("dtSteps");
  if (!node) return;
  node.innerHTML = DT_STEPS.map(([label, state], i) => {
    const cls = state(DT.result, DT.file) || "";
    return `<div class="step ${cls}"><span class="n">${i + 1}</span>${label}</div>`;
  }).join("");
}

/* ─── 업로드 ───────────────────────────────────────────── */
function dtSetFile(file) {
  DT.file = file || null;
  const dz = el("dtDrop");
  if (!dz) return;
  if (!file) {
    dz.classList.remove("has");
    dz.innerHTML =
      `<div class="dz-ico">⬓</div>
       <div class="dz-t">사진을 여기에 놓으십시오</div>
       <div class="dz-s">JPG · PNG · WebP · 최대 20MB</div>`;
  } else {
    dz.classList.add("has");
    const url = URL.createObjectURL(file);
    dz.innerHTML =
      `<div class="dz-file">
         <img src="${url}" alt="" />
         <div class="meta">
           <div class="nm" title="${esc(file.name)}">${esc(file.name)}</div>
           <div class="sz">${(file.size / 1024 / 1024).toFixed(2)} MB</div>
           <div class="sz" style="color:var(--accent)">다른 사진을 놓으면 교체됩니다</div>
         </div>
       </div>`;
  }
  dtRenderSteps();
}

function dtBindDropzone() {
  const dz = el("dtDrop");
  const input = el("dtFile");
  if (!dz || !input) return;

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => dtSetFile(input.files[0]));

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("over");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove("over");
    })
  );
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f && f.type.startsWith("image/")) dtSetFile(f);
  });
}

/* ─── 결과 뷰어 ─────────────────────────────────────────── */
function dtOriginalUrl(d) {
  return `/media/uploads/${d.filename}`;
}

function dtRenderViewer() {
  const node = el("dtViewer");
  const d = DT.result;
  if (!node) return;
  if (!d) {
    node.className = "empty";
    node.textContent = "분석 결과가 여기에 표시됩니다";
    return;
  }
  node.className = "";

  if (DT.mode === "overlay") {
    node.innerHTML = `<img class="overlay-img" src="${d.overlay_url}?t=${Date.now()}" alt="검출 오버레이" />`;
    el("dtViewNote").textContent = "서버가 그린 오버레이 — 인쇄·보고서에 쓰는 원본입니다";
    return;
  }

  if (DT.mode === "compare") {
    node.innerHTML =
      `<div class="compare" id="dtCmp" style="--split:${DT.split}%">
         <img src="${dtOriginalUrl(d)}" alt="원본" />
         <div class="after" id="dtCmpAfter" style="clip-path:inset(0 0 0 ${DT.split}%)">
           <img src="${d.overlay_url}?t=${Date.now()}" alt="검출" />
         </div>
         <div class="handle" id="dtCmpHandle" style="left:${DT.split}%"></div>
         <span class="lbl l">원본</span><span class="lbl r">검출</span>
       </div>`;
    el("dtViewNote").textContent = "가운데 손잡이를 끌어 원본과 대조하십시오";
    dtBindCompare();
    return;
  }

  // 중심선 모드 — 원본 위에 SVG로 균열을 직접 그린다
  const [h, w] = d.image_size;
  const marks = d.cracks
    .map((c) => {
      const color = GRADE_HEX[c.grade] || "#38bdf8";
      const pts = (c.polyline || []).map((p) => p.join(",")).join(" ");
      const [x, y, bw, bh] = c.bbox;
      const line = pts
        ? `<polyline class="cr" data-i="${c.index}" points="${pts}" stroke="${color}" />`
        : "";
      return `${line}<rect class="bx" data-i="${c.index}" x="${x}" y="${y}"
                width="${bw}" height="${bh}" stroke="${color}" />`;
    })
    .join("");

  node.innerHTML =
    `<div class="shot" id="dtShot">
       <img src="${dtOriginalUrl(d)}" alt="원본" />
       <svg class="marks" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${marks}</svg>
     </div>`;
  el("dtViewNote").textContent =
    "중심선은 검출기가 실제로 측정한 경로입니다 — 클릭하면 표에서 선택됩니다";

  node.querySelectorAll("svg.marks .cr").forEach((n) =>
    n.addEventListener("click", () => dtSelect(Number(n.dataset.i)))
  );
  dtApplySelection();
}

function dtBindCompare() {
  const wrap = el("dtCmp");
  if (!wrap) return;
  const move = (clientX) => {
    const r = wrap.getBoundingClientRect();
    const pct = Math.max(2, Math.min(98, ((clientX - r.left) / r.width) * 100));
    DT.split = pct;
    el("dtCmpAfter").style.clipPath = `inset(0 0 0 ${pct}%)`;
    el("dtCmpHandle").style.left = `${pct}%`;
  };
  let dragging = false;
  wrap.addEventListener("pointerdown", (e) => {
    dragging = true;
    wrap.setPointerCapture(e.pointerId);
    move(e.clientX);
  });
  wrap.addEventListener("pointermove", (e) => dragging && move(e.clientX));
  wrap.addEventListener("pointerup", (e) => {
    dragging = false;
    try { wrap.releasePointerCapture(e.pointerId); } catch (_) {}
  });
}

/* ─── 선택 연동 ─────────────────────────────────────────── */
function dtSelect(index) {
  DT.selected = DT.selected === index ? null : index;
  dtApplySelection();
}

function dtApplySelection() {
  const sel = DT.selected;
  document.querySelectorAll("#dtCracks tr[data-crack]").forEach((tr) =>
    tr.classList.toggle("pick", Number(tr.dataset.crack) === sel)
  );
  document.querySelectorAll("#dtShot svg.marks .cr").forEach((n) => {
    const i = Number(n.dataset.i);
    n.classList.toggle("hot", sel === i);
    n.classList.toggle("dim", sel !== null && sel !== i);
  });
  document.querySelectorAll("#dtShot svg.marks .bx").forEach((n) =>
    n.classList.toggle("hot", sel === Number(n.dataset.i))
  );

  const note = el("dtSelNote");
  if (note) {
    const c = sel ? DT.result?.cracks.find((x) => x.index === sel) : null;
    note.innerHTML = c
      ? `#${c.index} 선택 · 폭 <b class="num">${num(c.width_mm_p95, 3)}</b>mm ·
         길이 <b class="num">${num(c.length_mm, 0)}</b>mm`
      : "";
  }
}

/* ─── 품질 · 통계 ───────────────────────────────────────── */
function dtRenderQuality(d) {
  // 선명도는 라플라시안 분산이라 상한이 없다. 45를 통과선으로 두고 300에서 포화시킨다.
  const sharpPct = Math.min(100, (d.sharpness / 300) * 100);
  const sharpOk = d.sharpness >= 45;
  const scaleOk = !!d.mm_per_px;

  el("dtQuality").innerHTML = `
    <div class="gauge" style="margin-bottom:14px">
      <div class="g-top">
        <span class="g-v">${num(d.sharpness, 0)}</span>
        <span class="g-u">선명도</span>
        <span class="g-lim">통과선 45</span>
      </div>
      <div class="g-bar">
        <i style="width:${sharpPct.toFixed(0)}%;background:${sharpOk ? "var(--ok)" : "var(--bad)"}"></i>
        <span class="mark" style="left:15%"></span>
      </div>
      <div class="note">${
        sharpOk
          ? "선명도 충분 — 폭 측정이 신뢰 구간에 있습니다"
          : "흐린 사진입니다. 균열이 번져 폭이 과대평가됩니다 — 재촬영을 권고합니다"
      }</div>
    </div>

    <div class="gauge">
      <div class="g-top">
        <span class="g-v">${scaleOk ? num(d.mm_per_px, 4) : "—"}</span>
        <span class="g-u">mm/px</span>
      </div>
      <div class="note">${esc(d.gsd_source)}</div>
    </div>

    <div class="chips" style="margin-top:14px">
      ${chip("검출 균열", `<span class="num">${int(d.crack_count)}</span>`, "건")}
      ${chip("균열 면적률", `<span class="num">${num(d.crack_area_ratio * 100, 3)}</span>`, "%")}
      ${chip("점검 종합등급", gradeBadge(d.inspection_grade))}
    </div>`;
}

function dtRenderGrades(d) {
  const order = ["a", "b", "c", "d", "e"];
  const counts = Object.fromEntries(order.map((g) => [g, 0]));
  d.cracks.forEach((c) => (counts[c.grade] = (counts[c.grade] || 0) + 1));
  const total = d.cracks.length || 1;

  el("dtGrades").innerHTML = `
    <div class="stack">
      ${order
        .filter((g) => counts[g])
        .map(
          (g) =>
            `<i style="width:${((counts[g] / total) * 100).toFixed(1)}%;background:${GRADE_HEX[g]}"
                title="${g.toUpperCase()} ${counts[g]}건">${counts[g]}</i>`
        )
        .join("") || '<i style="width:100%;background:var(--elev);color:var(--text-mute)">0</i>'}
    </div>
    <div class="stack-lg">
      ${order
        .map(
          (g) =>
            `<span><i style="background:${GRADE_HEX[g]}"></i>${g.toUpperCase()}
               <b class="num">${counts[g]}</b></span>`
        )
        .join("")}
    </div>
    <div class="note" style="margin-top:10px">
      보수 필요 <b class="num">${int(d.cracks.filter((c) => c.repair_required).length)}</b>건 —
      허용균열폭을 넘긴 균열입니다.
    </div>`;
}

function dtRenderHist(d) {
  const widths = d.cracks.map((c) => c.width_mm_p95).filter((v) => v != null);
  const node = el("dtHist");
  if (!widths.length) {
    node.innerHTML = '<div class="empty">폭 데이터가 없습니다</div>';
    return;
  }
  // 판정 경계(0.1·0.2·0.3·1.0)에 맞춘 구간 — 통계용이 아니라 판정용 히스토그램이다
  const edges = [0, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, Infinity];
  const labels = ["<0.1", "~0.2", "~0.3", "~0.5", "~1.0", "~2.0", "2.0+"];
  const bins = new Array(labels.length).fill(0);
  widths.forEach((w) => {
    for (let i = 0; i < labels.length; i++) {
      if (w < edges[i + 1]) { bins[i]++; break; }
    }
  });
  const max = Math.max(...bins, 1);
  const colors = ["#22c55e", "#84cc16", "#eab308", "#f97316", "#f97316", "#ef4444", "#ef4444"];

  node.innerHTML = `
    <div class="hist">
      ${bins
        .map(
          (n, i) =>
            `<div class="b" style="height:${(n / max) * 100}%;background:${colors[i]}"
                  title="${labels[i]}mm · ${n}건"></div>`
        )
        .join("")}
    </div>
    <div class="hist-x">${labels.map((l) => `<span>${l}</span>`).join("")}</div>
    <div class="note" style="margin-top:8px">
      중앙값 <b class="num">${num(
        widths.slice().sort((a, b) => a - b)[Math.floor(widths.length / 2)], 3
      )}</b>mm ·
      최대 <b class="num">${num(Math.max(...widths), 3)}</b>mm
    </div>`;
}

function dtRenderTable(d) {
  renderTable(
    el("dtCracks"),
    [
      { h: "#", cls: "num", render: (c) => int(c.index) },
      { h: "등급", render: (c) => gradeBadge(c.grade) },
      { h: "폭 p95(mm)", cls: "num", render: (c) => num(c.width_mm_p95, 3) },
      { h: "최대(mm)", cls: "num", render: (c) => num(c.width_mm_max, 3) },
      { h: "길이(mm)", cls: "num", render: (c) => num(c.length_mm, 0) },
      { h: "신뢰도", cls: "num", render: (c) => num(c.confidence, 2) },
      {
        h: "조치",
        render: (c) =>
          c.repair_required
            ? '<span class="badge bad">보수 필요</span>'
            : '<span class="badge mute">경과관찰</span>',
      },
      { h: "판정 근거", render: (c) => esc(c.basis) },
    ],
    d.cracks,
    "검출된 균열이 없습니다"
  );
  // 행에 균열 번호를 붙여 영상과 양방향으로 잇는다
  el("dtCracks")
    .querySelectorAll("tbody tr")
    .forEach((tr, i) => {
      const c = d.cracks[i];
      if (!c) return;
      tr.dataset.crack = c.index;
      tr.addEventListener("click", () => dtSelect(c.index));
    });
}

/* ─── 전체 렌더 ─────────────────────────────────────────── */
function dtRender(d) {
  DT.result = d;
  DT.selected = null;
  el("dtHint").textContent =
    `${d.image_size[1]}×${d.image_size[0]}px · 균열 ${d.crack_count}건`;

  dtRenderSteps();
  dtRenderViewer();
  dtRenderQuality(d);
  dtRenderGrades(d);
  dtRenderHist(d);
  dtRenderTable(d);

  const quality = d.quality_ok ? "" : `<div class="alert">${esc(d.quality_note)}</div>`;
  el("dtStatus").innerHTML =
    quality +
    `<div class="alert info">분석 완료 — 결함 ${d.crack_count}건을 점검 회차에 저장하고
     종합 안전등급을 ${String(d.inspection_grade || "-").toUpperCase()} 로 갱신했습니다.</div>`;
}

/* ─── 실행 ─────────────────────────────────────────────── */
async function dtRun() {
  if (!DT.file) {
    el("dtStatus").innerHTML =
      '<div class="alert">사진을 먼저 선택하거나 “합성 표본 시연”을 누르십시오.</div>';
    return;
  }
  const fd = new FormData();
  fd.append("file", DT.file);
  fd.append("inspection_id", el("dtInsp").value);
  fd.append("member_code", el("dtMember").value);
  fd.append("sensitivity", el("dtSens").value);
  if (el("dtDist").value) fd.append("distance_m", el("dtDist").value);
  if (el("dtGsd").value) fd.append("gsd_mm_per_px_in", el("dtGsd").value);

  el("dtStatus").innerHTML =
    '<div class="alert info"><span class="spin"></span> 분석 중…</div>';
  try {
    dtRender(await api("/api/detect", { method: "POST", body: fd }));
    await dtRefresh();
  } catch (e) {
    el("dtStatus").innerHTML = `<div class="alert critical">분석 실패: ${esc(e.message)}</div>`;
  }
}

async function dtDemo() {
  el("dtStatus").innerHTML =
    '<div class="alert info"><span class="spin"></span> 합성 표본 생성 및 분석 중…</div>';
  try {
    const seed = Math.floor(Math.random() * 100000);
    const d = await api(
      `/api/detect/demo?inspection_id=${el("dtInsp").value}&seed=${seed}` +
        `&member_code=${encodeURIComponent(el("dtMember").value)}`,
      { method: "POST" }
    );
    dtRender(d);
    await dtRefresh();
  } catch (e) {
    el("dtStatus").innerHTML = `<div class="alert critical">시연 실패: ${esc(e.message)}</div>`;
  }
}

async function dtRefresh() {
  App.inspections = await api(`/api/inspections?building_id=${App.buildingId}`);
  State.detail = await api(`/api/buildings/${App.buildingId}`);
  if (typeof renderOverview === "function") renderOverview();
}

function dtCsv() {
  const d = DT.result;
  if (!d) return;
  const head = "번호,등급,폭_p95_mm,최대폭_mm,길이_mm,신뢰도,보수필요,판정근거";
  const rows = d.cracks.map((c) =>
    [
      c.index, c.grade, c.width_mm_p95 ?? "", c.width_mm_max ?? "",
      c.length_mm ?? "", c.confidence, c.repair_required ? "Y" : "N",
      `"${(c.basis || "").replace(/"/g, '""')}"`,
    ].join(",")
  );
  // BOM 을 붙여야 엑셀에서 한글이 깨지지 않는다
  const blob = new Blob(["﻿" + [head, ...rows].join("\r\n")], {
    type: "text/csv;charset=utf-8",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `KO-Detect_균열_${d.photo_id}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function dtSetMode(mode) {
  DT.mode = mode;
  ["Compare", "Marks", "Overlay"].forEach((m) => {
    const b = el("dtMode" + m);
    if (b) b.classList.toggle("primary", m.toLowerCase() === mode);
  });
  dtRenderViewer();
}

function dtBind() {
  dtBindDropzone();
  dtRenderSteps();
  dtSetMode("compare");

  el("dtRun")?.addEventListener("click", dtRun);
  el("dtDemo")?.addEventListener("click", dtDemo);
  el("dtCsv")?.addEventListener("click", dtCsv);
  el("dtModeCompare")?.addEventListener("click", () => dtSetMode("compare"));
  el("dtModeMarks")?.addEventListener("click", () => dtSetMode("marks"));
  el("dtModeOverlay")?.addEventListener("click", () => dtSetMode("overlay"));

  el("dtReset")?.addEventListener("click", () => {
    DT.result = null;
    DT.selected = null;
    dtSetFile(null);
    el("dtFile").value = "";
    el("dtStatus").innerHTML = "";
    el("dtHint").textContent = "분석을 실행하면 표시됩니다";
    ["dtQuality", "dtGrades", "dtHist"].forEach((id) => (el(id).innerHTML = ""));
    el("dtCracks").innerHTML = "";
    dtRenderViewer();
  });

  const sens = el("dtSens");
  sens?.addEventListener("input", () => (el("dtSensVal").textContent = sens.value));

  const upd = () => {
    const d = el("dtDist").value, g = el("dtGsd").value;
    el("dtScaleNote").textContent = g
      ? `직접 입력한 ${g} mm/px 를 씁니다.`
      : d
      ? `촬영거리 ${d}m 로 GSD를 산정합니다.`
      : "스케일이 없으면 균열폭을 mm로 환산할 수 없어 등급 판정이 성립하지 않습니다.";
  };
  el("dtDist")?.addEventListener("input", upd);
  el("dtGsd")?.addEventListener("input", upd);
}

document.addEventListener("DOMContentLoaded", dtBind);
