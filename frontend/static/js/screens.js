/* 나머지 화면들 — 포트폴리오 · 알림 · 수동입력 · 검출성능 ·
 *                 점검회차 · 채널 · 결함통계 · CAPA
 *
 * 섹션을 app.html 에 미리 적지 않고 여기서 만들어 붙인다. 화면이 늘어날수록
 * HTML 파일만 비대해지고, 마크업과 그 마크업을 채우는 코드가 멀어져 고치기
 * 어려워지기 때문이다. 한 화면의 구조와 데이터가 한 함수 안에 있어야 한다.
 */

function scSection(key) {
  let node = document.getElementById("view-" + key);
  if (!node) {
    node = document.createElement("section");
    node.className = "view";
    node.id = "view-" + key;
    document.querySelector(".stage").appendChild(node);
  }
  return node;
}

function scCard(title, hint, body) {
  return `<div class="card">
    <h2>${title}${hint ? `<span class="hint">${hint}</span>` : ""}</h2>
    ${body}
  </div>`;
}

/* ─── 시설물 포트폴리오 ─────────────────────────────────── */
async function scPortfolio() {
  const node = scSection("portfolio");
  const bs = await api("/api/buildings");
  const year = new Date().getFullYear();

  const grades = ["A", "B", "C", "D", "E"];
  const dist = Object.fromEntries(grades.map((g) => [g, 0]));
  bs.forEach((b) => b.latest_grade && dist[b.latest_grade]++);
  const total = bs.length || 1;
  const HEX = { A: "#22c55e", B: "#84cc16", C: "#eab308", D: "#f97316", E: "#ef4444" };

  node.innerHTML = `
    <div class="row c4">
      ${kpi("관리 시설물", `<span class="num">${int(bs.length)}</span>`, "동")}
      ${kpi("보수 대상 등급 (D·E)", `<span class="num">${int(dist.D + dist.E)}</span>`, "동",
            "", dist.D + dist.E ? "bad" : "ok")}
      ${kpi("총 점검 회차",
            `<span class="num">${int(bs.reduce((a, b) => a + b.inspection_count, 0))}</span>`, "회")}
      ${kpi("총 결함",
            `<span class="num">${int(bs.reduce((a, b) => a + b.defect_count, 0))}</span>`, "건")}
    </div>
    <div class="row">
      ${scCard("안전등급 분포", "",
        `<div class="stack">
          ${grades.filter((g) => dist[g])
            .map((g) => `<i style="width:${(dist[g] / total * 100).toFixed(1)}%;background:${HEX[g]}"
                            title="${g} ${dist[g]}동">${dist[g]}</i>`).join("")
            || '<i style="width:100%;background:var(--elev);color:var(--text-mute)">판정 이력 없음</i>'}
         </div>
         <div class="stack-lg">${grades.map((g) =>
           `<span><i style="background:${HEX[g]}"></i>${g} <b class="num">${dist[g]}</b></span>`).join("")}</div>`)}
    </div>
    <div class="row">
      ${scCard("시설물 목록", "행을 클릭하면 해당 시설물로 전환합니다",
        '<div class="table-wrap"><table id="pfTable"></table></div>')}
    </div>`;

  renderTable(
    el("pfTable"),
    [
      { h: "시설물", render: (b) => esc(b.name) },
      { h: "종별", render: (b) => esc(b.facility_class) },
      { h: "구조", render: (b) => esc(b.structure_type) },
      { h: "준공", cls: "num", render: (b) => (b.completed_year ? b.completed_year : "—") },
      { h: "경과", cls: "num", render: (b) => (b.completed_year ? year - b.completed_year : "—") },
      { h: "등급", render: (b) => gradeBadge(b.latest_grade) },
      { h: "결함도", cls: "num", render: (b) => num(b.latest_index, 4) },
      { h: "점검", cls: "num", render: (b) => int(b.inspection_count) },
      { h: "결함", cls: "num", render: (b) => int(b.defect_count) },
    ],
    bs.slice().sort((a, b) => (b.latest_index || 0) - (a.latest_index || 0)),
    "시설물이 없습니다"
  );
  el("pfTable").querySelectorAll("tbody tr").forEach((tr, i) => {
    const sorted = bs.slice().sort((a, b) => (b.latest_index || 0) - (a.latest_index || 0));
    const b = sorted[i];
    if (!b) return;
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => {
      el("buildingSel").value = String(b.id);
      selectBuilding(b.id).then(() => (location.hash = "#overview"));
    });
  });
}

/* ─── 알림 센터 ─────────────────────────────────────────── */
async function scAlerts() {
  const node = scSection("alerts");
  const [tick, insps] = await Promise.all([
    api(`/api/live/${App.buildingId}/tick`).catch(() => null),
    api(`/api/inspections?building_id=${App.buildingId}`),
  ]);
  const latest = insps[0];
  const defects = latest
    ? (await api(`/api/inspections/${latest.id}/defects`)).filter((d) => d.repair_required)
    : [];

  const alerts = tick?.alerts || [];
  node.innerHTML = `
    <div class="row c3">
      ${kpi("계측 경보", `<span class="num">${int(alerts.length)}</span>`, "건", "",
            alerts.some((a) => a.status === "critical") ? "crit" : alerts.length ? "warn" : "ok")}
      ${kpi("보수 대상 결함", `<span class="num">${int(defects.length)}</span>`, "건", "",
            defects.length ? "bad" : "ok")}
      ${kpi("현재 건전성", `<span class="num">${tick ? num(tick.health_index, 1) : "—"}</span>`,
            "/100", tick ? `등급 ${tick.health_grade}` : "")}
    </div>
    <div class="row wide-left">
      ${scCard("계측 경보", "임계값을 넘긴 채널",
        `<div id="alChannels"></div>`)}
      ${scCard("보수 대상 결함", "허용균열폭 초과",
        `<div class="table-wrap table-scroll"><table id="alDefects"></table></div>`)}
    </div>`;

  el("alChannels").innerHTML = alerts.length
    ? alerts.map((a) =>
        `<div class="alert ${a.status === "critical" ? "critical" : ""}">
           <b>${esc(a.code)}</b> ${esc(a.kind_label)} —
           <span class="num">${num(a.value, 3)}</span> ${esc(a.unit)}
           (임계 <span class="num">${num(a.threshold, 2)}</span>) ${statusBadge(a.status)}
         </div>`).join("")
    : '<div class="empty">경보 없음</div>';

  renderTable(
    el("alDefects"),
    [
      { h: "유형", render: (d) => esc(d.defect_type) },
      { h: "부재", render: (d) => esc(d.member_code) },
      { h: "등급", render: (d) => gradeBadge(d.grade) },
      { h: "폭(mm)", cls: "num", render: (d) => num(d.width_mm, 3) },
      { h: "근거", render: (d) => esc(d.basis) },
    ],
    defects,
    "보수 대상 결함이 없습니다"
  );
}

/* ─── 결함 수동 입력 ────────────────────────────────────── */
async function scManual() {
  const node = scSection("manual");
  const inspId = App.inspections?.[0]?.id;
  const photos = inspId ? await api(`/api/photos?inspection_id=${inspId}`) : [];

  node.innerHTML = `
    <div class="row">
      ${scCard("사진을 골라 손상을 직접 그립니다",
        "AI가 놓친 결함을 기술자가 추가합니다",
        `<div class="alert info">
           직접 그린 손상은 <b>재분석해도 보존</b>됩니다. 사진을 클릭하면
           상세 화면이 <b>손상 그리기</b> 도구로 열립니다.
         </div>
         <div class="photo-grid" id="mnGrid" style="margin-top:12px"></div>`)}
    </div>`;

  el("mnGrid").innerHTML = photos.length
    ? photos.map((p) =>
        `<div class="photo-card" data-photo="${p.id}">
           <img class="thumb" loading="lazy" src="${esc(p.image_url || p.url)}" alt="" />
           <div class="pc-body">
             <div class="pc-top">
               <span class="dot ${p.analysis_state}"></span>
               <span class="pc-id">#${p.id}</span>
               <span class="pc-n">결함 ${int(p.defect_count ?? 0)}</span>
             </div>
             <div class="pc-note">${esc(p.member_code)}</div>
           </div>
         </div>`).join("")
    : '<div class="empty" style="grid-column:1/-1">등록된 사진이 없습니다. 검출 결과 브라우저에서 먼저 등록하십시오.</div>';

  el("mnGrid").querySelectorAll("[data-photo]").forEach((n) =>
    n.addEventListener("click", async () => {
      await phOpen(Number(n.dataset.photo));
      PH.tool = "draw";
      PH.pts = [];
      phRender();
    })
  );
}

/* ─── 검출 성능 ─────────────────────────────────────────── */
async function scBench() {
  const node = scSection("bench");
  const b = await api("/api/policy/benchmark");
  if (!b.available) {
    node.innerHTML = `<div class="row">${scCard("검출 성능", "",
      `<div class="alert">${esc(b.message)}</div>`)}</div>`;
    return;
  }
  const d = b.detection, w = b.width_mm, g = b.grade;
  const bands = b.recall_by_width || {};

  node.innerHTML = `
    <div class="row c4">
      ${kpi("F1", `<span class="num">${num(d.f1, 3)}</span>`, "",
            `정밀도 ${num(d.precision, 3)} · 재현율 ${num(d.recall, 3)}`)}
      ${kpi("균열폭 MAE", `<span class="num">${num(w.mae, 3)}</span>`, "mm",
            `중앙값 ${num(w.median_ae, 3)}mm`)}
      ${kpi("측정 편향", `<span class="num">${signed(w.bias, 3)}</span>`, "mm",
            "0에 가까울수록 계통오차 없음")}
      ${kpi("등급 일치율", `<span class="num">${num(g.accuracy, 3)}</span>`, "",
            `표본 ${int(b.samples)}장`)}
    </div>
    <div class="row wide-left">
      ${scCard("폭 구간별 재현율", "보수 대상(0.3mm 이상)을 놓치지 않는 것이 핵심",
        `<div class="table-wrap"><table id="bmBands"></table></div>`)}
      ${scCard("해석의 전제", "",
        `<div class="alert">${esc(b.caveat)}</div>
         <div class="note" style="margin-top:10px">
           검출 ${int(d.tp)}건 적중 · ${int(d.fp)}건 오검출 · ${int(d.fn)}건 누락<br/>
           이 중 ${int(d.fp_on_other_defect || 0)}건은 다른 유형의 결함 위에서 난
           오검출로, 유형 분류의 문제이지 유령 검출이 아닙니다.
         </div>`)}
    </div>`;

  renderTable(
    el("bmBands"),
    [
      { h: "폭 구간", render: (r) => esc(r.band) },
      { h: "정답", cls: "num", render: (r) => int(r.gt) },
      { h: "검출", cls: "num", render: (r) => int(r.hit) },
      {
        h: "재현율", cls: "num",
        render: (r) =>
          r.recall == null ? "—"
            : `<span style="color:${r.recall >= 0.6 ? "var(--ok)" : "var(--bad)"}">${num(r.recall, 3)}</span>`,
      },
    ],
    Object.entries(bands).map(([band, v]) => ({ band, ...v })),
    "구간 데이터가 없습니다"
  );
}

/* ─── 점검 회차 ─────────────────────────────────────────── */
async function scInspections() {
  const node = scSection("inspections");
  const rows = await api(`/api/inspections?building_id=${App.buildingId}`);

  node.innerHTML = `
    <div class="row">
      ${scCard("새 점검 회차", "",
        `<div class="field-row">
           <div class="field">
             <label for="inKind">점검 종류</label>
             <select id="inKind">
               <option value="regular">정기안전점검</option>
               <option value="precise">정밀안전점검</option>
               <option value="diagnosis">정밀안전진단</option>
               <option value="emergency">긴급안전점검</option>
             </select>
           </div>
           <div class="field">
             <label for="inInspector">점검자</label>
             <input id="inInspector" type="text" placeholder="이름" />
           </div>
           <button class="primary" id="inCreate">회차 만들기</button>
           <span class="note" id="inNote" style="margin-left:auto"></span>
         </div>`)}
    </div>
    <div class="row">
      ${scCard("회차 목록", `${rows.length}회`,
        '<div class="table-wrap"><table id="inTable"></table></div>')}
    </div>`;

  renderTable(
    el("inTable"),
    [
      { h: "점검일", cls: "nowrap", render: (i) => fmtDate(i.inspected_at) },
      { h: "종류", render: (i) => esc(i.kind_label) },
      { h: "등급", render: (i) => gradeBadge(i.safety_grade) },
      { h: "결함도", cls: "num", render: (i) => num(i.defect_index, 4) },
      { h: "결함", cls: "num", render: (i) => int(i.defect_count) },
      { h: "보수필요", cls: "num", render: (i) => int(i.repair_required_count) },
      { h: "점검자", render: (i) => esc(i.inspector) },
    ],
    rows,
    "점검 회차가 없습니다"
  );

  el("inCreate").addEventListener("click", async () => {
    try {
      await api("/api/inspections", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          building_id: App.buildingId,
          kind: el("inKind").value,
          inspector: el("inInspector").value.trim(),
        }),
      });
      App.inspections = await api(`/api/inspections?building_id=${App.buildingId}`);
      fillInspectionSelects();
      el("inNote").textContent = "회차를 만들었습니다.";
      await scInspections();
    } catch (e) {
      el("inNote").innerHTML = `<span style="color:var(--crit)">${esc(e.message)}</span>`;
    }
  });
}

/* ─── 채널 · 임계값 ─────────────────────────────────────── */
async function scChannels() {
  const node = scSection("channels");
  const chs = await api(`/api/live/${App.buildingId}/channels`);

  node.innerHTML = `
    <div class="row">
      ${scCard("계측 채널", `${chs.length}개 · 임계값은 경보/위험 2단계`,
        '<div class="table-wrap"><table id="chTable"></table></div>')}
    </div>
    <div class="row">
      ${scCard("임계값의 의미", "",
        `<div class="note">
           <b>경보</b>를 넘으면 주의 관찰 대상이 되고, <b>위험</b>을 넘으면 즉시 조치
           대상입니다. 건전성 지수(MTM)는 채널별 스트레스를 결합해 계산하되
           <b>최악 채널이 지배</b>하도록 가중합니다 — 평균을 쓰면 한 채널의 이상이
           다른 정상 채널에 희석되어 사라지기 때문입니다.
         </div>`)}
    </div>`;

  renderTable(
    el("chTable"),
    [
      { h: "채널", cls: "nowrap", render: (c) => esc(c.code) },
      { h: "종류", render: (c) => esc(c.kind_label) },
      { h: "부재", render: (c) => esc(c.member_code) },
      { h: "현재값", cls: "num", render: (c) => `${num(c.latest, 3)} ${esc(c.unit)}` },
      { h: "경보", cls: "num", render: (c) => num(c.warn_threshold, 2) },
      { h: "위험", cls: "num", render: (c) => num(c.critical_threshold, 2) },
      {
        h: "여유", cls: "num",
        render: (c) =>
          c.critical_threshold
            ? num(((c.critical_threshold - c.latest) / c.critical_threshold) * 100, 0) + "%"
            : "—",
      },
      { h: "상태", render: (c) => statusBadge(c.status) },
    ],
    chs,
    "계측 채널이 없습니다"
  );
}

/* ─── 결함 통계 ─────────────────────────────────────────── */
async function scStats() {
  const node = scSection("stats");
  const insps = await api(`/api/inspections?building_id=${App.buildingId}`);
  if (!insps.length) {
    node.innerHTML = `<div class="row">${scCard("결함 통계", "",
      '<div class="empty">점검 이력이 없습니다</div>')}</div>`;
    return;
  }
  const defects = await api(`/api/inspections/${insps[0].id}/defects`);

  const by = (key) =>
    defects.reduce((a, d) => ((a[d[key]] = (a[d[key]] || 0) + 1), a), {});
  const byType = by("defect_type");
  const byMember = by("member_code");
  const byGrade = by("grade");
  const HEX = { a: "#22c55e", b: "#84cc16", c: "#eab308", d: "#f97316", e: "#ef4444" };
  const total = defects.length || 1;

  const barList = (obj, color) =>
    Object.entries(obj)
      .sort((a, b) => b[1] - a[1])
      .map(
        ([k, v]) => `
        <div style="margin-bottom:8px">
          <div class="field-row" style="margin:0 0 3px">
            <span style="font-size:12px">${esc(k)}</span>
            <span class="num" style="margin-left:auto;font-size:12px">${v}</span>
          </div>
          <div class="bar"><i style="width:${(v / total * 100).toFixed(1)}%;background:${color}"></i></div>
        </div>`
      )
      .join("") || '<div class="empty">데이터 없음</div>';

  node.innerHTML = `
    <div class="row c3">
      ${scCard("결함 유형별", `최근 회차 ${defects.length}건`, barList(byType, "var(--accent)"))}
      ${scCard("부재별", "", barList(byMember, "#a78bfa"))}
      ${scCard("상태등급별", "",
        `<div class="stack">${["a","b","c","d","e"].filter((g) => byGrade[g])
          .map((g) => `<i style="width:${(byGrade[g] / total * 100).toFixed(1)}%;background:${HEX[g]}">${byGrade[g]}</i>`)
          .join("") || '<i style="width:100%;background:var(--elev)">0</i>'}</div>
         <div class="stack-lg">${["a","b","c","d","e"].map((g) =>
           `<span><i style="background:${HEX[g]}"></i>${g.toUpperCase()} <b class="num">${byGrade[g] || 0}</b></span>`).join("")}</div>`)}
    </div>
    <div class="row">
      ${scCard("회차별 추이", "결함 수와 보수 대상 건수",
        '<div class="table-wrap"><table id="stTrend"></table></div>')}
    </div>`;

  renderTable(
    el("stTrend"),
    [
      { h: "점검일", cls: "nowrap", render: (i) => fmtDate(i.inspected_at) },
      { h: "종류", render: (i) => esc(i.kind_label) },
      { h: "등급", render: (i) => gradeBadge(i.safety_grade) },
      { h: "결함", cls: "num", render: (i) => int(i.defect_count) },
      { h: "보수필요", cls: "num", render: (i) => int(i.repair_required_count) },
      { h: "결함도", cls: "num", render: (i) => num(i.defect_index, 4) },
    ],
    insps.slice().reverse()
  );
}

/* ─── CAPA ──────────────────────────────────────────────── */
/* #bhc 의 칸반이 '지금 어느 단계에 몇 건 있는가'를 보여준다면, 이 화면은
 * '무엇부터 손대야 하는가'를 한 줄로 세운다. 같은 데이터라도 착수 순서를
 * 정하는 일과 진행 상황을 훑는 일은 다른 화면이 필요하다. */
async function scCapa() {
  const node = scSection("capa");
  let capa;
  try {
    capa = await api(`/api/bhc/${App.buildingId}/capa`);
  } catch (e) {
    node.innerHTML = `<div class="row">${scCard("CAPA 조치", "",
      `<div class="alert bad">${esc(e.message)}</div>`)}</div>`;
    return;
  }

  const rows = capa.prescriptions || [];
  const m = capa.metrics || {};
  // 시급도 → 기한 → 지연일 순. 같은 P0 라도 기한이 이른 것이 먼저다.
  const sorted = rows.slice().sort(
    (a, b) =>
      (a.priority || "P9").localeCompare(b.priority || "P9") ||
      String(a.due_date || "").localeCompare(String(b.due_date || "")) ||
      (b.days_overdue || 0) - (a.days_overdue || 0)
  );
  const overdue = m.overdue || 0;

  node.innerHTML = `
    <div class="kpis">
      ${kpi("발행", `<span class="num">${int(m.issued)}</span>`, "건",
            "조치가 지시된 결함")}
      ${kpi("기한 초과", `<span class="num">${int(overdue)}</span>`, "건", "",
            overdue ? "crit" : "ok")}
      ${kpi("에스컬레이션", `<span class="num">${int(m.escalated)}</span>`, "건",
            "상위 보고 단계로 올라간 건", m.escalated ? "warn" : "ok")}
      ${kpi("종결률", `<span class="num">${num((m.closure_rate || 0) * 100, 1)}</span>`,
            "%", "검증까지 끝난 비율")}
      ${kpi("기한 준수율", `<span class="num">${num((m.on_time_rate || 0) * 100, 1)}</span>`,
            "%", "기한 안에 처리된 비율")}
    </div>
    <div class="row">
      ${scCard("시정·예방조치 (CAPA)", `${sorted.length}건 · 시급도 순`,
        `<div class="note" style="margin-bottom:10px">
           CAPA는 결함을 <b>고치는 일(시정)</b>과 <b>다시 생기지 않게 하는 일(예방)</b>을
           나눠 관리하는 체계입니다. 보수만 반복하면 원인이 남아 같은 결함이 재발합니다.
           단계별 진행 상황은 <a href="#bhc">건축물 건강검진</a>의 CAPA 보드에서 봅니다.
         </div>
         <div class="table-wrap"><table id="cpTable"></table></div>
         <div class="note" style="margin-top:8px">${esc(capa.note || "")}</div>`)}
    </div>`;

  renderTable(
    el("cpTable"),
    [
      {
        h: "시급도",
        cls: "nowrap",
        render: (r) =>
          `<span class="badge ${PRIORITY_TONE[r.priority] || "mute"}">${
            esc(r.priority)} ${esc(r.priority_label)}</span>`,
      },
      { h: "결함", cls: "mono nowrap", render: (r) => esc(r.defect_id) },
      {
        h: "부재",
        render: (r) => `${esc(r.member_label)} <span class="hint">${esc(r.system)}</span>`,
      },
      { h: "정도", cls: "nowrap", render: (r) => esc(r.severity) },
      { h: "조치", render: (r) => esc(r.action) },
      { h: "근거", cls: "nowrap", render: (r) => esc(r.basis) },
      {
        h: "기한",
        cls: "nowrap",
        render: (r) =>
          `${esc(r.due_date || "—")}` +
          (r.days_overdue > 0
            ? ` <span class="badge crit">${int(r.days_overdue)}일 초과</span>`
            : ""),
      },
      {
        h: "상태",
        cls: "nowrap",
        render: (r) =>
          (CAPA_STATE_LABEL[r.capa_state] || r.capa_state) +
          (r.escalation
            ? ` <span class="badge warn">${esc(r.escalation.level)}</span>`
            : ""),
      },
    ],
    sorted,
    "CAPA 항목이 없습니다"
  );
}

/* ─── 등록 ──────────────────────────────────────────────── */
const SCREENS = {
  portfolio: scPortfolio,
  alerts: scAlerts,
  manual: scManual,
  bench: scBench,
  inspections: scInspections,
  channels: scChannels,
  stats: scStats,
  capa: scCapa,
};
