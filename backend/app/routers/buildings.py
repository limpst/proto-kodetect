"""건축물 · 점검 · 결함 · 균열 진행 API."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import (
    DEFECT_LABELS_KO,
    INSPECTION_KIND_LABELS_KO,
    MEMBER_CLASSES,
    SAFETY_GRADE_DESCRIPTION_KO,
    ConditionGrade,
    DefectType,
    Environment,
    InspectionKind,
    SafetyGrade,
)
from ..grading import DefectAssessment, assess_inspection
from ..models import Building, CrackTrack, Defect, Inspection
from ..schemas import (
    BuildingIn,
    BuildingOut,
    DefectOut,
    InspectionIn,
    InspectionOut,
    MemberSummary,
    ProgressionOut,
)
from ..services.timeseries import ProgressionPoint, analyze_progression

router = APIRouter(prefix="/api", tags=["buildings"])


# ─── 건축물 ────────────────────────────────────────────────────
@router.get("/buildings", response_model=list[BuildingOut])
def list_buildings(db: Session = Depends(get_db)) -> list[BuildingOut]:
    out: list[BuildingOut] = []
    for b in db.scalars(select(Building).order_by(Building.id)).all():
        latest = db.scalars(
            select(Inspection)
            .where(Inspection.building_id == b.id)
            .order_by(Inspection.inspected_at.desc())
            .limit(1)
        ).first()
        n_insp = db.scalar(
            select(func.count(Inspection.id)).where(Inspection.building_id == b.id)
        )
        n_def = db.scalar(
            select(func.count(Defect.id))
            .join(Inspection, Defect.inspection_id == Inspection.id)
            .where(Inspection.building_id == b.id)
        )
        out.append(
            BuildingOut(
                **{k: getattr(b, k) for k in BuildingIn.model_fields},
                id=b.id,
                created_at=b.created_at,
                latest_grade=latest.safety_grade if latest else None,
                latest_index=latest.defect_index if latest else None,
                inspection_count=int(n_insp or 0),
                defect_count=int(n_def or 0),
            )
        )
    return out


@router.post("/buildings", response_model=BuildingOut, status_code=201)
def create_building(body: BuildingIn, db: Session = Depends(get_db)) -> BuildingOut:
    b = Building(**body.model_dump())
    db.add(b)
    db.commit()
    db.refresh(b)
    return BuildingOut(
        **{k: getattr(b, k) for k in BuildingIn.model_fields},
        id=b.id,
        created_at=b.created_at,
    )


@router.get("/buildings/{building_id}")
def get_building(building_id: int, db: Session = Depends(get_db)) -> dict:
    b = db.get(Building, building_id)
    if not b:
        raise HTTPException(404, "건축물을 찾을 수 없습니다")
    insp = db.scalars(
        select(Inspection)
        .where(Inspection.building_id == building_id)
        .order_by(Inspection.inspected_at)
    ).all()
    return {
        "building": {
            **{k: getattr(b, k) for k in BuildingIn.model_fields},
            "id": b.id,
        },
        "grade_description": (
            SAFETY_GRADE_DESCRIPTION_KO.get(SafetyGrade(insp[-1].safety_grade))
            if insp and insp[-1].safety_grade
            else None
        ),
        "history": [
            {
                "inspection_id": i.id,
                "at": i.inspected_at.isoformat(),
                "kind": INSPECTION_KIND_LABELS_KO.get(
                    InspectionKind(i.kind), i.kind
                ),
                "grade": i.safety_grade,
                "defect_index": i.defect_index,
                "health_index": round(max(0.0, 100.0 * (1 - i.defect_index)), 2),
            }
            for i in insp
        ],
    }


# ─── 점검 ──────────────────────────────────────────────────────
def _member_summaries(db: Session, inspection_id: int) -> list[MemberSummary]:
    defects = db.scalars(
        select(Defect).where(Defect.inspection_id == inspection_id)
    ).all()
    grouped: dict[str, list[DefectAssessment]] = {}
    for d in defects:
        grouped.setdefault(d.member_code, []).append(
            DefectAssessment(
                defect_type=DefectType(d.defect_type),
                grade=ConditionGrade(d.grade),
                severity=d.severity,
                repair_required=d.repair_required,
                basis=d.basis,
            )
        )
    result = assess_inspection(grouped)
    return [
        MemberSummary(
            member_code=m.member_code,
            member_label=(
                MEMBER_CLASSES[m.member_code].label_ko
                if m.member_code in MEMBER_CLASSES
                else m.member_code
            ),
            grade=m.grade.value,
            defect_index=m.defect_index,
            defect_count=m.defect_count,
        )
        for m in result.members
    ]


def _to_inspection_out(db: Session, i: Inspection) -> InspectionOut:
    defects = db.scalars(select(Defect).where(Defect.inspection_id == i.id)).all()
    return InspectionOut(
        id=i.id,
        building_id=i.building_id,
        kind=i.kind,
        kind_label=INSPECTION_KIND_LABELS_KO.get(InspectionKind(i.kind), i.kind),
        inspected_at=i.inspected_at,
        inspector=i.inspector,
        safety_grade=i.safety_grade,
        defect_index=i.defect_index,
        notes=i.notes,
        defect_count=len(defects),
        repair_required_count=sum(1 for d in defects if d.repair_required),
        members=_member_summaries(db, i.id),
    )


@router.get("/inspections", response_model=list[InspectionOut])
def list_inspections(
    building_id: int | None = None, db: Session = Depends(get_db)
) -> list[InspectionOut]:
    stmt = select(Inspection).order_by(Inspection.inspected_at.desc())
    if building_id:
        stmt = stmt.where(Inspection.building_id == building_id)
    return [_to_inspection_out(db, i) for i in db.scalars(stmt).all()]


@router.post("/inspections", response_model=InspectionOut, status_code=201)
def create_inspection(body: InspectionIn, db: Session = Depends(get_db)) -> InspectionOut:
    if not db.get(Building, body.building_id):
        raise HTTPException(404, "건축물을 찾을 수 없습니다")
    i = Inspection(
        building_id=body.building_id,
        kind=body.kind,
        inspected_at=body.inspected_at or datetime.now(),
        inspector=body.inspector,
        notes=body.notes,
    )
    db.add(i)
    db.commit()
    db.refresh(i)
    return _to_inspection_out(db, i)


@router.get("/inspections/{inspection_id}/defects", response_model=list[DefectOut])
def list_defects(inspection_id: int, db: Session = Depends(get_db)) -> list[DefectOut]:
    return [
        DefectOut.model_validate(d)
        for d in db.scalars(
            select(Defect)
            .where(Defect.inspection_id == inspection_id)
            .order_by(Defect.severity.desc())
        ).all()
    ]


@router.get("/defect-types")
def defect_types() -> list[dict]:
    return [
        {"code": t.value, "label": DEFECT_LABELS_KO[t]} for t in DefectType
    ]


@router.get("/member-classes")
def member_classes() -> list[dict]:
    return [
        {
            "code": c.code,
            "label": c.label_ko,
            "is_primary": c.is_primary,
            "weight": c.weight,
        }
        for c in MEMBER_CLASSES.values()
    ]


# ─── 균열 진행 (시계열) ────────────────────────────────────────
@router.get("/buildings/{building_id}/progression", response_model=list[ProgressionOut])
def progression(building_id: int, db: Session = Depends(get_db)) -> list[ProgressionOut]:
    b = db.get(Building, building_id)
    if not b:
        raise HTTPException(404, "건축물을 찾을 수 없습니다")
    env = Environment(b.environment)

    out: list[ProgressionOut] = []
    tracks = db.scalars(
        select(CrackTrack).where(CrackTrack.building_id == building_id)
    ).all()
    for t in tracks:
        rows = db.execute(
            select(Defect, Inspection.inspected_at)
            .join(Inspection, Defect.inspection_id == Inspection.id)
            .where(Defect.track_id == t.id, Defect.width_mm.is_not(None))
            .order_by(Inspection.inspected_at)
        ).all()
        points = [
            ProgressionPoint(at=at, width_mm=d.width_mm, inspection_id=d.inspection_id)
            for d, at in rows
        ]
        if not points:
            continue
        r = analyze_progression(t.id, t.label, t.member_code, points, env)
        out.append(
            ProgressionOut(
                track_id=r.track_id,
                label=r.label,
                member_code=r.member_code,
                model=r.model,
                rate_mm_per_year=(
                    round(r.rate_mm_per_year, 4) if r.rate_mm_per_year else None
                ),
                r_squared=round(r.r_squared, 4) if r.r_squared is not None else None,
                latest_width_mm=r.latest_width_mm,
                allowable_mm=r.allowable_mm,
                years_to_allowable=r.years_to_allowable,
                verdict=r.verdict,
                points=[
                    {"at": p.at, "width_mm": p.width_mm, "inspection_id": p.inspection_id}
                    for p in r.points
                ],
                forecast=[[d, v] for d, v in r.forecast],
            )
        )
    out.sort(key=lambda r: (r.years_to_allowable if r.years_to_allowable is not None else 1e9))
    return out
