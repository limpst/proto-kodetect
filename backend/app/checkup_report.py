"""건축물 건강소견서 (BHC-STD-2026 §9) HTML 생성.

표준이 규정한 5부 구성을 그대로 따른다.

    제1부  1면 요약        — **반드시 단일 페이지**
    제2부  계통별 소견      — S1~S6 점수 · 주요 결함 3건 이내 · 전회 대비 증감
    제3부  결함 상세        — 코드 · 위치 · 측정값과 허용오차 · 심각도 · 확산도 · 근거
    제4부  처방            — 우선순위 · 조치 · 기한 · 개략 공사비 · 근거 조항
    제5부  부록            — 표본계획 · 장비 교정 · AI 사용내역과 인간검토 · 미실시 항목

설계상 지킨 것
--------------
* 1면 요약은 CSS `page-break-after` 로 물리적으로 한 장에 가둔다. "요약이 두 장"인
  소견서는 요약이 아니다.
* 적신호가 발동하면 1면에 **규칙번호와 근거 결함코드를 병기**한다(§8.5). 이를
  누락한 소견서는 표준을 준수한 것으로 보지 않는다.
* 공사비는 **개략**이며 그렇게 표기한다. 산출 근거(단가 기준)를 부록에 남긴다.
* 미실시 항목과 AI 사용 내역을 부록에 반드시 적는다. 무엇을 안 봤는지 밝히지 않은
  소견서는 "이상 없음"을 말할 자격이 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from html import escape

from . import bhc
from .domain import (
    ALLOWABLE_CRACK_WIDTH_MM,
    DEFECT_LABELS_KO,
    ENVIRONMENT_LABELS_KO,
    MEMBER_CLASSES,
    DefectType,
    Environment,
)

# ─── 개략 공사비 (백만원) ──────────────────────────────────────
# 심각도별 표준 조치의 부재 단위 개략 단가. 실적공사비가 아니라 계획 수립용
# 자릿수 추정이며, 예산 편성 전에 반드시 실적단가로 재산정해야 한다.
UNIT_COST_MW: dict[bhc.Severity, float] = {
    bhc.Severity.D5: 24.0,   # 응급 안전조치 + 구조검토 + 보강
    bhc.Severity.D4: 8.5,    # 단면복구 + 방청 + 마감
    bhc.Severity.D3: 2.4,    # 표면보수 + 보호도장
    bhc.Severity.D2: 0.3,    # 재측정·경과관찰
    bhc.Severity.D1: 0.0,
}

# 부재별 접근·가설 난이도 계수 (고소작업·지하 등)
ACCESS_FACTOR: dict[str, float] = {
    "column": 1.15, "girder": 1.35, "slab": 1.20, "wall_shear": 1.25,
    "foundation": 1.60, "retaining_wall": 1.30, "wall_non": 1.00,
    "parapet": 1.45, "finish": 0.90,
}

COST_BASIS_KO = (
    "심각도별 표준 조치 단가 × 부재 접근난이도 계수 × (1 + 확산도). "
    "가설·접근 비용을 부재별 계수로 반영했으며, 자재·노무 실적단가는 반영하지 "
    "않았습니다."
)


def estimate_cost(p: bhc.PrescriptionDraft, extent: float = 0.0) -> float:
    base = UNIT_COST_MW.get(p.severity, 0.0)
    factor = ACCESS_FACTOR.get(p.member_code, 1.0)
    return round(base * factor * (1.0 + min(max(extent, 0.0), 1.0)), 2)


# ─── 표시 도우미 ───────────────────────────────────────────────
GRADE_HEX = {"A": "#2f9e44", "B": "#66a80f", "C": "#d9a406", "D": "#e8590c", "E": "#d6336c"}
SEV_HEX = {"D1": "#2f9e44", "D2": "#66a80f", "D3": "#d9a406", "D4": "#e8590c", "D5": "#d6336c"}
PRIORITY_HEX = {"P0": "#d6336c", "P1": "#e8590c", "P2": "#d9a406", "P3": "#1971c2", "P4": "#868e96"}


def _member_label(code: str) -> str:
    c = MEMBER_CLASSES.get(code)
    return c.label_ko if c else code


def _chip(text: str, color: str) -> str:
    return (
        f'<span class="chip" style="background:{color}">{escape(text)}</span>'
    )


@dataclass
class ReportContext:
    building: dict
    inspection: dict
    result: bhc.CheckupResult
    observations: list[bhc.DefectObservation]
    summary_lines: dict
    sentences: list
    environment: Environment
    statutory_grade: str | None
    prev_scores: dict
    surveyed_per_member: int
    detector_name: str = "opencv-ridge-baseline"
    reviewer: str = ""


STYLE = """
@page { size: A4; margin: 16mm 14mm 18mm; }
*{box-sizing:border-box}
body{font-family:"Malgun Gothic","Apple SD Gothic Neo",-apple-system,sans-serif;
     color:#14181f;background:#fff;font-size:11.5px;line-height:1.6;margin:0}
.sheet{max-width:940px;margin:0 auto;padding:22px 26px 40px}
h1{font-size:21px;margin:0 0 3px;letter-spacing:-.02em}
h2{font-size:13.5px;margin:26px 0 9px;padding:7px 10px;background:#14181f;color:#fff;
   letter-spacing:-.01em;border-radius:3px}
h3{font-size:12px;margin:16px 0 7px;padding-bottom:4px;border-bottom:1px solid #ccd2db}
.sub{color:#5b6472;font-size:11px}
table{width:100%;border-collapse:collapse;margin-bottom:8px}
th,td{border:1px solid #ccd2db;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f1f4f8;font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.c,th.c{text-align:center}
.chip{display:inline-block;min-width:26px;padding:2px 8px;border-radius:3px;
      color:#fff;font-weight:700;font-size:10.5px;text-align:center;
      font-variant-numeric:tabular-nums}
.note{color:#5b6472;font-size:10.5px;line-height:1.7}
.warn{border-left:4px solid #e8590c;background:#fff4e6;padding:9px 12px;margin:10px 0}
.crit{border-left:4px solid #d6336c;background:#fff0f6;padding:9px 12px;margin:10px 0}
.info{border-left:4px solid #1971c2;background:#e7f5ff;padding:9px 12px;margin:10px 0}

/* 제1부 — 반드시 한 장 */
.part1{page-break-after:always}
.hero{display:flex;gap:16px;align-items:stretch;border:2px solid #14181f;
      padding:14px 16px;margin:10px 0 12px}
.hero .g{font-size:52px;font-weight:800;line-height:1;padding:6px 18px;border-radius:6px;
         color:#fff;display:flex;align-items:center;font-variant-numeric:tabular-nums}
.hero .m{flex:1;min-width:0}
.hero .bhi{font-size:30px;font-weight:800;font-variant-numeric:tabular-nums}
.hero .bhi small{font-size:12px;color:#5b6472;font-weight:600;margin-left:5px}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0}
.kv div{border:1px solid #ccd2db;padding:7px 9px}
.kv .k{font-size:10px;color:#5b6472}
.kv .v{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}
.sign{margin-top:26px;display:flex;gap:26px}
.sign div{flex:1;border-top:1px solid #14181f;padding-top:7px;font-size:10.5px}
.op{border:1px solid #ccd2db;padding:8px 10px;margin-bottom:7px}
.op .t{display:inline-block;width:30px;font-weight:700;color:#fff;text-align:center;
       border-radius:3px;font-size:10px;margin-right:6px}
@media print{.noprint{display:none}}
"""


def _part1(ctx: ReportContext) -> str:
    r = ctx.result
    s = ctx.summary_lines
    ha, rate = r.health_age, r.rate

    flags_html = ""
    if r.red_flags:
        rows = "".join(
            f"<tr><td class='c'>{_chip(f.code, '#d6336c')}</td>"
            f"<td>{escape(f.condition_ko)}</td>"
            f"<td class='num'>{f.bhi_cap:.1f}</td>"
            f"<td class='c'>{_chip(f.forced_grade, GRADE_HEX.get(f.forced_grade, '#868e96'))}</td>"
            f"<td class='note'>{escape(' / '.join(f.evidence[:3])) or '—'}</td></tr>"
            for f in r.red_flags
        )
        flags_html = f"""
        <div class="crit"><b>적신호 {len(r.red_flags)}건 발동</b> —
        가중합 {r.bhi_raw:.1f}점이 {r.bhi:.1f}점으로 강제 하향되었습니다 (§8.5)</div>
        <table><thead><tr><th class="c">규칙</th><th>조건</th>
        <th class="num">BHI 상한</th><th class="c">강제등급</th><th>근거 결함</th></tr></thead>
        <tbody>{rows}</tbody></table>"""
    else:
        flags_html = '<div class="info">적신호 발동 없음 — 가중합 BHI가 그대로 종합등급이 됩니다.</div>'

    dev = "—" if ha.deviation is None else f"{ha.deviation:+.1f}년"
    rate_txt = "기준선" if rate.baseline else f"{rate.value:+.2f} 점/년"

    return f"""
<section class="part1">
  <h1>건축물 건강소견서</h1>
  <div class="sub">{escape(ctx.result.standard)} · 검진번호 {escape(ctx.inspection['checkup_id'])}
    · 검진수준 {escape(ctx.inspection['level'])} · 검진일 {escape(ctx.inspection['at'][:10])}
    · 발행 {datetime.now():%Y-%m-%d %H:%M}</div>

  <h2>제1부 · 1면 요약</h2>

  <div class="hero">
    <div class="g" style="background:{GRADE_HEX.get(r.grade, '#868e96')}">{r.grade}</div>
    <div class="m">
      <div style="font-size:15px;font-weight:700">{escape(ctx.building['name'])}</div>
      <div class="bhi">{r.bhi:.1f}<small>/ 100 · {escape(r.grade_label_ko)}</small></div>
      <div class="note">{escape(s['grade_line'])}</div>
    </div>
  </div>

  <div class="kv">
    <div><div class="k">건강나이 (BHA)</div><div class="v">{ha.bha_years:.1f}년</div></div>
    <div><div class="k">노화편차 Δ</div><div class="v">{dev}</div></div>
    <div><div class="k">열화속도 v</div><div class="v">{rate_txt}</div></div>
    <div><div class="k">P0 / P1 처방</div><div class="v">{r.p0_count} / {r.p1_count}건</div></div>
  </div>

  {flags_html}

  <table>
    <tr><th style="width:120px">종합 판정</th><td>{escape(s['grade_line'])}</td></tr>
    <tr><th>건강나이</th><td>{escape(s['health_age_line'])}</td></tr>
    <tr><th>열화속도</th><td>{escape(s['rate_line'])}</td></tr>
    <tr><th>처방</th><td>{escape(s['prescription_line'])}</td></tr>
    <tr><th>다음 검진</th><td>{escape(s['next_checkup_line'])}</td></tr>
  </table>

  <div class="note" style="margin-top:8px">
    <b>유의사항</b><br/>
    {"<br/>".join("· " + escape(c) for c in s['caveats'])}
  </div>

  <div class="sign">
    <div>점검자 (서명)</div><div>책임기술자 (서명)</div><div>확인일자</div>
  </div>
</section>"""


def _part2(ctx: ReportContext) -> str:
    r = ctx.result
    by_system: dict[bhc.System, list[bhc.DefectObservation]] = {}
    for o in ctx.observations:
        by_system.setdefault(o.system, []).append(o)

    rows = []
    for sc in r.systems:
        prev = ctx.prev_scores.get(sc.system)
        delta = "—" if prev is None else f"{sc.score - prev:+.1f}"
        top = sorted(
            by_system.get(sc.system, []),
            key=lambda o: bhc.SEVERITY_ORDER.index(o.severity),
            reverse=True,
        )[:3]                                     # §9.1 — 주요 결함 3건 이내
        top_txt = (
            "<br/>".join(
                f"{escape(o.defect_id)} {_chip(o.severity.value, SEV_HEX[o.severity.value])} "
                f"{escape(o.basis)}"
                for o in top
            )
            or "관측된 결함 없음"
        )
        note = (
            "미실시 — D3 상당(65점) 처리" if not sc.performed
            else "계통 내 D5로 상한 30점 적용" if sc.capped_by_d5 else ""
        )
        rows.append(
            f"<tr><td class='c'><b>{sc.system.value}</b></td>"
            f"<td>{escape(sc.label_ko)}</td>"
            f"<td class='num'>{sc.score:.1f}</td>"
            f"<td class='num'>{'—' if prev is None else f'{prev:.1f}'}</td>"
            f"<td class='num'>{delta}</td>"
            f"<td class='num'>{sc.weight:.2f}</td>"
            f"<td class='num'>{sc.defect_count}</td>"
            f"<td>{top_txt}<div class='note'>{escape(note)}</div></td></tr>"
        )

    return f"""
<h2>제2부 · 계통별 소견</h2>
<table>
  <thead><tr><th class="c">계통</th><th>명칭</th><th class="num">점수</th>
  <th class="num">전회</th><th class="num">증감</th><th class="num">가중치</th>
  <th class="num">결함</th><th>주요 결함 (3건 이내)</th></tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
<div class="note">
  계통 건강점수 S<sub>i</sub> = Σ(중요도 × 보정항목점수) / Σ중요도 (§8.3) ·
  종합 건강지수 BHI = Σ(가중치 × S<sub>i</sub>) (§8.4)
</div>"""


def _part3(ctx: ReportContext) -> str:
    allowable = ALLOWABLE_CRACK_WIDTH_MM[ctx.environment]
    rows = []
    for o in sorted(
        ctx.observations,
        key=lambda x: bhc.SEVERITY_ORDER.index(x.severity),
        reverse=True,
    )[:150]:
        width = f"{o.width_mm:.2f} ± 0.02" if o.width_mm is not None else "—"
        area = f"{o.area_ratio * 100:.2f}%" if o.area_ratio is not None else "—"
        exceed = (
            "초과" if (o.width_mm is not None and o.width_mm >= allowable) else "이내"
        ) if o.defect_type is DefectType.CRACK else "—"
        rows.append(
            f"<tr><td class='mono'>{escape(o.defect_id)}</td>"
            f"<td>{escape(DEFECT_LABELS_KO.get(o.defect_type, o.defect_type.value))}</td>"
            f"<td>{escape(_member_label(o.member_code))}</td>"
            f"<td class='num'>{width}</td>"
            f"<td class='num'>{area}</td>"
            f"<td class='num'>{o.extent:.2f}</td>"
            f"<td class='c'>{_chip(o.severity.value, SEV_HEX[o.severity.value])}</td>"
            f"<td class='c'>{exceed}</td>"
            f"<td class='note'>{escape(o.basis)}</td></tr>"
        )

    sent = "".join(
        f"""<div class="op">
          <div class="note" style="margin-bottom:4px">{escape(x.defect_id)}</div>
          <div><span class="t" style="background:#1971c2">관측</span>{escape(x.observation)}</div>
          <div><span class="t" style="background:#7048e8">해석</span>{escape(x.interpretation)}</div>
          <div><span class="t" style="background:#e8590c">권고</span>{escape(x.recommendation)}</div>
        </div>"""
        for x in ctx.sentences[:25]
    )

    return f"""
<h2>제3부 · 결함 상세</h2>
<table>
  <thead><tr><th>결함코드</th><th>유형</th><th>부재</th>
  <th class="num">폭(mm)</th><th class="num">면적률</th><th class="num">확산도 ρ</th>
  <th class="c">심각도</th><th class="c">허용폭</th><th>판정 근거</th></tr></thead>
  <tbody>{"".join(rows) or "<tr><td colspan='9'>관측된 결함이 없습니다</td></tr>"}</tbody>
</table>
<div class="note">
  허용균열폭 판정 기준: {escape(ENVIRONMENT_LABELS_KO[ctx.environment])}
  {allowable:.2f}mm (KDS 14 20 30) · 측정 허용오차 ±0.02mm ·
  확산도 ρ = 결함 발현 부재 수 / 조사 동종 부재 수 (§8.2)
</div>

<h3>소견 문장 (§9.2 관측 · 해석 · 권고 분리)</h3>
{sent or "<div class='note'>기재할 소견이 없습니다.</div>"}"""


def _part4(ctx: ReportContext) -> str:
    r = ctx.result
    extent_by_id = {o.defect_id: o.extent for o in ctx.observations}

    rows = []
    total = 0.0
    for p in r.prescriptions:
        spec = bhc.PRIORITIES[p.priority]
        cost = estimate_cost(p, extent_by_id.get(p.defect_id, 0.0))
        total += cost
        due = p.due_date.isoformat() if p.due_date else "차기 검진"
        basis = f"§9.3 {p.priority.value} · {spec.trigger_ko}"
        rows.append(
            f"<tr><td class='c'>{_chip(p.priority.value, PRIORITY_HEX[p.priority.value])}</td>"
            f"<td class='mono'>{escape(p.defect_id)}</td>"
            f"<td>{escape(_member_label(p.member_code))}</td>"
            f"<td class='c'>{_chip(p.severity.value, SEV_HEX[p.severity.value])}</td>"
            f"<td>{escape(p.action_ko)}</td>"
            f"<td class='c'>{escape(due)}</td>"
            f"<td class='num'>{cost:.1f}</td>"
            f"<td class='note'>{escape(basis)}</td></tr>"
        )

    by_priority: dict[str, tuple[int, float]] = {}
    for p in r.prescriptions:
        c = estimate_cost(p, extent_by_id.get(p.defect_id, 0.0))
        n, s = by_priority.get(p.priority.value, (0, 0.0))
        by_priority[p.priority.value] = (n + 1, s + c)
    summary_rows = "".join(
        f"<tr><td class='c'>{_chip(k, PRIORITY_HEX[k])}</td>"
        f"<td>{escape(bhc.PRIORITIES[bhc.Priority(k)].label_ko)}</td>"
        f"<td>{escape(bhc.PRIORITIES[bhc.Priority(k)].due_text)}</td>"
        f"<td class='num'>{v[0]}</td><td class='num'>{v[1]:.1f}</td></tr>"
        for k, v in sorted(by_priority.items())
    )

    p0_note = (
        '<div class="crit"><b>P0 처방은 소견서 완성 여부와 무관하게 즉시 통보</b>하여야 '
        '합니다(§9.3). 소견서 작성을 이유로 P0 통보를 지연한 경우 표준 위반으로 봅니다.</div>'
        if r.p0_count else ""
    )

    return f"""
<h2>제4부 · 처방</h2>
{p0_note}
<h3>우선순위별 집계</h3>
<table>
  <thead><tr><th class="c">우선순위</th><th>구분</th><th>조치 기한</th>
  <th class="num">건수</th><th class="num">개략 공사비(백만원)</th></tr></thead>
  <tbody>{summary_rows or "<tr><td colspan='5'>처방 없음</td></tr>"}
  <tr><th colspan="3" style="text-align:right">합계</th>
      <th class="num">{len(r.prescriptions)}</th>
      <th class="num">{total:.1f}</th></tr></tbody>
</table>

<h3>처방 목록</h3>
<table>
  <thead><tr><th class="c">순위</th><th>결함코드</th><th>부재</th><th class="c">심각도</th>
  <th>조치 내용</th><th class="c">기한</th><th class="num">개략비용</th>
  <th>근거 조항</th></tr></thead>
  <tbody>{"".join(rows) or "<tr><td colspan='8'>처방 없음</td></tr>"}</tbody>
</table>
<div class="note"><b>공사비 산출 근거</b> — {escape(COST_BASIS_KO)}</div>"""


def _part5(ctx: ReportContext) -> str:
    r = ctx.result
    not_performed = [s for s in r.systems if not s.performed]
    np_rows = (
        "".join(
            f"<tr><td class='c'>{s.system.value}</td><td>{escape(s.label_ko)}</td>"
            f"<td>본 검진 범위에 포함되지 않음</td>"
            f"<td>§7.2에 따라 D3 상당(65점) 처리</td></tr>"
            for s in not_performed
        )
        or "<tr><td colspan='4'>미실시 항목 없음</td></tr>"
    )

    capa_rows = "".join(
        f"<tr><td class='c'>{escape(bhc.CAPA_LABELS_KO[st])}</td>"
        f"<td>{escape(', '.join(bhc.CAPA_LABELS_KO[t] for t in bhc.CAPA_TRANSITIONS[st]) or '—')}</td>"
        f"<td>{escape(bhc.CAPA_EVIDENCE_KO.get(st, '—'))}</td></tr>"
        for st in bhc.CapaState
    )

    return f"""
<h2>제5부 · 부록</h2>

<h3>부록 A · 표본계획 및 실시내역</h3>
<table>
  <tr><th style="width:150px">조사 방식</th>
      <td>영상 기반 자동 판독 (드론·현장 촬영) + 상시 계측</td></tr>
  <tr><th>확산도 분모</th>
      <td>조사 동종 부재 수를 <b>{ctx.surveyed_per_member}개로 가정</b>하여 산정.
      표본계획이 도입되면 실제 조사 부재 수로 대체하여야 합니다.</td></tr>
  <tr><th>관측 결함 수</th><td>{r.defect_count}건
      (D5 {r.severity_counts['D5']} · D4 {r.severity_counts['D4']} ·
       D3 {r.severity_counts['D3']} · D2 {r.severity_counts['D2']} ·
       D1 {r.severity_counts['D1']})</td></tr>
</table>

<h3>부록 B · 측정 방법 및 장비</h3>
<table>
  <tr><th style="width:150px">균열폭 측정</th>
      <td>중심선 법선 방향 밝기 프로파일의 반치전폭(FWHM)에서 촬영계 PSF를
      이차합으로 제거. 이진화 임계에 좌우되지 않는 정의입니다.</td></tr>
  <tr><th>픽셀-실치수 환산</th>
      <td>GSD = (촬영거리 × 센서폭) / (초점거리 × 이미지폭), 짐벌 경사는 1/cosθ 보정</td></tr>
  <tr><th>품질 게이트</th>
      <td>라플라시안 분산 기반 선명도 검사. 미달 영상은 재촬영 권고 후
      측정값을 신뢰하지 않습니다.</td></tr>
  <tr><th>교정</th>
      <td class="note">PSF 보정계수는 합성 벤치마크로 산정한 값입니다.
      <b>현장 배포 전 균열 게이지(0.1mm 단위 표준 스케일) 대조 촬영으로
      재보정하여야 합니다.</b></td></tr>
</table>

<h3>부록 C · AI 사용 내역 및 인간 검토 기록</h3>
<table>
  <tr><th style="width:150px">검출 엔진</th><td>{escape(ctx.detector_name)}</td></tr>
  <tr><th>AI 자율성 등급</th>
      <td>결함 검출·정량화·심각도 산정까지 자동. <b>등급 확정과 소견서 발행은
      사람의 승인을 요구</b>합니다 (HITL).</td></tr>
  <tr><th>소견 문장 생성</th>
      <td>측정값과 판정 근거로부터 규칙 기반 조립. 생성형 모델을 쓰지 않으므로
      기술용어 환각이 원리적으로 발생하지 않습니다.</td></tr>
  <tr><th>자동 판정 불가 항목</th>
      <td>적신호 RF-3~RF-6 · RF-8(소방설비 기능정지, 피난동선 적치, 방화구획,
      내력벽 제거, 가연성 외장재)은 영상으로 판정할 수 없어 현장 확인 입력에
      의존합니다. 본 소견서에서 해당 항목은
      {"발동됨" if any(f.code not in ("RF-1", "RF-2") for f in r.red_flags) else "입력되지 않았습니다"}.</td></tr>
  <tr><th>책임기술자 검토</th>
      <td>{escape(ctx.reviewer) if ctx.reviewer else
      "<b>미검토</b> — 본 소견서는 검토·서명 전 초안입니다."}</td></tr>
</table>

<h3>부록 D · 미실시 항목과 사유</h3>
<table>
  <thead><tr><th class="c">계통</th><th>명칭</th><th>사유</th><th>처리</th></tr></thead>
  <tbody>{np_rows}</tbody>
</table>

<h3>부록 E · 시정조치 폐루프(CAPA) 상태 정의</h3>
<table>
  <thead><tr><th class="c">상태</th><th>다음 상태</th><th>필수 증빙</th></tr></thead>
  <tbody>{capa_rows}</tbody>
</table>
<div class="note">
  <b>이행(Executed)만으로는 종결되지 않습니다.</b> 검증(Verified)을 거치지 않은
  처방은 다음 검진에서 미이행으로 계상됩니다(§10.1).
</div>

<div class="warn" style="margin-top:18px">
  <b>본 소견서의 법적 지위</b> — 본 표준은 시설물안전법·건축물관리법에 따른
  법정점검을 대체하지 않습니다. AI 분석 결과는 보조 참고자료이며, 법적 효력이 있는
  안전진단 결과로 사용하려면 책임기술자(구조기술사 등)의 현장 확인과 서명이
  반드시 필요합니다.
</div>"""


def render(ctx: ReportContext) -> str:
    title = f"건강소견서 — {ctx.building['name']} ({ctx.inspection['at'][:10]})"
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"/>
<title>{escape(title)}</title><style>{STYLE}</style></head>
<body><div class="sheet">
{_part1(ctx)}
{_part2(ctx)}
{_part3(ctx)}
{_part4(ctx)}
{_part5(ctx)}
<div class="note" style="margin-top:22px;text-align:center">
  {escape(ctx.result.standard)} · 발행 {datetime.now():%Y-%m-%d %H:%M} ·
  검진번호 {escape(ctx.inspection['checkup_id'])}
</div>
</div></body></html>"""
