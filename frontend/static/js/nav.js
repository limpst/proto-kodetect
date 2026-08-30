/* 2단 내비게이션 — 상단(대분류) · 좌측(기능)
 *
 * 기능이 수십 개가 되면 목록을 HTML에 손으로 적는 순간 관리가 무너진다.
 * 여기 한 곳에서 구조를 정의하고 화면은 이걸 그린다.
 *
 * view 가 null 인 항목은 아직 화면이 없는 기능이다. 감추지 않고 '설계' 태그로
 * 남겨 둔다 — 무엇이 있고 무엇이 아직 없는지가 보이는 편이 정직하다.
 *
 * F1~F9 는 노션 "건축물 안전진단 AI 솔루션 — 도메인 & 기능 총정리" 의
 * 핵심 기능 번호다. 화면과 명세를 같은 이름으로 부르기 위해 붙였다.
 */

const NAV = [
  {
    key: "dash",
    label: "대시보드",
    icon: "▤",
    items: [
      { key: "overview", label: "종합 현황", icon: "◧", view: "overview",
        desc: "선택한 시설물의 안전등급·건전성 추이·부재별 상태를 한 화면에서 봅니다." },
      { key: "portfolio", label: "시설물 포트폴리오", icon: "▦", view: "portfolio",
        desc: "관리 중인 전체 시설물을 등급·경과연수·결함 수로 비교합니다." },
      { key: "alerts", label: "알림 센터", icon: "◬", view: "alerts",
        desc: "임계값을 넘긴 계측 채널과 보수 대상 결함을 모아 봅니다." },
    ],
  },
  {
    key: "diag",
    label: "진단",
    icon: "◈",
    items: [
      { key: "detect", label: "영상 분석", icon: "◈", view: "detect", tag: "F1·F2",
        desc: "사진에서 균열을 검출하고 폭을 mm로 정량화해 상태등급을 판정합니다." },
      { key: "photos", label: "검출 결과 브라우저", icon: "▩", view: "photos",
        desc: "분석한 사진을 상태별로 훑어보고 재분석 대상을 고릅니다." },
      { key: "manual", label: "결함 수동 입력", icon: "✎", view: "manual",
        desc: "자동 검출이 놓친 결함을 기술자가 직접 기록합니다." },
      { key: "bench", label: "검출 성능", icon: "◎", view: "bench",
        desc: "합성 정답과 대조한 검출기 벤치마크입니다." },
      { key: "calib", label: "스케일 보정", icon: "⌗", view: null,
        desc: "균열 게이지 대조 촬영으로 촬영계 PSF를 현장 보정합니다." },
    ],
  },
  {
    key: "field",
    label: "현장",
    icon: "◫",
    items: [
      { key: "groups", label: "사진 그룹", icon: "▩", view: "groups",
        desc: "부재·위치 단위로 사진을 묶습니다. 보고서와 도면 배치의 기본 단위입니다." },
      { key: "drawings", label: "도면 · 위치", icon: "◫", view: "drawings",
        desc: "외관조사망도에 점검 위치 핀을 배치합니다. 도면 없이 시작할 수 있습니다." },
      { key: "inspections", label: "점검 회차", icon: "◷", view: "inspections",
        desc: "정기점검·정밀점검·정밀안전진단 회차를 관리합니다." },
    ],
  },
  {
    key: "mon",
    label: "모니터링",
    icon: "◉",
    items: [
      { key: "live", label: "실시간 계측", icon: "◉", view: "live", tag: "F9",
        desc: "IoT 채널 값과 건전성 지수를 1초 주기로 갱신합니다." },
      { key: "view3d", label: "3D 디지털 트윈", icon: "⬢", view: "view3d", tag: "F9",
        desc: "구조물 위에 센서와 상태등급을 매핑해 봅니다." },
      { key: "channels", label: "채널 · 임계값", icon: "⚟", view: "channels",
        desc: "계측 채널의 경보·위험 임계값을 확인합니다." },
    ],
  },
  {
    key: "ana",
    label: "분석",
    icon: "◔",
    items: [
      { key: "progression", label: "균열 진행", icon: "◔", view: "progression",
        desc: "회차별 균열폭 이력으로 진행 속도와 허용폭 도달 시점을 추정합니다." },
      { key: "bhc", label: "건축물 건강검진", icon: "🩺", view: "bhc",
        desc: "BHC-STD-2026 기준으로 계통별 건강도를 판정합니다." },
      { key: "stats", label: "결함 통계", icon: "▥", view: "stats",
        desc: "부재·유형·등급별 결함 분포와 회차 간 변화를 봅니다." },
    ],
  },
  {
    key: "dec",
    label: "의사결정",
    icon: "◆",
    items: [
      { key: "policy", label: "유지관리 정책", icon: "◆", view: "policy",
        desc: "강화학습 정책이 예산 제약 아래 부재별 조치를 추천합니다." },
      { key: "capa", label: "CAPA 조치", icon: "✔", view: "capa",
        desc: "시정·예방조치 항목을 우선순위와 함께 관리합니다." },
      { key: "budget", label: "예산 시뮬레이션", icon: "₩", view: null,
        desc: "예산 수준을 바꿔가며 30년 위험비용 변화를 봅니다." },
    ],
  },
  {
    key: "out",
    label: "산출물",
    icon: "▦",
    items: [
      { key: "report", label: "판정서", icon: "▦", view: "report", tag: "F3",
        desc: "시설물 개요·종합판정·결함목록·적용기준을 갖춘 판정서입니다." },
      { key: "deliver", label: "보고서 빌더", icon: "⬇", view: "deliver",
        desc: "사진 그룹·도면 범위를 골라 산출물 묶음을 만듭니다." },
      { key: "submit", label: "법정 제출", icon: "↗", view: null, tag: "F6",
        desc: "KALIS-FMS·세움터 자동 제출 (설계 단계)." },
    ],
  },
];

/* 조회용 색인 — 화면 키에서 그룹·항목을 되찾는다 */
const NAV_INDEX = {};
NAV.forEach((g) =>
  g.items.forEach((it) => {
    NAV_INDEX[it.key] = { group: g, item: it };
  })
);

function navSectionOf(viewKey) {
  if (viewKey === "view3d") return document.getElementById("view3d-section");
  return document.getElementById("view-" + viewKey);
}

function renderGroupNav(activeGroup) {
  const node = document.getElementById("groupNav");
  if (!node) return;
  node.innerHTML = NAV.map(
    (g) =>
      `<a href="#${g.items[0].key}" data-group="${g.key}"
          class="${g.key === activeGroup ? "active" : ""}">
         <span class="gi">${g.icon}</span>${g.label}
       </a>`
  ).join("");
}

function renderSubNav(group, activeItem) {
  const node = document.getElementById("subNav");
  if (!node || !group) return;
  node.innerHTML =
    `<div class="sec">${group.label}</div>` +
    group.items
      .map((it) => {
        const tag = it.tag
          ? `<span class="tag f">${it.tag}</span>`
          : it.view
          ? ""
          : `<span class="tag">설계</span>`;
        return `<a href="#${it.key}" data-item="${it.key}"
                   class="${it.key === activeItem ? "active" : ""}">
                  <span class="si">${it.icon}</span>${it.label}${tag}
                </a>`;
      })
      .join("");
}

/** 아직 화면이 없는 기능에 보여줄 안내. 빈 화면보다 정직하다. */
function renderPlaceholder(item) {
  const stage = document.querySelector(".stage");
  let node = document.getElementById("view-placeholder");
  if (!node) {
    node = document.createElement("section");
    node.className = "view";
    node.id = "view-placeholder";
    stage.appendChild(node);
  }
  node.innerHTML = `
    <div class="row"><div class="card">
      <h2>설계 단계</h2>
      <div class="alert info">
        <b>${esc(item.label)}</b> — ${esc(item.desc || "")}
      </div>
      <div class="note" style="margin-top:10px">
        이 기능은 명세가 정리되어 있고 화면은 아직 없습니다. 구현되지 않은 것을
        구현된 것처럼 보이게 두지 않으려고 이 안내를 남깁니다.
      </div>
    </div></div>`;
  return node;
}

function showItem(key) {
  const entry = NAV_INDEX[key] || NAV_INDEX.overview;
  const { group, item } = entry;

  document.querySelectorAll(".view").forEach((s) => s.classList.remove("active"));

  // view 키가 있어도 해당 섹션이 아직 없을 수 있다. 그때 빈 화면을 보여주면
  // 사용자는 고장으로 받아들인다 — 안내로 대체한다.
  const section = (item.view && navSectionOf(item.view)) || renderPlaceholder(item);
  section.classList.add("active");

  renderGroupNav(group.key);
  renderSubNav(group, item.key);

  el("viewTitle").textContent = item.label;
  el("viewDesc").textContent = item.desc || "";
  el("viewActs").innerHTML = "";

  App.view = item.view || item.key;
  App.navKey = item.key;
  if (window.onViewShown) window.onViewShown(App.view);
}
