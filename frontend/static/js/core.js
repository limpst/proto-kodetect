/* 공통 유틸 · 앱 상태 · 라우팅 */

const App = {
  buildingId: null,
  buildings: [],
  inspections: [],
  members: [],
  channels: [],
  ws: null,
  view: "overview",
};

/* ─── 요청 ──────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("unauthenticated");
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

/* ─── 표시 형식 ─────────────────────────────────────────── */
const el = (id) => document.getElementById(id);

/** 수치 포맷 — 자릿수를 고정해 표가 흔들리지 않게 한다. */
function num(v, digits = 2, dash = "—") {
  if (v === null || v === undefined || Number.isNaN(v)) return dash;
  return Number(v).toLocaleString("ko-KR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function int(v, dash = "—") {
  if (v === null || v === undefined) return dash;
  return Number(v).toLocaleString("ko-KR");
}

function signed(v, digits = 2) {
  if (v === null || v === undefined) return "";
  const s = Number(v);
  return (s >= 0 ? "+" : "") + num(s, digits);
}

function gradeBadge(g, extra = "") {
  if (!g) return `<span class="grade g-none ${extra}">—</span>`;
  return `<span class="grade g-${g} ${extra}">${String(g).toUpperCase()}</span>`;
}

function statusBadge(s) {
  const map = {
    normal: ["ok", "정상"],
    warn: ["warn", "주의"],
    critical: ["crit", "위험"],
  };
  const [cls, label] = map[s] || ["mute", s];
  return `<span class="badge ${cls}">${label}</span>`;
}

function fmtDate(iso) {
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate()
  ).padStart(2, "0")}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/** 표를 한 번에 그린다. cols: [{h, k, cls, render}] */
function renderTable(node, cols, rows, emptyText = "데이터가 없습니다") {
  if (!rows || !rows.length) {
    node.innerHTML = `<tbody><tr><td class="empty">${emptyText}</td></tr></tbody>`;
    return;
  }
  const head = cols.map((c) => `<th>${c.h}</th>`).join("");
  const body = rows
    .map(
      (r) =>
        "<tr>" +
        cols
          .map((c) => {
            const v = c.render ? c.render(r) : esc(r[c.k]);
            return `<td class="${c.cls || ""}">${v}</td>`;
          })
          .join("") +
        "</tr>"
    )
    .join("");
  node.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
}

/** KPI 타일 — 값이 잘리지 않도록 라벨만 말줄임한다. */
function kpi(label, value, unit = "", detail = "", tone = "") {
  return `<div class="kpi ${tone}">
    <div class="kpi-l" title="${esc(label)}">${esc(label)}</div>
    <div class="kpi-v">${value}${unit ? `<small>${esc(unit)}</small>` : ""}</div>
    ${detail ? `<div class="kpi-d">${detail}</div>` : ""}
  </div>`;
}

function chip(label, value, unit = "", tone = "") {
  return `<div class="chip ${tone}">
    <div class="c-l" title="${esc(label)}">${esc(label)}</div>
    <div class="c-v num">${value}${unit ? `<small>${esc(unit)}</small>` : ""}</div>
  </div>`;
}

/* ─── 라우팅 ────────────────────────────────────────────── */
const VIEW_TITLES = {
  overview: "개요",
  bhc: "건강검진 (BHC-STD-2026)",
  detect: "균열 분석",
  progression: "시계열 진행",
  live: "실시간 계측",
  view3d: "3D 뷰",
  policy: "유지관리 정책",
  report: "판정서",
};

function sectionOf(view) {
  return view === "view3d" ? el("view3d-section") : el("view-" + view);
}

function showView(view) {
  if (!VIEW_TITLES[view]) view = "overview";
  App.view = view;
  document.querySelectorAll(".view").forEach((s) => s.classList.remove("active"));
  sectionOf(view)?.classList.add("active");
  document
    .querySelectorAll("#nav a")
    .forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  el("viewTitle").textContent = VIEW_TITLES[view];
  if (window.onViewShown) window.onViewShown(view);
}

window.addEventListener("hashchange", () =>
  showView(location.hash.replace("#", "") || "overview")
);

/* ─── 부트 ──────────────────────────────────────────────── */
async function boot() {
  const me = await api("/api/auth/me");
  el("userLine").textContent = me.user
    ? `${me.user} 님`
    : me.auth_enabled
    ? "미인증"
    : "인증 비활성";

  el("logout").addEventListener("click", async (e) => {
    e.preventDefault();
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    location.href = "/login";
  });

  App.buildings = await api("/api/buildings");
  App.members = await api("/api/member-classes");

  const sel = el("buildingSel");
  sel.innerHTML = App.buildings
    .map((b) => `<option value="${b.id}">${esc(b.name)}</option>`)
    .join("");
  sel.addEventListener("change", () => selectBuilding(Number(sel.value)));

  if (App.buildings.length) {
    await selectBuilding(App.buildings[0].id);
  }
  showView(location.hash.replace("#", "") || "overview");
}

document.addEventListener("DOMContentLoaded", () => {
  boot().catch((e) => console.error("boot failed", e));
});
