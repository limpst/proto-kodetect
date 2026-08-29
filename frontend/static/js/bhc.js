/* 건축물 건강검진 대시보드 (BHC-STD-2026)
   게이지·레이더는 라이브러리 없이 SVG로 직접 그린다 — 등급 색과 임계선을
   표준에 정확히 맞춰야 하는데, 범용 차트 라이브러리는 그 통제가 어렵다. */

const BHC = { data: null, capa: null };

const GRADE_COLOR = {
  A: "var(--g-a)", B: "var(--g-b)", C: "var(--g-c)",
  D: "var(--g-d)", E: "var(--g-e)",
};
const SEVERITY_COLOR = {
  D1: "#22c55e", D2: "#84cc16", D3: "#eab308", D4: "#f97316", D5: "#ef4444",
};
const SEVERITY_LABEL = {
  D1: "정상", D2: "관찰", D3: "주의", D4: "위험", D5: "긴급",
};
const PRIORITY_TONE = { P0: "crit", P1: "bad", P2: "warn", P3: "info", P4: "mute" };

/* ─── BHI 게이지 ────────────────────────────────────────── */
function bhiGauge(bhi, bhiRaw, grade, gradeLabel) {
  // 240° 아크. 등급 경계(50·65·80·90)를 눈금으로 새겨 위치를 즉시 읽게 한다.
  const W = 260, H = 190, cx = W / 2, cy = 150, r = 104;
  const start = 180, sweep = 180;             // 반원
  const polar = (val) => {
    const a = ((start + (sweep * val) / 100) * Math.PI) / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  };
  const arc = (from, to, color, width) => {
    const [x1, y1] = polar(from), [x2, y2] = polar(to);
    const large = to - from > 50 ? 1 : 0;
    return `<path d="M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}"
            fill="none" stroke="${color}" stroke-width="${width}"
            stroke-linecap="butt" />`;
  };

  // 등급 구간을 배경 띠로
  const bands = [
    [0, 50, "var(--g-e)"], [50, 65, "var(--g-d)"], [65, 80, "var(--g-c)"],
    [80, 90, "var(--g-b)"], [90, 100, "var(--g-a)"],
  ].map(([a, b, c]) => arc(a, b, c, 13)).join("");

  // 지침(바늘)
  const [nx, ny] = polar(Math.max(0, Math.min(100, bhi)));
  const needle = `
    <line x1="${cx}" y1="${cy}" x2="${nx}" y2="${ny}" stroke="#fff"
          stroke-width="3" stroke-linecap="round" />
    <circle cx="${cx}" cy="${cy}" r="6" fill="#fff" />`;

  // 적신호 적용 전 위치를 흐린 눈금으로 — 얼마나 깎였는지 보이게 한다
  let rawMark = "";
  if (bhiRaw > bhi + 0.05) {
    const [rx, ry] = polar(Math.max(0, Math.min(100, bhiRaw)));
    const [ix, iy] = [cx + (r - 18) * (rx - cx) / r, cy + (r - 18) * (ry - cy) / r];
    rawMark = `<line x1="${ix}" y1="${iy}" x2="${rx}" y2="${ry}"
               stroke="var(--text-mute)" stroke-width="2" stroke-dasharray="3 2" />`;
  }

  const ticks = [50, 65, 80, 90].map((v) => {
    const [tx, ty] = polar(v);
    const [ix, iy] = [cx + (r - 20) * (tx - cx) / r, cy + (r - 20) * (ty - cy) / r];
    return `<line x1="${ix}" y1="${iy}" x2="${tx}" y2="${ty}"
            stroke="var(--bg)" stroke-width="2" />`;
  }).join("");

  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
            aria-label="종합 건강지수 ${bhi}점 ${grade}등급">
    ${bands}${ticks}${rawMark}${needle}
    <text x="${cx}" y="${cy - 34}" text-anchor="middle" fill="#fff"
          font-size="40" font-weight="800"
          style="font-variant-numeric:tabular-nums">${num(bhi, 1)}</text>
    <text x="${cx}" y="${cy - 14}" text-anchor="middle"
          fill="var(--text-dim)" font-size="11">BHI · 100점 만점</text>
    <text x="${cx - r - 4}" y="${cy + 18}" fill="var(--text-mute)" font-size="10">0</text>
    <text x="${cx + r - 8}" y="${cy + 18}" fill="var(--text-mute)" font-size="10">100</text>
  </svg>`;
}

/* ─── 6계통 레이더 ──────────────────────────────────────── */
function systemRadar(systems) {
  const S = 300, c = S / 2, R = 108;
  const n = systems.length;
  const pt = (i, v) => {
    const a = (-90 + (360 * i) / n) * (Math.PI / 180);
    const rr = (R * Math.max(0, Math.min(100, v))) / 100;
    return [c + rr * Math.cos(a), c + rr * Math.sin(a)];
  };

  // 동심 격자 + 등급 경계선(65 = C, 50 = D)
  const rings = [25, 50, 65, 80, 100].map((v) => {
    const pts = systems.map((_, i) => pt(i, v).join(",")).join(" ");
    const isBoundary = v === 65 || v === 50;
    return `<polygon points="${pts}" fill="none"
            stroke="${isBoundary ? "rgba(234,179,8,.35)" : "var(--line)"}"
            stroke-width="1" ${isBoundary ? 'stroke-dasharray="3 3"' : ""} />`;
  }).join("");

  const spokes = systems.map((_, i) => {
    const [x, y] = pt(i, 100);
    return `<line x1="${c}" y1="${c}" x2="${x}" y2="${y}" stroke="var(--line)" />`;
  }).join("");

  const cur = systems.map((s, i) => pt(i, s.score).join(",")).join(" ");
  const hasPrev = systems.some((s) => s.previous !== null && s.previous !== undefined);
  const prev = hasPrev
    ? systems.map((s, i) => pt(i, s.previous ?? s.score).join(",")).join(" ")
    : null;

  const prevPoly = prev
    ? `<polygon points="${prev}" fill="none" stroke="var(--text-mute)"
        stroke-width="1.5" stroke-dasharray="4 3" />`
    : "";

  const dots = systems.map((s, i) => {
    const [x, y] = pt(i, s.score);
    const color = s.capped_by_d5 ? "var(--crit)" : !s.performed ? "var(--text-mute)" : "#38bdf8";
    return `<circle cx="${x}" cy="${y}" r="4" fill="${color}" stroke="var(--panel)" stroke-width="1.5" />`;
  }).join("");

  const labels = systems.map((s, i) => {
    const [x, y] = pt(i, 128);
    const anchor = Math.abs(x - c) < 12 ? "middle" : x > c ? "start" : "end";
    return `<text x="${x}" y="${y}" text-anchor="${anchor}" font-size="10.5"
             fill="var(--text-dim)">${s.code}</text>
            <text x="${x}" y="${y + 13}" text-anchor="${anchor}" font-size="12"
             fill="#fff" font-weight="700"
             style="font-variant-numeric:tabular-nums">${num(s.score, 1)}</text>`;
  }).join("");

  return `<svg viewBox="-24 -14 ${S + 48} ${S + 28}" width="100%" style="max-height:320px">
    ${rings}${spokes}
    <polygon points="${cur}" fill="rgba(56,189,248,.20)" stroke="#38bdf8" stroke-width="2" />
    ${prevPoly}${dots}${labels}
  </svg>
  <div class="legend">
    <span><i style="background:#38bdf8"></i>금회</span>
    ${hasPrev ? '<span><i style="background:#667389"></i>전회</span>' : ""}
    <span><i style="background:#eab308"></i>등급 경계 (65=C · 50=D)</span>
  </div>`;
}

/* ─── 적신호 배너 ───────────────────────────────────────── */
function redFlagBanner(flags, bhi, bhiRaw) {
  if (!flags.length) {
    return `<div class="alert info" style="margin-bottom:14px">
      적신호 발동 없음 — 가중합 BHI가 그대로 종합등급이 됩니다.</div>`;
  }
  const items = flags.map((f) => `
    <div class="rf-item">
      <span class="badge crit">${esc(f.code)}</span>
      <div class="rf-body">
        <div class="rf-cond">${esc(f.condition)}</div>
        <div class="rf-meta">
          BHI 상한 <b class="num">${num(f.bhi_cap, 1)}</b> ·
          강제등급 <span class="grade g-${f.forced_grade}">${esc(f.forced_grade)}</span>
        </div>
        ${f.evidence?.length
          ? `<div class="rf-ev mono">${f.evidence.map(esc).join("<br/>")}</div>` : ""}
      </div>
    </div>`).join("");

  return `<div class="rf-banner">
    <div class="rf-head">
      적신호 ${flags.length}건 발동 — 가중합 <b class="num">${num(bhiRaw, 1)}</b>점이
      <b class="num">${num(bhi, 1)}</b>점으로 강제 하향되었습니다
      <span class="rf-note">§8.5 · 소견서 1면에 규칙번호와 근거를 반드시 병기</span>
    </div>
    <div class="rf-list">${items}</div>
  </div>`;
}

/* ─── CAPA 보드 ─────────────────────────────────────────── */
const CAPA_COLUMNS = [
  ["issued", "발행"], ["acknowledged", "접수"], ["planned", "계획"],
  ["executed", "이행"], ["verified", "검증"], ["closed", "종결"],
];

function capaBoard(rows) {
  const byState = Object.fromEntries(CAPA_COLUMNS.map(([k]) => [k, []]));
  rows.forEach((r) => (byState[r.capa_state] ?? byState.issued).push(r));

  return CAPA_COLUMNS.map(([key, label]) => {
    const items = byState[key] || [];
    const cards = items.map((r) => `
      <div class="capa-card ${r.escalation ? "esc" : ""}">
        <div class="capa-top">
          <span class="badge ${PRIORITY_TONE[r.priority]}">${r.priority} ${esc(r.priority_label)}</span>
          <span class="sev" style="color:${SEVERITY_COLOR[r.severity]}">${r.severity}</span>
        </div>
        <div class="capa-id mono">${esc(r.defect_id)}</div>
        <div class="capa-member">${esc(r.member_label)} · ${esc(r.system)}</div>
        <div class="capa-due">
          기한 <b class="num">${r.due_date ? esc(r.due_date) : "차기 검진"}</b>
          ${r.days_overdue > 0
            ? `<span class="badge crit">${int(r.days_overdue)}일 초과</span>` : ""}
        </div>
        ${r.escalation
          ? `<div class="capa-esc">${esc(r.escalation.level)} · ${esc(r.escalation.action)}</div>`
          : ""}
      </div>`).join("");

    return `<div class="capa-col">
      <div class="capa-col-head">${label}
        <span class="num">${int(items.length)}</span></div>
      ${cards || '<div class="capa-empty">—</div>'}
    </div>`;
  }).join("");
}

/* ─── 화면 조립 ─────────────────────────────────────────── */
async function loadBhc() {
  const d = await api(`/api/bhc/${App.buildingId}`);
  BHC.data = d;

  el("bhcStd").textContent =
    `${d.standard} · ${d.inspection.checkup_id} · ${d.inspection.level}`;

  el("bhcFlags").innerHTML = redFlagBanner(d.red_flags, d.bhi, d.bhi_raw);
  el("bhcGauge").innerHTML = bhiGauge(d.bhi, d.bhi_raw, d.grade, d.grade_label);

  const s = d.summary;
  el("bhcSummary").innerHTML = `
    <div class="bhi-grade">
      <span class="grade g-${d.grade} xl">${d.grade}</span>
      <div>
        <div class="bhi-label">${esc(d.grade_label)}</div>
        <div class="note">${esc(d.building.name)}</div>
      </div>
    </div>
    <div class="chips" style="margin-top:14px">
      ${chip("가중합 BHI", `<span class="num">${num(d.bhi_raw, 1)}</span>`, "점")}
      ${chip("적신호 적용 후", `<span class="num">${num(d.bhi, 1)}</span>`, "점",
             d.bhi < d.bhi_raw ? "critical" : "")}
      ${chip("법정 안전등급", gradeBadge(d.inspection.statutory_grade))}
      ${chip("검진 결함", `<span class="num">${int(d.prescriptions.length)}</span>`, "건")}
    </div>
    <div class="note" style="margin-top:12px">${esc(s.prescription_line)}</div>`;

  el("bhcRadar").innerHTML = systemRadar(d.systems);

  // 건강나이
  const h = d.health_age;
  const devTone = h.deviation === null ? "" :
    h.deviation >= 10 ? "crit" : h.deviation >= 5 ? "bad" :
    h.deviation <= -5 ? "ok" : "";
  el("bhcAge").innerHTML = `
    <div class="kpi-grid">
      ${kpi("건강나이", `<span class="num">${num(h.bha_years, 1)}</span>`, "년",
            `코호트 ${esc(h.cohort_label)} · β=${h.beta}`)}
      ${kpi("노화편차 Δ", h.deviation === null ? "—" :
            `<span class="num">${signed(h.deviation, 1)}</span>`, "년",
            `실제 경과 ${h.actual_years ?? "—"}년`, devTone)}
    </div>
    <div class="note" style="margin-top:10px">${esc(h.interpretation)}</div>
    ${h.advisory_only
      ? `<div class="alert" style="margin-top:10px">참고지표 — β가 잠정값이라
         등급 판정·처방 우선순위에 사용하지 않았습니다 (§8.7)</div>` : ""}`;

  // 열화속도
  const r = d.rate;
  const rateTone = r.baseline ? "" :
    r.value > 3 ? "crit" : r.value > h.beta ? "bad" : r.value > 0 ? "warn" : "ok";
  el("bhcRate").innerHTML = `
    <div class="kpi-grid">
      ${kpi("열화속도 v", r.baseline ? "기준선" :
            `<span class="num">${signed(r.value, 2)}</span>`,
            r.baseline ? "" : "점/년", esc(r.verdict), rateTone)}
    </div>
    <div class="note" style="margin-top:10px">${esc(r.action)}</div>
    ${r.saturated
      ? `<div class="alert" style="margin-top:10px">
          <b>포화 경고</b> — 두 회차가 같은 적신호 상한에 걸려 규범값이 0으로
          포화되었습니다. 상한 적용 전 보조지표는
          <b class="num">${signed(r.uncapped.value ?? 0, 2)}</b>점/년
          (${esc(r.uncapped.verdict)}) 입니다.
         </div>` : ""}`;

  // 심각도 분포 — 막대
  const counts = d.severity_counts;
  const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  el("bhcSeverity").innerHTML =
    Object.entries(counts).map(([k, v]) => `
      <div class="sev-row">
        <span class="sev-key" style="color:${SEVERITY_COLOR[k]}">${k}</span>
        <span class="sev-name">${SEVERITY_LABEL[k]}</span>
        <div class="bar"><i style="width:${((v / total) * 100).toFixed(1)}%;
          background:${SEVERITY_COLOR[k]}"></i></div>
        <span class="num sev-n">${int(v)}</span>
      </div>`).join("");

  // 계통 표
  renderTable(el("bhcSystems"), [
    { h: "계통", render: (x) => `${esc(x.code)} ${esc(x.label)}` },
    { h: "점수", cls: "num", render: (x) => num(x.score, 1) },
    { h: "전회", cls: "num", render: (x) => x.previous === null || x.previous === undefined
        ? "—" : num(x.previous, 1) },
    { h: "증감", cls: "num", render: (x) => x.previous === null || x.previous === undefined
        ? "—" : `<span style="color:${x.score >= x.previous ? "var(--ok)" : "var(--bad)"}">
                 ${signed(x.score - x.previous, 1)}</span>` },
    { h: "가중치", cls: "num", render: (x) => num(x.weight, 2) },
    { h: "결함", cls: "num", render: (x) => int(x.defect_count) },
    { h: "최고 심각도", render: (x) =>
        `<span style="color:${SEVERITY_COLOR[x.worst_severity]};font-weight:700">
         ${x.worst_severity}</span>` },
    { h: "소견", render: (x) => esc(x.comment) },
  ], d.systems);

  // 소견 3요소
  el("bhcSentences").innerHTML = d.sentences.length
    ? d.sentences.slice(0, 40).map((x) => `
        <div class="op-card">
          <div class="op-id mono">${esc(x.defect_id)}</div>
          <div class="op-row"><span class="op-tag obs">관측</span><span>${esc(x.observation)}</span></div>
          <div class="op-row"><span class="op-tag itp">해석</span><span>${esc(x.interpretation)}</span></div>
          <div class="op-row"><span class="op-tag rec">권고</span><span>${esc(x.recommendation)}</span></div>
        </div>`).join("")
    : '<div class="empty">결함이 없습니다</div>';

  // 주의사항
  el("bhcCaveats").innerHTML =
    [...s.caveats, ...(d.assumptions || [])]
      .map((c) => `<div class="alert">${esc(c)}</div>`).join("");

  // CAPA
  const capa = await api(`/api/bhc/${App.buildingId}/capa`);
  BHC.capa = capa;
  const m = capa.metrics;
  el("bhcCapaMetrics").textContent =
    `발행 ${m.issued} · 기한초과 ${m.overdue} · 에스컬레이션 ${m.escalated} · ` +
    `기한준수율 ${(m.on_time_rate * 100).toFixed(0)}%`;
  el("bhcCapa").innerHTML = capaBoard(capa.prescriptions);
  el("bhcCapaNote").textContent = capa.note;
}

/* 금지표현 검사 */
async function runLint() {
  const text = el("bhcLintText").value.trim();
  if (!text) return;
  const res = await api("/api/bhc/lint", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text }),
  });
  el("bhcLintOut").innerHTML = res.clean
    ? '<div class="alert info" style="margin-top:12px">금지표현이 발견되지 않았습니다.</div>'
    : res.findings.map((f) => `
        <div class="alert" style="margin-top:8px">
          <b>${esc(f.category)}</b> — “${esc(f.matched)}”<br/>
          <span class="note">${esc(f.reason)}. ${esc(f.suggestion)}</span>
        </div>`).join("");
}

document.addEventListener("DOMContentLoaded", () => {
  el("bhcLintRun")?.addEventListener("click", () => runLint().catch(console.error));
});
