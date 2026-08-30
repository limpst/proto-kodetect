"""프로젝트 — QuickGuide STEP 01·02.

  01  프로젝트 목록 화면에서 [새 프로젝트] 로 작성을 시작
  02  이름 · 저장 폴더 · 시설물 유형 (세 항목 모두 필수) → [시작하기]
      "점검 대상 시설물 이름은 자동으로 만들어지며, 자세한 정보는 나중에
       '프로젝트 정보' 화면에서 수정할 수 있습니다."

자동 생성 범위
--------------
프로젝트를 만들면 시설물 1개와 점검 회차 1개를 함께 만든다. 이게 없으면
사용자는 사진을 올릴 곳이 없어 다음 단계로 못 간다 — 빈 프로젝트를 만들어
두고 "이제 시설물을 만드십시오"라고 시키는 것은 단계를 하나 더 늘릴 뿐이다.
이름은 나중에 고칠 수 있게 해 둔다.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Building, Defect, Inspection, Photo, Project
from ..services.sensors import CHANNEL_SPECS, default_channels
from ..models import SensorChannel

router = APIRouter(prefix="/api/projects", tags=["projects"])

FACILITY_TYPES = ["건축물", "교량", "터널", "옹벽", "댐", "항만", "기타"]


class ProjectIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    save_dir: str = Field("", max_length=400)
    facility_type: str = "건축물"
    client: str = ""
    note: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    save_dir: str | None = None
    facility_type: str | None = None
    client: str | None = None
    note: str | None = None


def _summary(db: Session, p: Project) -> dict:
    b_ids = [b.id for b in p.buildings]
    n_insp = n_photo = n_def = 0
    latest_grade = None
    if b_ids:
        n_insp = db.scalar(
            select(func.count(Inspection.id)).where(Inspection.building_id.in_(b_ids))
        ) or 0
        i_ids = list(
            db.scalars(select(Inspection.id).where(Inspection.building_id.in_(b_ids))).all()
        )
        if i_ids:
            n_photo = db.scalar(
                select(func.count(Photo.id)).where(Photo.inspection_id.in_(i_ids))
            ) or 0
            n_def = db.scalar(
                select(func.count(Defect.id)).where(Defect.inspection_id.in_(i_ids))
            ) or 0
        last = db.scalars(
            select(Inspection)
            .where(Inspection.building_id.in_(b_ids))
            .order_by(Inspection.inspected_at.desc())
            .limit(1)
        ).first()
        latest_grade = last.safety_grade if last else None

    return {
        "id": p.id,
        "name": p.name,
        "save_dir": p.save_dir,
        "facility_type": p.facility_type,
        "client": p.client,
        "note": p.note,
        "created_at": p.created_at.isoformat(),
        "buildings": [{"id": b.id, "name": b.name} for b in p.buildings],
        "building_count": len(b_ids),
        "inspection_count": int(n_insp),
        "photo_count": int(n_photo),
        "defect_count": int(n_def),
        "latest_grade": latest_grade,
    }


@router.get("/facility-types")
def facility_types() -> list[str]:
    return FACILITY_TYPES


@router.get("")
def list_projects(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    return [_summary(db, p) for p in rows]


@router.post("", status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db)) -> dict:
    """새 프로젝트 시작 — 시설물과 첫 점검 회차를 함께 만든다."""
    if body.facility_type not in FACILITY_TYPES:
        raise HTTPException(400, f"시설물 유형은 {', '.join(FACILITY_TYPES)} 중 하나여야 합니다")

    p = Project(
        name=body.name.strip(),
        save_dir=body.save_dir.strip(),
        facility_type=body.facility_type,
        client=body.client.strip(),
        note=body.note.strip(),
    )
    db.add(p)
    db.flush()

    # 시설물 이름은 자동 생성 — 나중에 고칠 수 있다
    b = Building(
        project_id=p.id,
        name=f"{p.name} — 대상 시설물 1",
        facility_class="2종",
        structure_type="철근콘크리트" if body.facility_type == "건축물" else body.facility_type,
        environment="humid",
    )
    db.add(b)
    db.flush()

    # 계측 채널 — 모니터링 화면이 비어 있지 않도록 기본 구성을 깔아 둔다
    for code, kind, member, pos in default_channels(b.id):
        unit, _, warn, crit = CHANNEL_SPECS[kind]
        db.add(
            SensorChannel(
                building_id=b.id, code=code, kind=kind, member_code=member,
                unit=unit, warn_threshold=warn, critical_threshold=crit,
                position_x=pos[0], position_y=pos[1], position_z=pos[2],
            )
        )

    insp = Inspection(
        building_id=b.id,
        kind="regular",
        inspected_at=datetime.now(),
        inspector="",
        notes="프로젝트 생성 시 자동으로 만들어진 첫 회차입니다.",
    )
    db.add(insp)
    db.commit()
    db.refresh(p)

    out = _summary(db, p)
    out["first_inspection_id"] = insp.id
    out["note_ko"] = (
        "시설물과 첫 점검 회차를 함께 만들었습니다. 이름과 상세 정보는 "
        "프로젝트 정보에서 수정할 수 있습니다."
    )
    return out


@router.get("/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)) -> dict:
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    return _summary(db, p)


@router.patch("/{project_id}")
def update_project(
    project_id: int, body: ProjectPatch, db: Session = Depends(get_db)
) -> dict:
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    if body.facility_type and body.facility_type not in FACILITY_TYPES:
        raise HTTPException(400, "알 수 없는 시설물 유형입니다")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    db.commit()
    return _summary(db, p)


@router.delete("/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)) -> dict:
    """프로젝트만 지운다 — 시설물과 점검 데이터는 남긴다.

    프로젝트 삭제로 현장 사진과 판정 이력이 사라지면 되돌릴 방법이 없다.
    묶음을 푸는 것과 내용을 버리는 것은 다른 결정이므로 나눠 둔다.
    """
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    n = len(p.buildings)
    for b in p.buildings:
        b.project_id = None
    db.delete(p)
    db.commit()
    return {"ok": True, "buildings_detached": n}
