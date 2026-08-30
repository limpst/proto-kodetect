/* 화면별 로직 — 데이터 적재, 렌더링, 실시간 연결 */

const State = {
  detail: null,
  progression: [],
  channels: [],
  lastTick: null,
  loaded: {},        // 화면별 1회 로딩 여부
};

/* ─── 시설물 선택 ───────────────────────────────────────── */
async function selectBuilding(id) {
  App.buildingId = id;
  State.loaded = {};
  State.detail = await api(`/api/buildings/${id}`);
  App.inspections = await api(`/api/inspections?building_id=${id}`);

  fillInspectionSelects();
  renderOverview();
  connectLive();
  if (window.onViewShown) window.onViewShown(App.view);
}

function fillInspectionSelects() {
  const opts = App.inspections
    .map(
      (i) =>
        `<option value="${i.id}">${fmtDate(i.inspected_at)} · ${esc(
          i.kind_label
        )} · ${(i.safety_grade || "-").toUpperCase()}</option>`
    )
    .join("");
  ["dtInsp", "rpInsp"].forEach((id) => {
    const s = el(id);
    if (s) s.innerHTML = opts;
  });
  const m = el("dtMember");
  if (m) {
    m.innerHTML = App.members
      .map((c) => `<option value="${c.code}">${esc(c.label)}</option>`)
      .join("");
  }
}

/* ─── 개요 ──────────────────────────────────────────────── */
function renderOverview() {
  const b = State.detail.building;
  const hist = State.detail.history;
  const latest = App.inspections[0];

  el("ovMeta").textContent =
    `${b.facility_class} · ${b.structure_type} · ` +
    `${b.completed_year ? b.completed_year + "년 준공" : "준공연도 미상"}`;

  const grade = latest?.safety_grade;
  const health = hist.length ? hist[hist.length - 1].health_index : null;
  const prev = hist.length > 1 ? hist[hist.length - 2].health_index : null;
  const drop = prev !== null && health !== null ? health - prev : null;
  const repairs = latest?.repair_required_count ?? 0;
  const age = b.completed_year ? new Date().getFullYear() - b.completed_year : null;

  const tone =
    grade === "A" || grade === "B" ? "ok"
    : grade === "C" ? "warn"
    : grade === "D" ? "bad" : "crit";

  el("ovKpis").innerHTML = [
    kpi("종합 안전등급", gradeBadge(grade, "xl"), "",
        latest ? fmtDate(latest.inspected_at) + " 기준" : ""),
    kpi("건전성 지수", `<span class="num">${num(health, 1)}</span>`, "/100",
        drop === null ? "" :
        `<span style="color:${drop < 0 ? "var(--bad)" : "var(--ok)"}">${signed(drop, 1)}</span> 전회차 대비`,
        tone),
    kpi("결함도 지수", `<span class="num">${num(latest?.defect_index, 4)}</span>`, "", "0에 가까울수록 양호"),
    kpi("검출 결함", `<span class="num">${int(latest?.defect_count ?? 0)}</span>`, "건",
        `보수 필요 <b class="num">${int(repairs)}</b>건`,
        repairs > 0 ? "warn" : ""),
    kpi("점검 회차", `<span class="num">${int(App.inspections.length)}</span>`, "회",
        hist.length ? `최초 ${fmtDate(hist[0].at)}` : ""),
    kpi("경과 연수", age === null ? "—" : `<span class="num">${int(age)}</span>`, "년",
        `연면적 ${b.gross_area_m2 ? int(Math.round(b.gross_area_m2)) : "—"} m²`),
  ].join("");

  // 건전성 추이 (점검 회차 축)
  const entry = makeChart("ovHistoryChart");
  if (entry && hist.length) {
    const data = hist.map((h) => ({
      time: Math.floor(new Date(h.at).getTime() / 1000),
      value: h.health_index,
    }));
    const s = addArea(entry, data);
    addThreshold(s, 60, "C등급 경계", "#eab308");
    addThreshold(s, 40, "D등급 경계", "#f97316");
    fitChart(entry);
  }

  el("ovVerdict").innerHTML = `
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:12px">
      ${gradeBadge(grade, "xl")}
      <div>
        <div style="font-size:15px;font-weight:650">${esc(b.name)}</div>
        <div class="note">${esc(b.address || "")}</div>
      </div>
    </div>
    <div class="note">${esc(State.detail.grade_description || "판정 이력이 없습니다")}</div>
    <div class="alert info" style="margin-top:14px">
      AI 분석 결과는 보조 참고자료입니다. 법적 효력이 있는 안전진단 결과로 쓰려면
      책임기술자의 현장 확인과 서명이 필요합니다.
    </div>`;

  renderTable(
    el("ovMembers"),
    [
      { h: "부재", render: (m) => esc(m.member_label) },
      { h: "상태등급", render: (m) => gradeBadge(m.grade) },
      { h: "결함도", cls: "num", render: (m) => num(m.defect_index, 4) },
      { h: "결함 수", cls: "num", render: (m) => int(m.defect_count) },
    ],
    latest?.members || [],
    "결함이 없습니다"
  );

  renderTable(
    el("ovInspections"),
    [
      { h: "점검일", cls: "nowrap", render: (i) => fmtDate(i.inspected_at) },
      { h: "종류", render: (i) => esc(i.kind_label) },
      { h: "등급", render: (i) => gradeBadge(i.safety_grade) },
      { h: "결함", cls: "num", render: (i) => int(i.defect_count) },
      { h: "보수필요", cls: "num", render: (i) => int(i.repair_required_count) },
      { h: "점검자", render: (i) => esc(i.inspector) },
    ],
    App.inspections
  );
}

/* 진단 화면(영상 분석)은 detect.js 가 전담한다. */

/* ─── 시계열 진행 ───────────────────────────────────────── */
async function loadProgression() {
  State.progression = await api(`/api/buildings/${App.buildingId}/progression`);
  renderProgressionList();
  if (State.progression.length) drawProgression(State.progression[0]);

  renderTable(
    el("prTable"),
    [
      { h: "균열", render: (p) => esc(p.label) },
      { h: "부재", render: (p) => esc(p.member_code) },
      { h: "현재폭(mm)", cls: "num", render: (p) => num(p.latest_width_mm, 3) },
      { h: "허용폭(mm)", cls: "num", render: (p) => num(p.allowable_mm, 2) },
      {
        h: "진행률(mm/년)",
        cls: "num",
        render: (p) => num(p.rate_mm_per_year, 4),
      },
      { h: "모델", render: (p) => `<span class="badge mute">${esc(p.model)}</span>` },
      { h: "R²", cls: "num", render: (p) => num(p.r_squared, 3) },
      {
        h: "허용폭까지",
        cls: "num",
        render: (p) =>
          p.years_to_allowable === null
            ? "—"
            : p.years_to_allowable === 0
            ? '<span class="badge crit">초과</span>'
            : `${num(p.years_to_allowable, 1)}년`,
      },
      { h: "판정", render: (p) => esc(p.verdict) },
    ],
    State.progression,
    "추적 중인 균열이 없습니다"
  );
}

function renderProgressionList() {
  const node = el("prList");
  if (!State.progression.length) {
    node.innerHTML = '<div class="empty">추적 균열이 없습니다</div>';
    return;
  }
  node.innerHTML = State.progression
    .map((p, i) => {
      const urgent =
        p.years_to_allowable !== null && p.years_to_allowable <= 3 ? "warn" : "";
      const ratio = p.allowable_mm
        ? Math.min(1, (p.latest_width_mm || 0) / p.allowable_mm)
        : 0;
      const color =
        ratio >= 1 ? "var(--crit)" : ratio >= 0.8 ? "var(--bad)" : "var(--accent)";
      return `<div class="chip ${urgent}" style="width:100%;cursor:pointer;margin-bottom:8px"
                   data-track="${i}">
        <div class="c-l">${esc(p.label)} · ${esc(p.member_code)}</div>
        <div class="c-v num">${num(p.latest_width_mm, 3)}<small>mm</small></div>
        <div class="bar" style="margin-top:6px"><i style="width:${(ratio * 100).toFixed(
          0
        )}%;background:${color}"></i></div>
      </div>`;
    })
    .join("");
  node.querySelectorAll("[data-track]").forEach((n) =>
    n.addEventListener("click", () =>
      drawProgression(State.progression[Number(n.dataset.track)])
    )
  );
}

function drawProgression(p) {
  const entry = makeChart("prChart");
  if (!entry || !p) return;

  const measured = p.points.map((pt) => ({
    time: Math.floor(new Date(pt.at).getTime() / 1000),
    value: pt.width_mm,
  }));
  const s = addLine(entry, measured, { color: "#38bdf8", lineWidth: 2 });

  if (p.forecast?.length) {
    const last = measured[measured.length - 1];
    const fc = [last].concat(
      p.forecast.map(([d, v]) => ({
        time: Math.floor(new Date(d).getTime() / 1000),
        value: v,
      }))
    );
    addLine(entry, fc, { color: "#a78bfa", lineWidth: 2, lineStyle: 2 });
  }
  addThreshold(s, p.allowable_mm, "허용균열폭", "#ef4444");
  fitChart(entry);
}

/* ─── 실시간 계측 ───────────────────────────────────────── */
async function loadLive() {
  State.channels = await api(`/api/live/${App.buildingId}/channels`);
  App.channels = State.channels;

  const sel = el("lvChSel");
  sel.innerHTML = State.channels
    .map((c) => `<option value="${c.code}">${esc(c.code)} · ${esc(c.kind_label)}</option>`)
    .join("");
  sel.onchange = () => loadChannelChart(sel.value);
  if (State.channels.length) loadChannelChart(State.channels[0].code);

  const oh = await api(`/api/live/${App.buildingId}/health/ohlc`);
  const entry = makeChart("lvHealthChart");
  if (entry) {
    addCandles(entry, oh.candles);
    fitChart(entry);
  }

  renderTable(
    el("lvTable"),
    [
      { h: "채널", cls: "nowrap", render: (c) => esc(c.code) },
      { h: "종류", render: (c) => esc(c.kind_label) },
      { h: "부재", render: (c) => esc(c.member_code) },
      { h: "현재값", cls: "num", render: (c) => `${num(c.latest, 3)} ${esc(c.unit)}` },
      { h: "경보", cls: "num", render: (c) => num(c.warn_threshold, 2) },
      { h: "위험", cls: "num", render: (c) => num(c.critical_threshold, 2) },
      { h: "상태", render: (c) => statusBadge(c.status) },
    ],
    State.channels
  );
}

async function loadChannelChart(code) {
  const d = await api(
    `/api/live/${App.buildingId}/history?code=${encodeURIComponent(code)}`
  );
  el("lvChTitle").textContent = `${d.code} · ${d.kind_label} (${d.unit})`;
  const entry = makeChart("lvChannelChart");
  if (!entry) return;
  const s = addArea(entry, d.series, {
    lineColor: "#7dd3fc",
    topColor: "rgba(125,211,252,0.22)",
    bottomColor: "rgba(125,211,252,0.01)",
  });
  addThreshold(s, d.warn, "경보", "#eab308");
  addThreshold(s, d.critical, "위험", "#ef4444");
  fitChart(entry);
}

/* ─── 실시간 연결 ───────────────────────────────────────── */
function connectLive() {
  if (App.ws) {
    try { App.ws.close(); } catch (_) {}
    App.ws = null;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/live/ws/${App.buildingId}`);
  App.ws = ws;

  ws.onopen = () => setLiveDot(true, "실시간 연결됨");
  ws.onclose = () => {
    setLiveDot(false, "연결 끊김 — 재시도");
    // 시설물을 바꾼 경우가 아니면 잠시 후 재연결한다
    setTimeout(() => { if (App.ws === ws) connectLive(); }, 3000);
  };
  ws.onerror = () => setLiveDot(false, "연결 오류");
  ws.onmessage = (ev) => {
    const t = JSON.parse(ev.data);
    if (t.error) return;
    State.lastTick = t;
    applyTick(t);
  };
}

function setLiveDot(on, text) {
  const d = el("liveDot");
  d.className = "live-dot" + (on ? " on" : "");
  d.querySelector("span").textContent = text;
}

function applyTick(t) {
  // 상단 티커
  el("tickHealth").innerHTML =
    `${num(t.health_index, 1)}<small style="color:${
      t.health_delta < 0 ? "var(--bad)" : "var(--ok)"
    }">${t.health_delta ? signed(t.health_delta, 1) : ""}</small>`;
  const g = el("tickGrade");
  g.className = `grade g-${t.health_grade}`;
  g.textContent = t.health_grade;

  // 실시간 칩
  if (State.channels.length) {
    el("lvChips").innerHTML = State.channels
      .map((c) =>
        chip(
          `${c.code} · ${c.kind_label}`,
          `<span class="num">${num(t.values[c.code], 3)}</span>`,
          c.unit,
          t.statuses?.[c.code] === "critical"
            ? "critical"
            : t.statuses?.[c.code] === "warn"
            ? "warn"
            : ""
        )
      )
      .join("");
  }

  // 경보
  const alerts = t.alerts || [];
  el("lvAlerts").innerHTML = alerts.length
    ? alerts
        .map(
          (a) =>
            `<div class="alert ${a.status === "critical" ? "critical" : ""}">
               <b>${esc(a.code)}</b> ${esc(a.kind_label)} —
               <span class="num">${num(a.value, 3)}</span> ${esc(a.unit)}
               (임계 <span class="num">${num(a.threshold, 2)}</span>)
               ${statusBadge(a.status)}
             </div>`
        )
        .join("")
    : '<div class="empty">경보 없음</div>';

  // 기여도
  const c = t.contributors || {};
  el("lvContrib").innerHTML = [
    chip("점검 기반", `<span class="num">${num(c.inspection, 4)}</span>`),
    chip("계측 기반", `<span class="num">${num(c.sensors, 4)}</span>`),
  ].join("");

  if (App.view === "view3d") v3dUpdate(t.statuses, t.contributors);
}

/* ─── 3D ────────────────────────────────────────────────── */
async function load3D() {
  if (!State.channels.length) {
    State.channels = await api(`/api/live/${App.buildingId}/channels`);
  }
  const latest = App.inspections[0];
  v3dBuild(State.detail.building, latest?.members || [], State.channels);
  renderTable(
    el("v3dTable"),
    [
      { h: "채널", cls: "nowrap", render: (c) => esc(c.code) },
      { h: "종류", render: (c) => esc(c.kind_label) },
      { h: "값", cls: "num", render: (c) => `${num(c.latest, 3)} ${esc(c.unit)}` },
      { h: "상태", render: (c) => statusBadge(c.status) },
    ],
    State.channels
  );
}

/* ─── 유지관리 정책 ─────────────────────────────────────── */
async function loadPolicy() {
  const rec = await api(`/api/policy/recommend/${App.buildingId}`);
  el("plSource").textContent = `${rec.source} · 예산기조: ${rec.manager_action}`;
  renderTable(
    el("plActions"),
    [
      { h: "부재", render: (r) => esc(r.member_label) },
      { h: "추정 상태등급", render: (r) => gradeBadge(r.belief_grade) },
      {
        h: "권장 조치",
        render: (r) => `<span class="badge info">${esc(r.action)}</span>`,
      },
      { h: "기대가치", cls: "num", render: (r) => num(r.expected_value, 3) },
      { h: "CVaR", cls: "num", render: (r) => num(r.cvar, 3) },
      { h: "근거", render: (r) => esc(r.rationale) },
    ],
    rec.actions,
    "권장 조치가 없습니다"
  );

  const rep = await api("/api/policy/report");
  if (!rep.available) {
    el("plCompare").innerHTML =
      `<tbody><tr><td class="empty">${esc(rep.message)}</td></tr></tbody>`;
    el("plNote").textContent = "";
    return;
  }

  const rows = Object.entries(rep.policies).map(([name, v]) => ({ name, ...v }));
  const best = rows.reduce((a, b) => (b.return > a.return ? b : a));
  renderTable(
    el("plCompare"),
    [
      {
        h: "정책",
        render: (r) =>
          esc(r.name) + (r.name === best.name ? ' <span class="badge ok">최적</span>' : ""),
      },
      { h: "누적 보상", cls: "num", render: (r) => num(r.return, 1) },
      { h: "총 지출", cls: "num", render: (r) => num(r.cost, 1) },
      { h: "위험비용", cls: "num", render: (r) => num(r.risk, 1) },
      { h: "최악 등급", cls: "num", render: (r) => num(r.worst_grade, 2) },
      { h: "위험 노출 연수", cls: "num", render: (r) => num(r.failure_years, 2) },
    ],
    rows
  );

  const rl = rep.policies["강화학습 (HRL + CVaR)"];
  const pd = rep.policies["정기보수 (주기 기반)"];
  if (rl && pd) {
    const dRisk = pd.risk - rl.risk;
    const dCost = pd.cost - rl.cost;
    el("plNote").innerHTML =
      `정기보수 대비 위험비용 <b class="num">${signed(dRisk, 1)}</b>, ` +
      `지출 <b class="num">${signed(dCost, 1)}</b> (백만원, 30년 누적). ` +
      `CVaR 기준으로 행동을 고르므로 평균이 아니라 최악 시나리오를 줄이는 방향으로 학습됩니다.`;
  }

  const entry = makeChart("plCurve");
  if (entry && rep.training_curve?.length) {
    // 에피소드 축을 시간축에 대응시킨다 (차트 라이브러리 요구사항)
    const t0 = 1700000000;
    const data = rep.training_curve.map((v, i) => ({ time: t0 + i * 86400, value: v }));
    addLine(entry, data, { color: "#a78bfa" });
    fitChart(entry);
  }
}

/* ─── 판정서 ────────────────────────────────────────────── */
function openReport() {
  const id = el("rpInsp").value;
  el("rpFrame").src = `/api/reports/inspection/${id}`;
}

/* ─── 화면 전환 시 지연 로딩 ────────────────────────────── */
window.onViewShown = function (view) {
  if (!App.buildingId) return;
  const once = (key, fn) => {
    if (State.loaded[key]) return;
    State.loaded[key] = true;
    fn().catch((e) => console.error(key, e));
  };
  if (view === "bhc") once("bhc", loadBhc);
  if (view === "groups") once("groups", loadGroups);
  if (view === "drawings") once("drawings", loadDrawings);
  if (view === "deliver") once("deliver", loadDeliver);
  if (view === "progression") once("progression", loadProgression);
  if (view === "live") once("live", loadLive);
  if (view === "view3d") once("view3d", load3D);
  if (view === "policy") once("policy", loadPolicy);
  if (view === "report" && !el("rpFrame").src) openReport();
};

document.addEventListener("DOMContentLoaded", () => {
  el("rpOpen")?.addEventListener("click", openReport);
});
