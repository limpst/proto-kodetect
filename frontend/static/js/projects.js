/* 프로젝트 — QuickGuide STEP 01·02
 *
 * 01  프로젝트 목록에서 [새 프로젝트]
 * 02  이름 · 저장 폴더 · 시설물 유형 (셋 다 필수) → [시작하기]
 *
 * 저장 폴더에 관하여
 * ------------------
 * 데스크톱 제품에서는 결과가 실제로 저장될 폴더다. 웹에서는 브라우저 다운로드
 * 폴더로 내려가므로 경로 역할을 하지 않는다. 필드를 없애면 데스크톱판과 항목이
 * 어긋나고, 그대로 두면 동작을 오해한다. 그래서 남기되 무엇에 쓰이는지
 * (산출물 파일명 접두사·발주 구분) 화면에 적는다.
 */

const PJ = { list: [], types: [] };

async function pjLoad() {
  if (!PJ.types.length) {
    PJ.types = await api("/api/projects/facility-types");
  }
  PJ.list = await api("/api/projects");
  pjRender();
}

function pjRender() {
  el("pjCount").textContent = `${PJ.list.length}건`;

  if (!PJ.list.length) {
    el("pjGrid").innerHTML = `
      <div class="empty" style="grid-column:1/-1">
        아직 프로젝트가 없습니다. <b>새 프로젝트</b>로 시작하십시오.
      </div>`;
    return;
  }

  el("pjGrid").innerHTML = PJ.list
    .map(
      (p) => `
      <div class="card pj-card" data-project="${p.id}">
        <div class="pj-head">
          <div>
            <div class="pj-name">${esc(p.name)}</div>
            <div class="note">${esc(p.facility_type)}${
              p.client ? " · " + esc(p.client) : ""
            } · ${fmtDate(p.created_at)}</div>
          </div>
          ${gradeBadge(p.latest_grade)}
        </div>
        <div class="chips" style="margin-top:12px">
          ${chip("시설물", `<span class="num">${int(p.building_count)}</span>`, "동")}
          ${chip("점검 회차", `<span class="num">${int(p.inspection_count)}</span>`, "회")}
          ${chip("사진", `<span class="num">${int(p.photo_count)}</span>`, "장")}
          ${chip("결함", `<span class="num">${int(p.defect_count)}</span>`, "건")}
        </div>
        ${
          p.save_dir
            ? `<div class="note mono" style="margin-top:10px">${esc(p.save_dir)}</div>`
            : ""
        }
        <div class="field-row" style="margin-top:12px">
          <button class="primary" data-open="${p.id}">열기</button>
          <button class="ghost" data-edit="${p.id}">정보 수정</button>
        </div>
      </div>`
    )
    .join("");

  el("pjGrid")
    .querySelectorAll("[data-open]")
    .forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        pjOpen(Number(b.dataset.open));
      })
    );
  el("pjGrid")
    .querySelectorAll("[data-edit]")
    .forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        pjForm(PJ.list.find((x) => x.id === Number(b.dataset.edit)));
      })
    );
}

/** 프로젝트를 열면 그 시설물로 전환하고 진단 화면으로 보낸다. */
async function pjOpen(id) {
  const p = PJ.list.find((x) => x.id === id);
  if (!p || !p.buildings.length) {
    alert("이 프로젝트에는 시설물이 없습니다.");
    return;
  }
  const bid = p.buildings[0].id;
  const sel = el("buildingSel");
  if ([...sel.options].some((o) => Number(o.value) === bid)) {
    sel.value = String(bid);
    await selectBuilding(bid);
  } else {
    // 시설물 목록이 오래됐을 수 있다 — 새로 받아 다시 시도한다
    App.buildings = await api("/api/buildings");
    sel.innerHTML = App.buildings
      .map((b) => `<option value="${b.id}">${esc(b.name)}</option>`)
      .join("");
    sel.value = String(bid);
    await selectBuilding(bid);
  }
  location.hash = "#photos";
}

/* ─── 새 프로젝트 / 정보 수정 ───────────────────────────── */
function pjForm(existing) {
  const isNew = !existing;
  let box = el("pjModal");
  if (!box) {
    box = document.createElement("div");
    box.className = "modal";
    box.id = "pjModal";
    document.body.appendChild(box);
  }
  box.innerHTML = `
    <div class="modal-box" style="width:min(560px,94vw);height:auto">
      <div class="modal-head">
        <h2>${isNew ? "새 프로젝트 시작" : "프로젝트 정보"}</h2>
        <div class="modal-acts"></div>
        <button class="ghost" id="pjCancel">닫기</button>
      </div>
      <div style="padding:16px">
        <div class="field" style="margin-bottom:12px">
          <label for="pjName">프로젝트 이름 <span style="color:var(--crit)">*</span></label>
          <input id="pjName" type="text" placeholder="예: 아파트 정기점검"
                 value="${esc(existing?.name || "")}" />
        </div>
        <div class="field" style="margin-bottom:12px">
          <label for="pjDir">저장 폴더 <span style="color:var(--crit)">*</span></label>
          <input id="pjDir" type="text" placeholder="예: D:/안전진단/2026"
                 value="${esc(existing?.save_dir || "")}" />
          <div class="note">웹에서는 산출물이 브라우저 다운로드 폴더로 내려갑니다.
            이 값은 <b>파일명 접두사와 발주 구분 라벨</b>로 쓰입니다.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="pjType">시설물 유형 <span style="color:var(--crit)">*</span></label>
            <select id="pjType">
              ${PJ.types
                .map(
                  (t) =>
                    `<option value="${t}" ${
                      (existing?.facility_type || "건축물") === t ? "selected" : ""
                    }>${t}</option>`
                )
                .join("")}
            </select>
          </div>
          <div class="field" style="flex:1">
            <label for="pjClient">발주처</label>
            <input id="pjClient" type="text" placeholder="선택"
                   value="${esc(existing?.client || "")}" />
          </div>
        </div>
        <div id="pjErr" style="margin-top:12px"></div>
        <div class="note" style="margin-top:12px">
          ${
            isNew
              ? "시설물과 첫 점검 회차가 함께 만들어집니다. 이름은 나중에 고칠 수 있습니다."
              : "시설물·점검 데이터는 그대로 유지됩니다."
          }
        </div>
        <div class="field-row" style="margin-top:14px">
          <button class="primary" id="pjSubmit">${isNew ? "시작하기" : "저장"}</button>
          ${
            isNew
              ? ""
              : '<button class="ghost" id="pjDelete">프로젝트 삭제</button>'
          }
        </div>
      </div>
    </div>`;
  box.classList.add("open");

  const close = () => box.classList.remove("open");
  el("pjCancel").addEventListener("click", close);
  box.addEventListener("click", (e) => e.target === box && close());

  el("pjSubmit").addEventListener("click", async () => {
    const name = el("pjName").value.trim();
    const dir = el("pjDir").value.trim();
    if (!name || !dir) {
      el("pjErr").innerHTML =
        '<div class="alert">프로젝트 이름과 저장 폴더는 필수입니다.</div>';
      return;
    }
    const body = {
      name,
      save_dir: dir,
      facility_type: el("pjType").value,
      client: el("pjClient").value.trim(),
    };
    try {
      if (isNew) {
        const out = await api("/api/projects", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        App.buildings = await api("/api/buildings");
        el("buildingSel").innerHTML = App.buildings
          .map((b) => `<option value="${b.id}">${esc(b.name)}</option>`)
          .join("");
        close();
        await pjLoad();
        await pjOpen(out.id);
      } else {
        await api(`/api/projects/${existing.id}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        close();
        await pjLoad();
      }
    } catch (e) {
      el("pjErr").innerHTML = `<div class="alert critical">${esc(e.message)}</div>`;
    }
  });

  el("pjDelete")?.addEventListener("click", async () => {
    if (
      !confirm(
        "프로젝트를 삭제합니다.\n\n시설물과 점검 데이터는 남습니다 — 묶음만 풀립니다.\n계속하시겠습니까?"
      )
    )
      return;
    try {
      await api(`/api/projects/${existing.id}`, { method: "DELETE" });
      close();
      await pjLoad();
    } catch (e) {
      el("pjErr").innerHTML = `<div class="alert critical">${esc(e.message)}</div>`;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  el("pjNew")?.addEventListener("click", () => pjForm(null));
});
