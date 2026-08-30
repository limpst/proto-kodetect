"""app.html 셸을 2단 내비게이션 구조로 교체하고 진단 화면을 재구성한다.

한 번 쓰고 버리는 스크립트가 아니라 저장소에 남긴다. 화면 구조를 크게
바꿀 때 무엇을 어떻게 바꿨는지가 diff보다 이 파일에서 더 잘 읽히기 때문이다.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = ROOT / "frontend" / "app.html"

NEW_SHELL = """<div class="app">

  <!-- ─── 상단: 대분류 ─────────────────────────────────── -->
  <header class="topbar2">
    <div class="brand2">
      <span class="mk">KO-Detect</span>
      <span class="ko">안전진단 플랫폼</span>
    </div>

    <nav class="groups" id="groupNav"></nav>

    <div class="right">
      <select id="buildingSel" title="대상 시설물"></select>
      <div class="ticker" title="점검 결과와 실시간 계측을 결합한 현재 건전성">
        <div>
          <div class="tk-l">건전성 MTM</div>
          <div class="tk-v" id="tickHealth">—<span class="tk-d" id="tickDelta"></span></div>
        </div>
        <canvas id="tickSpark" width="76" height="26"></canvas>
        <span class="grade g-none" id="tickGrade">—</span>
      </div>
      <div class="live-dot" id="liveDot"><i></i><span>연결 대기</span></div>
    </div>
  </header>

  <div class="body2">
    <!-- ─── 좌측: 기능 ─────────────────────────────────── -->
    <aside class="subnav">
      <nav id="subNav"></nav>
      <div class="foot2">
        <div id="userLine">—</div>
        <div style="margin-top:6px">
          <a href="#" id="logout" style="color:var(--text-dim)">로그아웃</a>
        </div>
      </div>
    </aside>

    <!-- ─── 화면 ───────────────────────────────────────── -->
    <div class="stage">
      <div class="stage-head">
        <div>
          <h1 id="viewTitle">종합 현황</h1>
          <div class="desc" id="viewDesc"></div>
        </div>
        <div class="acts" id="viewActs"></div>
      </div>
"""

NEW_DETECT = """      <!-- ── 진단 · 영상 분석 ── -->
      <section class="view" id="view-detect">

        <div class="steps" id="dtSteps"></div>

        <div class="row wide-right">
          <!-- 입력 -->
          <div class="card">
            <h2>분석 입력 <span class="hint">사진을 끌어다 놓거나 클릭해 선택</span></h2>

            <div class="dropzone" id="dtDrop">
              <div class="dz-ico">⬓</div>
              <div class="dz-t">사진을 여기에 놓으십시오</div>
              <div class="dz-s">JPG · PNG · WebP · 최대 20MB</div>
            </div>
            <input id="dtFile" type="file" accept="image/*" hidden />

            <div class="field-row" style="margin-top:12px">
              <div class="field">
                <label for="dtInsp">점검 회차</label>
                <select id="dtInsp"></select>
              </div>
              <div class="field">
                <label for="dtMember">부재</label>
                <select id="dtMember"></select>
              </div>
            </div>

            <h2 style="margin-top:16px">스케일 (GSD)
              <span class="hint">픽셀을 mm로 바꾸는 기준</span></h2>
            <div class="field-row">
              <div class="field">
                <label for="dtDist">촬영거리 (m)</label>
                <input id="dtDist" type="number" step="0.1" placeholder="예: 8.0" />
              </div>
              <div class="field">
                <label for="dtGsd">직접 입력 (mm/px)</label>
                <input id="dtGsd" type="number" step="0.001" placeholder="거리로 산정" />
              </div>
            </div>
            <div class="note" id="dtScaleNote">
              스케일이 없으면 균열폭을 mm로 환산할 수 없어 등급 판정이 성립하지 않습니다.
            </div>

            <h2 style="margin-top:16px">검출 감도
              <span class="hint" id="dtSensVal">1.0</span></h2>
            <div class="field-row">
              <input id="dtSens" type="range" min="0.5" max="3" step="0.1" value="1" />
              <span class="note" style="flex:1;min-width:170px">
                높이면 미세 균열까지 잡지만 오검출이 늘어납니다.
              </span>
            </div>

            <div class="field-row" style="margin-top:14px">
              <button class="primary" id="dtRun">분석 실행</button>
              <button id="dtDemo">합성 표본 시연</button>
              <button class="ghost" id="dtReset">초기화</button>
            </div>

            <div id="dtStatus" style="margin-top:10px"></div>
          </div>

          <!-- 결과 뷰어 -->
          <div class="card">
            <h2>검출 결과 <span class="hint" id="dtHint">분석을 실행하면 표시됩니다</span></h2>

            <div class="field-row" style="margin-bottom:10px">
              <button class="ghost" id="dtModeCompare">원본 비교</button>
              <button class="ghost" id="dtModeMarks">중심선 표시</button>
              <button class="ghost" id="dtModeOverlay">서버 오버레이</button>
              <span class="note" style="margin-left:auto" id="dtViewNote"></span>
            </div>

            <div id="dtViewer" class="empty">분석 결과가 여기에 표시됩니다</div>
          </div>
        </div>

        <div class="row c3">
          <div class="card">
            <h2>촬영 품질 <span class="hint">측정 신뢰도의 전제</span></h2>
            <div id="dtQuality"></div>
          </div>
          <div class="card">
            <h2>등급 분포 <span class="hint">검출된 균열의 상태등급</span></h2>
            <div id="dtGrades"></div>
          </div>
          <div class="card">
            <h2>균열폭 분포 <span class="hint">mm</span></h2>
            <div id="dtHist"></div>
          </div>
        </div>

        <div class="row">
          <div class="card">
            <h2>균열 상세
              <span class="hint">행을 클릭하면 영상에서 해당 균열이 강조됩니다</span></h2>
            <div class="field-row" style="margin-bottom:8px">
              <button class="ghost" id="dtCsv">CSV 내보내기</button>
              <span class="note" id="dtSelNote" style="margin-left:auto"></span>
            </div>
            <div class="table-wrap table-scroll"><table id="dtCracks"></table></div>
          </div>
        </div>
      </section>
"""


def main() -> int:
    src = HTML.read_text(encoding="utf-8")

    # 1) 새 스타일시트 연결
    if "shell.css" not in src:
        src = src.replace(
            '<link rel="stylesheet" href="/static/css/app.css" />',
            '<link rel="stylesheet" href="/static/css/app.css" />\n'
            '<link rel="stylesheet" href="/static/css/shell.css" />',
        )

    # 2) 셸 교체 — <div class="shell"> ... <div class="content"> 까지
    start = src.index('<div class="shell">')
    end = src.index('<div class="content">') + len('<div class="content">')
    src = src[:start] + NEW_SHELL + src[end:]

    # 3) 진단 화면 교체
    ds = src.index('<section class="view" id="view-detect">')
    # 다음 섹션 시작 지점까지가 진단 화면
    de = src.index('<section class="view"', ds + 10)
    # 섹션 앞의 주석 줄도 함께 걷어낸다
    head = src.rfind("<!--", 0, ds)
    if head != -1 and ds - head < 200:
        ds = head
    src = src[:ds] + NEW_DETECT + "\n      " + src[de:]

    HTML.write_text(src, encoding="utf-8")
    print(f"app.html 재구성 완료 — {len(src.splitlines())}줄")
    return 0


if __name__ == "__main__":
    sys.exit(main())
