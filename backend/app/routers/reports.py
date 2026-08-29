"""판정서(점검 결과 보고서) 생성 API."""

from __future__ import annotations

from datetime import datetime
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import (
    ALLOWABLE_CRACK_WIDTH_MM,
    CONDITION_GRADE_LABELS_KO,
    DEFECT_LABELS_KO,
    ENVIRONMENT_LABELS_KO,
    INSPECTION_KIND_LABELS_KO,
    MEMBER_CLASSES,
    SAFETY_GRADE_DESCRIPTION_KO,
    ConditionGrade,
    DefectType,
    Environment,
    InspectionKind,
    SafetyGrade,
)
from ..models import Building, Defect, Inspection

router = APIRouter(prefix="/api/reports", tags=["reports"])

_STYLE = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body { font-family: "Malgun Gothic", "Apple SD Gothic Neo", -apple-system, sans-serif;
       color: #14181f; background: #fff; font-size: 12px; line-height: 1.65; margin: 0; }
.sheet { max-width: 900px; margin: 0 auto; padding: 28px 32px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 14px; margin: 26px 0 10px; padding-bottom: 6px;
     border-bottom: 2px solid #14181f; letter-spacing: -0.01em; }
.sub { color: #5b6472; font-size: 12px; margin-bottom: 22px; }
table { width: 100%; border-collapse: collapse; margin-bottom: 6px; }
th, td { border: 1px solid #ccd2db; padding: 7px 9px; text-align: left;
         vertical-align: top; }
th { background: #f1f4f8; font-weight: 600; width: 130px; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
thead th { width: auto; text-align: center; }
.grade { display: inline-block; min-width: 30px; padding: 3px 10px; border-radius: 4px;
         font-weight: 700; text-align: center; color: #fff;
         font-variant-numeric: tabular-nums; }
.g-A,.g-a { background: #2f9e44; } .g-B,.g-b { background: #66a80f; }
.g-C,.g-c { background: #d9a406; } .g-D,.g-d { background: #e8590c; }
.g-E,.g-e { background: #d6336c; }
.verdict { border: 2px solid #14181f; padding: 14px 16px; margin: 14px 0 6px;
           background: #f8fafc; }
.verdict .big { font-size: 30px; font-weight: 800; letter-spacing: -0.02em; }
.note { color: #5b6472; font-size: 11px; margin-top: 6px; }
.warn { border-left: 4px solid #e8590c; background: #fff4e6; padding: 10px 14px;
        margin: 12px 0; font-size: 11.5px; }
.sign { margin-top: 34px; display: flex; gap: 40px; }
.sign div { flex: 1; border-top: 1px solid #14181f; padding-top: 8px; font-size: 11px; }
@media print { .noprint { display: none; } }
"""


def _grade_html(g: str | None) -> str:
    if not g:
        return '<span class="grade" style="background:#868e96">-</span>'
    return f'<span class="grade g-{escape(g)}">{escape(g.upper())}</span>'


@router.get("/inspection/{inspection_id}", response_class=HTMLResponse)
def inspection_report(inspection_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """점검 1회차의 판정서를 HTML로 생성한다 (브라우저 인쇄로 PDF 변환)."""
    insp = db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "점검 회차를 찾을 수 없습니다")
    b = db.get(Building, insp.building_id)
    env = Environment(b.environment if b else "humid")
    allowable = ALLOWABLE_CRACK_WIDTH_MM[env]

    defects = db.scalars(
        select(Defect)
        .where(Defect.inspection_id == inspection_id)
        .order_by(Defect.severity.desc())
    ).all()

    # 부재별 집계
    by_member: dict[str, list[Defect]] = {}
    for d in defects:
        by_member.setdefault(d.member_code, []).append(d)

    grade = insp.safety_grade
    desc = SAFETY_GRADE_DESCRIPTION_KO.get(SafetyGrade(grade)) if grade else "-"
    repairs = [d for d in defects if d.repair_required]

    member_rows = "".join(
        f"<tr><td>{escape(MEMBER_CLASSES[c].label_ko if c in MEMBER_CLASSES else c)}</td>"
        f"<td>{'주요부재' if c in MEMBER_CLASSES and MEMBER_CLASSES[c].is_primary else '보조부재'}</td>"
        f"<td class='num'>{len(ds)}</td>"
        f"<td class='num'>{sum(1 for d in ds if d.repair_required)}</td>"
        f"<td>{_grade_html(max(ds, key=lambda d: d.severity).grade)}</td></tr>"
        for c, ds in sorted(
            by_member.items(), key=lambda kv: -max(d.severity for d in kv[1])
        )
    ) or "<tr><td colspan='5'>검출된 결함이 없습니다</td></tr>"

    defect_rows = "".join(
        f"<tr><td class='num'>{i}</td>"
        f"<td>{escape(DEFECT_LABELS_KO.get(DefectType(d.defect_type), d.defect_type))}</td>"
        f"<td>{escape(MEMBER_CLASSES[d.member_code].label_ko if d.member_code in MEMBER_CLASSES else d.member_code)}</td>"
        f"<td class='num'>{f'{d.width_mm:.2f}' if d.width_mm is not None else '-'}</td>"
        f"<td class='num'>{f'{d.length_mm:.0f}' if d.length_mm is not None else '-'}</td>"
        f"<td>{_grade_html(d.grade)}</td>"
        f"<td>{'보수 필요' if d.repair_required else '경과관찰'}</td>"
        f"<td>{escape(d.basis)}</td></tr>"
        for i, d in enumerate(defects[:120], start=1)
    ) or "<tr><td colspan='8'>검출된 결함이 없습니다</td></tr>"

    over = [
        d for d in defects
        if d.defect_type == DefectType.CRACK.value
        and d.width_mm is not None
        and d.width_mm >= allowable
    ]
    warn_block = (
        f"<div class='warn'><b>허용균열폭 초과 {len(over)}건</b> — "
        f"{ENVIRONMENT_LABELS_KO[env]}의 허용균열폭은 {allowable:.2f}mm입니다 "
        f"(KDS 14 20 30). 해당 균열은 내구성 확보를 위해 보수 대상입니다.</div>"
        if over else ""
    )

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"/>
<title>판정서 — {escape(b.name if b else '')} ({insp.inspected_at:%Y-%m-%d})</title>
<style>{_STYLE}</style></head><body><div class="sheet">
<h1>시설물 상태평가 판정서</h1>
<div class="sub">
  {escape(INSPECTION_KIND_LABELS_KO.get(InspectionKind(insp.kind), insp.kind))} ·
  점검일 {insp.inspected_at:%Y-%m-%d} ·
  발행 {datetime.now():%Y-%m-%d %H:%M}
</div>

<h2>1. 시설물 개요</h2>
<table>
  <tr><th>시설물명</th><td>{escape(b.name if b else '-')}</td>
      <th>시설물 종별</th><td>{escape(b.facility_class if b else '-')}</td></tr>
  <tr><th>소재지</th><td colspan="3">{escape(b.address if b else '-')}</td></tr>
  <tr><th>구조형식</th><td>{escape(b.structure_type if b else '-')}</td>
      <th>준공연도</th><td>{b.completed_year or '-'}</td></tr>
  <tr><th>규모</th>
      <td>지상 {b.floors_above or '-'}층 / 지하 {b.floors_below or '-'}층,
          연면적 {f'{b.gross_area_m2:,.0f}' if b and b.gross_area_m2 else '-'} m²</td>
      <th>노출환경</th><td>{escape(ENVIRONMENT_LABELS_KO[env])}</td></tr>
  <tr><th>점검자</th><td>{escape(insp.inspector or '-')}</td>
      <th>허용균열폭</th><td>{allowable:.2f} mm</td></tr>
</table>

<h2>2. 종합 판정</h2>
<div class="verdict">
  <div class="big">{_grade_html(grade)} &nbsp; 종합 안전등급</div>
  <div style="margin-top:8px">{escape(desc or '-')}</div>
  <div class="note">
    결함도 지수 {insp.defect_index:.4f} · 검출 결함 {len(defects)}건 ·
    보수 필요 {len(repairs)}건
  </div>
</div>
{warn_block}

<h2>3. 부재별 상태평가</h2>
<table>
  <thead><tr><th>부재</th><th>구분</th><th>결함 수</th><th>보수 필요</th>
  <th>상태등급</th></tr></thead>
  <tbody>{member_rows}</tbody>
</table>
<div class="note">상태등급 a 우수 · b 양호 · c 보통 · d 미흡 · e 불량
({', '.join(f'{k.value} {v}' for k, v in CONDITION_GRADE_LABELS_KO.items())})</div>

<h2>4. 결함 목록</h2>
<table>
  <thead><tr><th>No</th><th>유형</th><th>부재</th><th>폭(mm)</th><th>길이(mm)</th>
  <th>등급</th><th>조치</th><th>판정 근거</th></tr></thead>
  <tbody>{defect_rows}</tbody>
</table>

<h2>5. 적용 기준 및 한계</h2>
<table>
  <tr><th>적용 기준</th><td>
    시설물의 안전 및 유지관리에 관한 특별법 · 안전점검 및 정밀안전진단 세부지침 ·
    KDS 14 20 30 (콘크리트구조 사용성 설계기준)
  </td></tr>
  <tr><th>측정 방법</th><td>
    영상 기반 자동 균열 검출. 균열폭은 중심선 법선 방향 밝기 프로파일의
    반치전폭(FWHM)에서 촬영계 PSF를 보정해 산출하며, 픽셀-실치수 환산은
    촬영거리 기반 GSD를 적용함.
  </td></tr>
  <tr><th>한계</th><td>
    본 판정서의 AI 분석 결과는 <b>보조 참고자료</b>이며, 법적 효력이 있는
    안전진단 결과로 사용하려면 책임기술자(구조기술사 등)의 현장 확인과 서명이
    반드시 필요합니다. 촬영 선명도가 부족한 영상은 균열폭이 과대평가될 수
    있으므로 재촬영 후 재분석해야 합니다.
  </td></tr>
</table>

<div class="sign">
  <div>점검자 (서명)</div><div>책임기술자 (서명)</div><div>확인일자</div>
</div>
</div></body></html>"""
    return HTMLResponse(html)
