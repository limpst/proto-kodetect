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
/** 서버 오류 본문을 사람이 읽을 한 문장으로 만든다.
 *
 * FastAPI 는 422(검증 실패) 에서 detail 을 **객체 배열**로 돌려준다.
 * 이걸 그대로 Error 에 넣으면 화면에 "[object Object]" 가 뜨고, 사용자는
 * 물론 개발자도 원인을 알 수 없다. 필드 위치와 사유를 풀어 쓴다.
 */
function errorText(body, status) {
  const d = body && body.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d) && d.length) {
    return d
      .map((e) => {
        const at = (e.loc || []).filter((x) => x !== "body").join(".");
        return at ? `${at}: ${e.msg}` : e.msg;
      })
      .join(" / ");
  }
  if (d && typeof d === "object") return JSON.stringify(d);
  return `HTTP ${status}`;
}

async function api(path, opts = {}) {
  // 본문이 문자열(JSON)인데 헤더가 없으면 fetch 는 text/plain 을 붙인다.
  // FastAPI 는 그걸 JSON 본문으로 읽지 않아 422 가 난다 — 여기서 채워 준다.
  const init = { credentials: "same-origin", ...opts };
  if (typeof init.body === "string") {
    const h = new Headers(init.headers || {});
    if (!h.has("content-type")) h.set("content-type", "application/json");
    init.headers = h;
  }
  const r = await fetch(path, init);
  if (r.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("unauthenticated");
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(errorText(body, r.status));
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
/* ─── 라우팅 — 실제 구조는 nav.js 가 갖는다 ────────────── */
window.addEventListener("hashchange", () =>
  showItem(location.hash.replace("#", "") || "overview")
);

/** 저장이 휘발성이면 알린다.
 *
 * 무료 배포는 DATABASE_URL 없이 뜨면 SQLite 파일을 컨테이너에 만들고, 다음
 * 배포에서 통째로 사라진다. 사진·그룹·도면·수기 손상이 함께 날아가는데
 * 화면에는 "데이터가 없습니다"로만 보여 원인을 짚을 수 없다.
 */
async function checkPersistence() {
  const node = el("persistWarn");
  if (!node) return;
  try {
    const h = await api("/healthz");
    const lost = [];
    if (h.database_persistent === false) lost.push("점검 기록");
    if (h.storage_persistent === false) lost.push("업로드 사진");
    if (!lost.length) return;
    node.textContent = "임시 저장";
    node.title =
      `${lost.join(" · ")}이(가) 재배포·재시작 때 초기화됩니다. ` +
      `DB=${h.database}, 저장소=${h.storage_dir}. ` +
      "보존하려면 DATABASE_URL(관리형 Postgres)과 영구 디스크를 지정하십시오.";
    node.hidden = false;
  } catch {
    // 상태를 못 읽는 것 자체로 화면을 막지는 않는다.
  }
}

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

  await checkPersistence();

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
  showItem(location.hash.replace("#", "") || "overview");
}

document.addEventListener("DOMContentLoaded", () => {
  boot().catch((e) => console.error("boot failed", e));
});
