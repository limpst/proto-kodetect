"""건축물 건강검진 (BHC-STD-2026) API.

기존 점검 데이터(Defect)를 표준의 결함 관측으로 사상하여 BHI·적신호·처방·
소견서를 산출한다. 법정 안전등급은 그대로 두고 나란히 표기한다.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bhc
from ..db import get_db
from ..domain import MEMBER_CLASSES, DefectType, Environment
from ..models import Building, Defect, Inspection
from ..opinion import build_sentences, build_summary, lint_opinion, system_comment

router = APIRouter(prefix="/api/bhc", tags=["bhc"])

# 확산도 ρ 의 분모(조사한 동종 부재 수). 현재 데이터 모델은 조사 부재 수를
# 기록하지 않으므로 운용 가정값을 두고, 응답에 그 사실을 함께 내보낸다.
# 표본계획이 도입되면 Inspection 에 실제 조사 부재 수를 저장해 대체한다.
DEFAULT_SURVEYED_PER_MEMBER = 10


def _observations(
    db: Session, inspection: Inspection, environment: Environment, surveyed: int
) -> list[bhc.DefectObservation]:
    """Defect 행 → 표준의 결함 관측. 확산도는 동종 부재 결함 수로 추정한다."""
    rows = db.scalars(
        select(Defect).where(Defect.inspection_id == inspection.id)
    ).all()

    # (부재, 유형)별 건수 → 확산도 분자
    counts: dict[tuple[str, str], int] = {}
    for d in rows:
        counts[(d.member_code, d.defect_type)] = (
            counts.get((d.member_code, d.defect_type), 0) + 1
        )

    serials: dict[str, int] = {}
    out: list[bhc.DefectObservation] = []
    for d in rows:
        dtype = DefectType(d.defect_type)
        severity, basis = bhc.classify_severity(
            dtype,
            width_mm=d.width_mm,
            area_ratio=d.area_ratio,
            environment=environment,
        )
        system = bhc.MEMBER_TO_SYSTEM.get(d.member_code, bhc.System.S1)
        key = f"{system.value}-{d.member_code}-{dtype.value}"
        serials[key] = serials.get(key, 0) + 1

        n = counts[(d.member_code, d.defect_type)]
        extent = min(1.0, n / max(surveyed, 1))

        out.append(
            bhc.DefectObservation(
                defect_type=dtype,
                member_code=d.member_code,
                severity=severity,
                extent=extent,
                width_mm=d.width_mm,
                area_ratio=d.area_ratio,
                basis=basis,
                location=(
                    MEMBER_CLASSES[d.member_code].label_ko
                    if d.member_code in MEMBER_CLASSES
                    else d.member_code
                ),
                defect_id=bhc.defect_id(system, d.member_code, dtype, serials[key]),
            )
        )
    return out


def _years_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / (365.25 * 86400.0)


@router.get("/systems")
def systems() -> list[dict]:
    """6대 계통 정의 — 가중치와 하한 제약."""
    return [
        {
            "code": s.code.value,
            "label": s.label_ko,
            "human_analogue": s.human_analogue,
            "scope": s.scope,
            "weight": s.weight,
            "weight_floor": s.weight_floor,
        }
        for s in bhc.SYSTEMS.values()
    ]


@router.get("/severity-scale")
def severity_scale() -> dict:
    """심각도 D1~D5와 균열폭 임계 · 처방 우선순위 정의."""
    return {
        "standard": bhc.STANDARD_ID,
        "severities": [
            {
                "code": s.value,
                "label": bhc.SEVERITY_LABELS_KO[s],
                "item_score": bhc.SEVERITY_SCORE[s],
            }
            for s in bhc.SEVERITY_ORDER
        ],
        "crack_width_bounds_mm": [
            {"below": (None if u == float("inf") else u), "severity": s.value}
            for u, s in bhc.CRACK_WIDTH_SEVERITY
        ],
        "priorities": [
            {
                "code": p.code.value,
                "label": p.label_ko,
                "trigger": p.trigger_ko,
                "due": p.due_text,
                "notify": p.notify_ko,
            }
            for p in bhc.PRIORITIES.values()
        ],
        "red_flags": [
            {
                "code": f.code,
                "condition": f.condition_ko,
                "bhi_cap": f.bhi_cap,
                "forced_grade": f.forced_grade,
                "auto_detectable": f.code in ("RF-1", "RF-2"),
            }
            for f in bhc.RED_FLAGS.values()
        ],
        "capa_states": [
            {
                "code": s.value,
                "label": bhc.CAPA_LABELS_KO[s],
                "next": [t.value for t in bhc.CAPA_TRANSITIONS[s]],
                "evidence": bhc.CAPA_EVIDENCE_KO.get(s, ""),
            }
            for s in bhc.CapaState
        ],
    }


@router.get("/{building_id}")
def checkup(
    building_id: int,
    inspection_id: int | None = None,
    surveyed_per_member: int = Query(
        DEFAULT_SURVEYED_PER_MEMBER, ge=1, le=1000,
        description="확산도 ρ 산정에 쓰는 조사 동종 부재 수 (가정값)",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """검진 1회의 종합 판정 — BHI · 적신호 · 건강나이 · 열화속도 · 처방 · 소견."""
    b = db.get(Building, building_id)
    if not b:
        raise HTTPException(404, "건축물을 찾을 수 없습니다")

    stmt = (
        select(Inspection)
        .where(Inspection.building_id == building_id)
        .order_by(Inspection.inspected_at.desc())
    )
    inspections = db.scalars(stmt).all()
    if not inspections:
        raise HTTPException(404, "점검 이력이 없습니다")

    current = (
        next((i for i in inspections if i.id == inspection_id), None)
        if inspection_id
        else inspections[0]
    )
    if current is None:
        raise HTTPException(404, "해당 점검 회차를 찾을 수 없습니다")

    idx = inspections.index(current)
    previous = inspections[idx + 1] if idx + 1 < len(inspections) else None

    environment = Environment(b.environment)
    obs = _observations(db, current, environment, surveyed_per_member)

    prev_result = None
    years_since_prev = None
    if previous is not None:
        prev_obs = _observations(db, previous, environment, surveyed_per_member)
        prev_result = bhc.assess_checkup(prev_obs)
        years_since_prev = _years_between(previous.inspected_at, current.inspected_at)

    actual_years = (
        float(current.inspected_at.year - b.completed_year) if b.completed_year else None
    )
    cohort = bhc.cohort_of(b.structure_type, b.facility_class)

    result = bhc.assess_checkup(
        obs,
        actual_years=actual_years,
        cohort=cohort,
        bhi_prev=prev_result.bhi if prev_result else None,
        years_since_prev=years_since_prev,
        issued=current.inspected_at.date(),
    )

    # §8.8은 BHI(적신호 적용 후)로 열화속도를 정의한다. 그런데 두 회차가 모두
    # 같은 적신호 상한에 걸리면 차이가 0이 되어 실제 악화가 가려진다.
    # 표준 값을 그대로 두되, 상한 적용 전 값으로 산출한 보조지표를 병기한다.
    rate_uncapped = bhc.deterioration_rate(
        result.bhi_raw,
        prev_result.bhi_raw if prev_result else None,
        years_since_prev,
        result.health_age.beta,
    )

    summary = build_summary(
        result,
        building_name=b.name,
        statutory_grade=current.safety_grade,
    )
    sentences = build_sentences(obs, result.prescriptions, environment)

    prev_scores = (
        {s.system: s.score for s in prev_result.systems} if prev_result else {}
    )

    return {
        "standard": result.standard,
        "building": {"id": b.id, "name": b.name, "cohort": cohort},
        "inspection": {
            "id": current.id,
            "at": current.inspected_at.isoformat(),
            "kind": current.kind,
            "statutory_grade": current.safety_grade,
            "checkup_id": bhc.checkup_id(
                str(b.id), current.inspected_at.year,
                {"regular": "BAS", "precise": "ADV",
                 "diagnosis": "ADV", "emergency": "TGT"}.get(current.kind, "BAS"),
                idx + 1,
            ),
            "level": bhc.KIND_TO_LEVEL.get(
                {"regular": "BAS", "precise": "ADV",
                 "diagnosis": "ADV", "emergency": "TGT"}.get(current.kind, "BAS"), "L1"
            ),
        },
        "bhi": result.bhi,
        "bhi_raw": result.bhi_raw,
        "grade": result.grade,
        "grade_label": result.grade_label_ko,
        "systems": [
            {
                "code": s.system.value,
                "label": s.label_ko,
                "score": s.score,
                "weight": s.weight,
                "defect_count": s.defect_count,
                "worst_severity": s.worst_severity.value,
                "capped_by_d5": s.capped_by_d5,
                "performed": s.performed,
                "previous": prev_scores.get(s.system),
                "comment": system_comment(s, prev_scores.get(s.system)),
            }
            for s in result.systems
        ],
        "red_flags": [
            {
                "code": f.code,
                "condition": f.condition_ko,
                "bhi_cap": f.bhi_cap,
                "forced_grade": f.forced_grade,
                "evidence": f.evidence,
            }
            for f in result.red_flags
        ],
        "health_age": {
            "bha_years": result.health_age.bha_years,
            "actual_years": result.health_age.actual_years,
            "deviation": result.health_age.deviation,
            "beta": result.health_age.beta,
            "cohort_label": result.health_age.cohort_label_ko,
            "interpretation": result.health_age.interpretation,
            "advisory_only": result.health_age.advisory_only,
        },
        "rate": {
            "value": result.rate.value,
            "baseline": result.rate.baseline,
            "verdict": result.rate.verdict,
            "action": result.rate.action,
            "saturated": (
                not result.rate.baseline
                and result.bhi < result.bhi_raw
                and abs(result.rate.value or 0.0) < 1e-9
            ),
            "uncapped": {
                "value": rate_uncapped.value,
                "verdict": rate_uncapped.verdict,
                "action": rate_uncapped.action,
                "note": (
                    "적신호 상한 적용 전 BHI로 산출한 보조지표입니다. "
                    "표준 §8.8의 규범값은 위의 value 이며, 두 회차가 같은 상한에 "
                    "걸려 규범값이 0으로 포화될 때 실제 악화를 읽기 위한 참고치입니다."
                ),
            },
        },
        "severity_counts": result.severity_counts,
        "prescriptions": [
            {
                "defect_id": p.defect_id,
                "system": p.system.value,
                "member_code": p.member_code,
                "member_label": (
                    MEMBER_CLASSES[p.member_code].label_ko
                    if p.member_code in MEMBER_CLASSES else p.member_code
                ),
                "defect_type": p.defect_type.value,
                "severity": p.severity.value,
                "priority": p.priority.value,
                "priority_label": bhc.PRIORITIES[p.priority].label_ko,
                "due_text": p.due_text,
                "due_date": p.due_date.isoformat() if p.due_date else None,
                "action": p.action_ko,
                "basis": p.basis_ko,
                "capa_state": bhc.CapaState.ISSUED.value,
            }
            for p in result.prescriptions
        ],
        "summary": {
            "headline": summary.headline,
            "grade_line": summary.grade_line,
            "health_age_line": summary.health_age_line,
            "rate_line": summary.rate_line,
            "red_flag_line": summary.red_flag_line,
            "prescription_line": summary.prescription_line,
            "next_checkup_line": summary.next_checkup_line,
            "caveats": summary.caveats,
        },
        "sentences": [
            {
                "defect_id": s.defect_id,
                "observation": s.observation,
                "interpretation": s.interpretation,
                "recommendation": s.recommendation,
            }
            for s in sentences
        ],
        "assumptions": [
            f"확산도 ρ 는 조사 동종 부재 수를 {surveyed_per_member}개로 가정해 "
            "산정했습니다. 표본계획이 도입되면 실제 조사 부재 수로 대체해야 합니다.",
        ],
    }


@router.post("/lint")
def lint(payload: dict) -> dict:
    """소견 문장의 §9.2 금지표현 검사. 사람이 쓴 문장에도 적용한다."""
    text = str(payload.get("text", ""))
    findings = lint_opinion(text)
    return {
        "clean": not findings,
        "findings": [
            {
                "category": f.category,
                "matched": f.matched,
                "reason": f.reason,
                "suggestion": f.suggestion,
                "position": f.position,
            }
            for f in findings
        ],
    }


@router.get("/{building_id}/capa")
def capa_board(
    building_id: int,
    surveyed_per_member: int = Query(DEFAULT_SURVEYED_PER_MEMBER, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """처방 폐루프 현황 — 기한 초과와 에스컬레이션 단계를 함께 계산한다."""
    data = checkup(building_id, None, surveyed_per_member, db)
    today = date.today()

    rows = []
    for p in data["prescriptions"]:
        due = date.fromisoformat(p["due_date"]) if p["due_date"] else None
        issued = date.fromisoformat(data["inspection"]["at"][:10])
        overdue = (today - due).days if due and today > due else 0
        esc = bhc.escalation_for(
            bhc.Priority(p["priority"]),
            bhc.CapaState.ISSUED,
            overdue,
            (today - issued).days,
        )
        rows.append(
            {
                **p,
                "days_overdue": overdue,
                "escalation": (
                    {"level": esc.level, "trigger": esc.trigger_ko, "action": esc.action_ko}
                    if esc else None
                ),
            }
        )

    total = len(rows) or 1
    return {
        "building_id": building_id,
        "prescriptions": rows,
        "metrics": {
            "issued": len(rows),
            "overdue": sum(1 for r in rows if r["days_overdue"] > 0),
            "escalated": sum(1 for r in rows if r["escalation"]),
            "closure_rate": 0.0,          # 종결 건수 ÷ 발행 건수 — 이행 기록 도입 후 산출
            "on_time_rate": round(
                sum(1 for r in rows if r["days_overdue"] == 0) / total, 3
            ),
        },
        "note": (
            "이행·검증 기록이 아직 저장되지 않아 모든 처방을 '발행' 상태로 봅니다. "
            "표준 §10.1은 이행(Executed)만으로 종결되지 않으며 검증(Verified)을 "
            "거쳐야 한다고 규정합니다."
        ),
    }
