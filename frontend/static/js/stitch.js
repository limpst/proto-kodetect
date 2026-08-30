/* 이미지 자동 정합 화면
 *
 * 왜 이 화면이 필요한가
 * ----------------------
 * 현장에서 벽면 한 면을 찍으면 수십 장이 나온다. 지금까지는 장마다 따로
 * 검출했는데, 겹친 영역의 같은 균열이 장마다 다시 세어졌다. 기술자가
 * 나중에 눈으로 대조해 지우는 게 수작업 시간의 큰 몫이었다.
 *
 * 정합해서 한 장으로 만들면 그 대조가 사라진다. 이 화면은 그 효과를
 * 주장이 아니라 **숫자 두 개**로 보여준다 — 타일별 합계와 파노라마 통합.
 */

const ST = {
  groups: [],
  result: null,
  layer: "detect", // detect | layout
  hot: -1,
  busy: false,
};

/* 정합이 실제로 밟는 단계 */
const ST_STEPS = [
  ["특징 추출", "CLAHE 후 SIFT — 콘크리트 저대비 텍스처 대응"],
  ["쌍 매칭", "Lowe 비율 + RANSAC 호모그래피 역검증"],
  ["그래프 정합", "최대 연결요소 선택 후 BFS 변환 누적"],
  ["파노라마 합성", "거리변환 가중 블렌딩 · GSD 전파"],
  ["통합 검출", "합쳐진 한 장에서 균열 재검출"],
];

/* 등급별 선 색. 다른 파일의 GRADE_COLOR 와 이름이 겹치면 스크립트 로딩이
   통째로 멈추므로 접두어를 붙인다 — 실제로 한 번 겪은 사고다. */
const ST_GRADE_STROKE = {
  a: "#22c55e", b: "#84cc16", c: "#eab308", d: "#f97316", e: "#ef4444",
  A: "#22c55e", B: "#84cc16", C: "#eab308", D: "#f97316", E: "#ef4444",
};

function stRenderSteps(active, failAt = -1) {
  const node = el("stSteps");
  if (!node) return;
  node.innerHTML = ST_STEPS.map((s, i) => {
    let cls = "step";
    if (failAt === i) cls += " fail";
    else if (i < active) cls += " done";
    else if (i === active) cls += " active";
    return `<div class="${cls}" title="${esc(s[1])}">
              <span class="n">${i + 1}</span>${esc(s[0])}
            </div>`;
  }).join("");
}

async function loadStitch() {
  const insp = el("stInsp");
  if (!insp) return;
  insp.innerHTML = inspectionOptions();
  stRenderSteps(-1);
  await stLoadGroups();
}

async function stLoadGroups() {
  const sel = el("stGroup");
  const iid = el("stInsp").value;
  if (!iid) {
    sel.innerHTML = `<option value="">점검 회차가 없습니다</option>`;
    el("stPre").innerHTML = `<div class="alert warn">점검 회차를 먼저 만드십시오.</div>`;
    el("stRun").disabled = true;
    return;
  }
  ST.groups = (await api(`/api/groups?inspection_id=${iid}`)).filter((g) => g.id);
  sel.innerHTML = ST.groups.length
    ? ST.groups
        .map((g) => `<option value="${g.id}">${esc(g.name)} · ${g.photo_count}장</option>`)
        .join("")
    : `<option value="">사진 그룹이 없습니다</option>`;
  await stEstimate();
}

/** 실행 전 사전 점검 — 무엇이 걸리는지 먼저 알려준다. */
async function stEstimate() {
  const gid = el("stGroup").value;
  const box = el("stPre");
  if (!gid) {
    box.innerHTML = `<div class="alert warn">
      사진 그룹이 없습니다. <b>현장 · 사진 그룹</b>에서 부재 단위로 사진을 묶은
      뒤 이 화면으로 돌아오십시오.</div>`;
    el("stRun").disabled = true;
    return;
  }
  box.innerHTML = `<div class="note">사전 점검 중…</div>`;
  try {
    const e = await api(`/api/stitch/estimate?group_id=${gid}`);
    const parts = [];
    parts.push(
      e.blockers.length
        ? `<div class="alert bad"><b>정합할 수 없습니다</b><br/>${e.blockers
            .map((b) => esc(b))
            .join("<br/>")}</div>`
        : `<div class="alert ok">사진 ${e.photo_count}장 — 정합할 수 있습니다.</div>`
    );
    if (e.notes.length) {
      parts.push(
        `<div class="note" style="margin-top:8px">${e.notes
          .map((n) => "· " + esc(n))
          .join("<br/>")}</div>`
      );
    }
    box.innerHTML = parts.join("");
    el("stRun").disabled = !e.can_stitch || ST.busy;
  } catch (err) {
    box.innerHTML = `<div class="alert bad">${esc(String(err.message || err))}</div>`;
    el("stRun").disabled = true;
  }
}

async function runStitch() {
  const gid = Number(el("stGroup").value);
  if (!gid || ST.busy) return;
  ST.busy = true;
  ST.result = null;
  ST.hot = -1;
  el("stRun").disabled = true;
  el("stRun").textContent = "정합 중…";
  el("stOut").innerHTML = "";
  el("stCompare").innerHTML = "";

  // 서버가 단계별 진행을 밀어주지 않으므로 소요 추정으로 단계를 넘긴다.
  // 마지막 단계는 실제 응답이 와야 채운다 — 가짜 완료를 만들지 않는다.
  const n = (ST.groups.find((g) => g.id === gid) || {}).photo_count || 4;
  let phase = 0;
  stRenderSteps(0);
  const tick = setInterval(() => {
    if (phase < ST_STEPS.length - 1) stRenderSteps(++phase);
  }, Math.max(1200, (n * 4000) / ST_STEPS.length));

  try {
    const r = await api("/api/stitch/group", {
      method: "POST",
      body: JSON.stringify({
        group_id: gid,
        ordered: el("stOrdered").checked,
        detect_on_panorama: el("stDetect").checked,
        replace_defects: el("stReplace").checked,
      }),
    });
    clearInterval(tick);
    ST.result = r;
    if (!r.ok) {
      stRenderSteps(2, 2);
      renderStitchFail(r);
    } else {
      stRenderSteps(ST_STEPS.length);
      renderStitch(r);
      if (window.refreshGroups) refreshGroups().catch(() => {});
    }
  } catch (err) {
    clearInterval(tick);
    stRenderSteps(0, 0);
    el("stOut").innerHTML = `<div class="card"><div class="alert bad">${esc(
      String(err.message || err)
    )}</div></div>`;
  } finally {
    ST.busy = false;
    el("stRun").textContent = "정합 실행";
    stEstimate();
  }
}

function renderStitchFail(r) {
  el("stOut").innerHTML = `
    <div class="card">
      <h2>정합하지 못했습니다</h2>
      <div class="alert bad">${(r.warnings || []).map((w) => esc(w)).join("<br/>")}</div>
      <div class="note" style="margin-top:10px">
        정합에는 이웃한 사진끼리 <b>30% 이상 겹침</b>이 필요합니다. 같은 면을
        옆으로 이동하며 촬영하고, 시점이 한 번에 크게 바뀌지 않게 하십시오.
        서로 다른 면을 한 그룹에 담으면 겹칠 영역이 없어 정합되지 않습니다.
        이 결과는 고장이 아니라 <b>겹침이 없다는 사실을 정확히 보고한 것</b>입니다.
      </div>
    </div>`;
}

function renderStitch(r) {
  const tile = r.tile_defect_count;
  const uni = r.crack_count;
  const dup = tile != null && uni != null ? Math.max(0, tile - uni) : null;

  const kpis = [
    kpi("정합 사진", `<span class="num">${r.placed}</span>`, `/ ${r.total}장`,
        r.placed === r.total ? "전부 배치됨" : `${r.total - r.placed}장 제외`,
        r.placed === r.total ? "ok" : "warn"),
    kpi("커버리지", `<span class="num">${num(r.coverage * 100, 1)}</span>`, "%",
        "캔버스에서 실제로 채워진 비율"),
    kpi("파노라마 GSD", `<span class="num">${r.mm_per_px ? num(r.mm_per_px, 4) : "—"}</span>`,
        "mm/px", "원본 실척을 면적비로 전파"),
    kpi("배율 편차", `<span class="num">${num(r.scale_drift, 3)}</span>`, "×",
        "1.000에 가까울수록 왜곡이 적음",
        Math.abs(r.scale_drift - 1) < 0.05 ? "ok" : "warn"),
    kpi("매칭 쌍", `<span class="num">${int(r.pairs)}</span>`, "쌍",
        `평균 신뢰도 ${num(r.mean_confidence, 3)}`),
    kpi("소요", `<span class="num">${num(r.elapsed_sec, 1)}</span>`, "초",
        `캔버스 ${r.canvas[0]}×${r.canvas[1]} px`),
  ].join("");

  const warn = (r.warnings || []).length
    ? `<div class="alert warn" style="margin-top:10px">${r.warnings
        .map((w) => esc(w))
        .join("<br/>")}</div>`
    : "";

  const dropped = (r.dropped || []).length
    ? `<div class="note" style="margin-top:8px">
         정합에서 빠진 사진 ${r.dropped.length}장 —
         ${r.dropped.slice(0, 6).map((d) => esc(d)).join(", ")}${
           r.dropped.length > 6 ? " 외" : ""
         }. 다른 사진과 겹치는 영역을 찾지 못했습니다. 이 사진들의 결함은
         원본 그대로 남아 있습니다.
       </div>`
    : "";

  // ─── 중복 계상 비교 — 정합 효과를 숫자로 ─────────────────
  let effect = "";
  if (dup != null) {
    const pct = tile > 0 ? Math.round((dup / tile) * 100) : 0;
    effect = `
      <div class="row"><div class="card">
        <h2>중복 계상 제거 <span class="hint">정합이 시간을 줄이는 실제 경로</span></h2>
        <div class="dupbar">
          <div class="dup-side">
            <div class="dup-n num">${int(tile)}</div>
            <div class="dup-l">타일별 검출 합계</div>
            <div class="dup-d">겹친 영역의 같은 균열이 장마다 다시 계상됨</div>
          </div>
          <div class="dup-arrow">→</div>
          <div class="dup-side hi">
            <div class="dup-n num">${int(uni)}</div>
            <div class="dup-l">파노라마 통합 검출</div>
            <div class="dup-d">한 장에서 한 번 — 중복이 생길 수 없음</div>
          </div>
          <div class="dup-side cut">
            <div class="dup-n num">${int(dup)}</div>
            <div class="dup-l">대조해야 할 항목 감소</div>
            <div class="dup-d">${pct}% 감소</div>
          </div>
        </div>
        <div class="note" style="margin-top:12px">
          기술자가 겹친 사진을 눈으로 대조해 중복을 지우는 작업이
          <b>${int(dup)}건</b>만큼 사라집니다. 검출이 빨라져서가 아니라
          <b>확인할 것이 줄어서</b> 시간이 줄어듭니다.
          ${
            r.replaced_defects != null
              ? `<br/>원본 사진의 AI 결함 ${r.replaced_defects}건을 파노라마 결과로 교체했습니다. 기술자가 직접 입력한 결함은 지우지 않았습니다.`
              : `<br/>이번 실행은 원본 사진의 결함을 그대로 두었습니다. 교체하려면 <b>기존 AI 결함 교체</b>를 켜고 다시 실행하십시오.`
          }
        </div>
      </div></div>`;
  }

  const cracks = r.cracks || [];
  const table = cracks.length
    ? `<div class="table-wrap"><table><thead><tr>
         <th>#</th><th>등급</th><th>폭 P95(mm)</th><th>길이(mm)</th>
         <th>신뢰도</th><th>보수</th><th>판정 근거</th>
       </tr></thead><tbody>${cracks
         .map(
           (c) => `<tr data-st-crack="${c.index}">
             <td class="num">${c.index}</td>
             <td>${gradeBadge(c.grade)}</td>
             <td class="num">${num(c.width_mm_p95, 3)}</td>
             <td class="num">${num(c.length_mm, 0)}</td>
             <td class="num">${num(c.confidence, 2)}</td>
             <td>${c.repair_required ? '<span class="badge bad">필요</span>' : "—"}</td>
             <td>${esc(c.basis)}</td>
           </tr>`
         )
         .join("")}</tbody></table></div>`
    : `<div class="empty">파노라마에서 검출된 균열이 없습니다.</div>`;

  el("stOut").innerHTML = `
    <div class="row"><div class="card">
      <h2>정합 결과 <span class="hint">${esc(r.group.name)}</span></h2>
      <div class="kpi-grid">${kpis}</div>${warn}${dropped}
    </div></div>
    ${effect}
    ${
      r.crack_count != null
        ? `<div class="row"><div class="card">
             <h2>파노라마 통합 검출
               <span class="hint">점검 종합 안전등급 ${gradeBadge(r.inspection_grade)}</span>
             </h2>${table}
             <div class="note" style="margin-top:8px">
               폭은 중심선 법선 방향 FWHM으로 재고 촬영계 PSF를 보정한 값입니다.
               등급은 KDS 14 20 30 허용균열폭 대비로 판정합니다.
             </div>
           </div></div>`
        : ""
    }`;

  renderStitchCanvas(r);
  stBindRows();
}

function stBindRows() {
  document.querySelectorAll("tr[data-st-crack]").forEach((tr) =>
    tr.addEventListener("click", () => {
      const i = Number(tr.dataset.stCrack);
      ST.hot = ST.hot === i ? -1 : i;
      stSyncRows();
      stPaintMarks();
    })
  );
}

function stSyncRows() {
  document
    .querySelectorAll("tr[data-st-crack]")
    .forEach((t) => t.classList.toggle("pick", Number(t.dataset.stCrack) === ST.hot));
}

/** 파노라마 + 겹치는 SVG (배치 윤곽 / 균열 중심선) */
function renderStitchCanvas(r) {
  const [w, h] = r.canvas;
  const img = ST.layer === "detect" && r.overlay_url ? r.overlay_url : r.panorama_url;
  el("stCompare").innerHTML = `
    <div class="row"><div class="card">
      <h2>파노라마
        <span class="hint">${w}×${h} px${
          r.mm_per_px ? ` · 실제 폭 약 ${num((w * r.mm_per_px) / 1000, 2)} m` : ""
        }</span>
      </h2>
      <div class="field-row">
        <button class="${ST.layer === "detect" ? "primary" : "ghost"}" data-st-layer="detect">검출 결과</button>
        <button class="${ST.layer === "layout" ? "primary" : "ghost"}" data-st-layer="layout">사진 배치</button>
        <a href="${r.panorama_url}" target="_blank" rel="noopener"><button class="ghost">원본 열기</button></a>
      </div>
      <div class="shot" id="stShot">
        <img src="${img}" alt="정합 파노라마" />
        <svg class="marks" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" id="stMarks"></svg>
      </div>
      <div class="note" style="margin-top:8px" id="stCanvasNote"></div>
    </div></div>`;

  el("stCompare")
    .querySelectorAll("[data-st-layer]")
    .forEach((b) =>
      b.addEventListener("click", () => {
        ST.layer = b.dataset.stLayer;
        renderStitchCanvas(ST.result);
      })
    );
  stPaintMarks();
}

function stPaintMarks() {
  const r = ST.result;
  const svg = el("stMarks");
  if (!r || !svg) return;
  const note = el("stCanvasNote");

  if (ST.layer === "layout") {
    // 원본이 파노라마 어디에 놓였는지 — 겹침이 눈에 보인다
    const fs = Math.max(14, r.canvas[0] / 55);
    svg.innerHTML = (r.placements || [])
      .map((p, i) => {
        const pts = p.corners.map((c) => c.join(",")).join(" ");
        const cx = p.corners.reduce((a, c) => a + c[0], 0) / 4;
        const cy = p.corners.reduce((a, c) => a + c[1], 0) / 4;
        const hue = (i * 47) % 360;
        return `<polygon points="${pts}" fill="hsla(${hue},80%,55%,0.10)"
                  stroke="hsl(${hue},80%,62%)" stroke-width="3" />
                <text x="${cx}" y="${cy}" fill="#fff" font-size="${fs}"
                  text-anchor="middle" dominant-baseline="middle"
                  style="paint-order:stroke;stroke:#000;stroke-width:4">${i + 1}</text>`;
      })
      .join("");
    note.innerHTML = `원본 사진 ${
      (r.placements || []).length
    }장의 배치입니다. 사각형이 겹친 부분이 바로 중복 계상이 일어나던 영역입니다.
      사각형이 기울어져 보이는 것은 호모그래피로 시점 차이를 편 결과입니다.`;
    return;
  }

  const cracks = r.cracks || [];
  svg.innerHTML = cracks
    .map((c) => {
      const pts = (c.polyline || []).map((p) => p.join(",")).join(" ");
      if (!pts) return "";
      const cls = ST.hot < 0 ? "cr" : ST.hot === c.index ? "cr hot" : "cr dim";
      const col = ST_GRADE_STROKE[c.grade] || "#38bdf8";
      return `<polyline class="${cls}" points="${pts}" stroke="${col}" data-i="${c.index}" />`;
    })
    .join("");
  svg.querySelectorAll("polyline").forEach((p) =>
    p.addEventListener("click", () => {
      const i = Number(p.dataset.i);
      ST.hot = ST.hot === i ? -1 : i;
      stSyncRows();
      stPaintMarks();
    })
  );
  note.innerHTML = cracks.length
    ? `균열 ${cracks.length}건. 선을 누르면 아래 표와 연동됩니다.`
    : `검출된 균열이 없습니다. 파노라마 원본을 열어 육안으로 확인하십시오.`;
}

document.addEventListener("DOMContentLoaded", () => {
  el("stInsp")?.addEventListener("change", () => stLoadGroups().catch(console.error));
  el("stGroup")?.addEventListener("change", () => stEstimate().catch(console.error));
  el("stRun")?.addEventListener("click", () => runStitch().catch(console.error));
});
