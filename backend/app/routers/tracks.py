"""회차 간 균열 자동 매칭 API.

`services/tracking.py` 가 계산하고, 여기서 `CrackTrack` 에 반영한다.

자동으로 잇되 조용히 잇지는 않는다
----------------------------------
매칭이 틀리면 진행 속도가 통째로 틀어지고, 그 속도가 보수 우선순위와 잔여
수명 예측에 그대로 들어간다. 그래서 두 가지를 지킨다.

1. 미리보기(`/preview`)와 적용(`/apply`)을 나눈다. 무엇이 어떻게 이어질지
   보고 나서 적용할 수 있어야 한다.
2. 점수가 낮은 연결은 `needs_review` 로 표시하고, 기본값에서는 적용하지 않는다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import MEMBER_CLASSES, DefectType
from ..models import Building, CrackTrack, Defect, Inspection, Photo, PhotoGroup
from ..services.tracking import REVIEW_SCORE, match, shape_of

router = APIRouter(prefix="/api/tracks", tags=["tracks"])


class AutoLinkIn(BaseModel):
    building_id: int
    min_score: float = 0.45
    # 낮은 점수 연결까지 적용할지. 기본은 사람이 확인해야 하는 것을 넘기지 않는다.
    include_review: bool = False


def _shapes(db: Session, inspection_id: int) -> list:
    """한 회차의 균열을 기하 요약으로 바꾼다.

    사진 그룹 '이름'을 위치 식별자로 쓴다. 그룹 행은 회차마다 새로 생기지만
    이름은 현장에서 부재·위치로 정한 것이라 회차가 달라도 같은 곳을 가리킨다.
    그룹이 없는 사진은 부재코드를 위치로 대신 쓴다.
    """
    rows = db.execute(
        select(Defect, Photo, PhotoGroup)
        .join(Photo, Defect.photo_id == Photo.id, isouter=True)
        .join(PhotoGroup, Photo.group_id == PhotoGroup.id, isouter=True)
        .where(
            Defect.inspection_id == inspection_id,
            Defect.defect_type == DefectType.CRACK.value,
        )
    ).all()

    out = []
    for d, ph, g in rows:
        out.append(
            shape_of(
                d.id,
                d.polyline,
                member_code=d.member_code,
                group_name=(g.name if g else f"__member:{d.member_code}"),
                mm_per_px=(ph.gsd_mm_per_px if ph else None),
                width_mm=d.width_mm,
                length_mm=d.length_mm,
            )
        )
    return out


def _plan(db: Session, building_id: int, min_score: float) -> dict:
    if not db.get(Building, building_id):
        raise HTTPException(404, "건축물을 찾을 수 없습니다")

    insps = db.scalars(
        select(Inspection)
        .where(Inspection.building_id == building_id)
        .order_by(Inspection.inspected_at)
    ).all()
    if len(insps) < 2:
        raise HTTPException(
            400, "회차가 2개 이상이어야 비교할 수 있습니다 (현재 %d개)" % len(insps)
        )

    steps = []
    for prev_i, curr_i in zip(insps, insps[1:]):
        a, b = _shapes(db, prev_i.id), _shapes(db, curr_i.id)
        links, gone, new = match(a, b, min_score=min_score)
        steps.append(
            {
                "from": {"id": prev_i.id, "at": prev_i.inspected_at.isoformat(),
                         "cracks": len(a)},
                "to": {"id": curr_i.id, "at": curr_i.inspected_at.isoformat(),
                       "cracks": len(b)},
                "links": links,
                "disappeared": gone,
                "new": new,
                "review_count": sum(1 for x in links if x["needs_review"]),
            }
        )
    return {
        "building_id": building_id,
        "inspections": len(insps),
        "steps": steps,
        "review_threshold": REVIEW_SCORE,
        "total_links": sum(len(s["links"]) for s in steps),
        "total_review": sum(s["review_count"] for s in steps),
    }


@router.get("/preview/{building_id}")
def preview(building_id: int, min_score: float = 0.45, db: Session = Depends(get_db)) -> dict:
    """무엇이 어떻게 이어질지 보여준다 — 아무것도 바꾸지 않는다."""
    out = _plan(db, building_id, min_score)
    # 근거를 사람이 읽을 수 있게 덧붙인다
    for st in out["steps"]:
        for ln in st["links"]:
            d = db.get(Defect, ln["curr_defect_id"])
            p = db.get(Defect, ln["prev_defect_id"])
            ln["member_label"] = (
                MEMBER_CLASSES[d.member_code].label_ko
                if d and d.member_code in MEMBER_CLASSES else (d.member_code if d else "")
            )
            ln["width_prev_mm"] = p.width_mm if p else None
            ln["width_curr_mm"] = d.width_mm if d else None
            if p and d and p.width_mm and d.width_mm:
                ln["width_delta_mm"] = round(d.width_mm - p.width_mm, 3)
    return out


@router.post("/auto-link")
def auto_link(body: AutoLinkIn, db: Session = Depends(get_db)) -> dict:
    """연결을 실제로 적용한다 — CrackTrack 을 만들거나 이어 붙인다."""
    plan = _plan(db, body.building_id, body.min_score)

    created = extended = skipped = 0
    for st in plan["steps"]:
        for ln in st["links"]:
            if ln["needs_review"] and not body.include_review:
                skipped += 1
                continue
            prev_d = db.get(Defect, ln["prev_defect_id"])
            curr_d = db.get(Defect, ln["curr_defect_id"])
            if not prev_d or not curr_d:
                continue
            if curr_d.track_id:
                continue  # 이미 이어져 있다 — 사람이 정한 것을 덮지 않는다

            track = db.get(CrackTrack, prev_d.track_id) if prev_d.track_id else None
            if track is None:
                member = MEMBER_CLASSES.get(prev_d.member_code)
                track = CrackTrack(
                    building_id=body.building_id,
                    label=f"AUTO-{prev_d.id}",
                    member_code=prev_d.member_code,
                    location_note=(
                        f"자동 매칭 · {member.label_ko if member else prev_d.member_code}"
                    ),
                )
                db.add(track)
                db.flush()
                prev_d.track_id = track.id
                created += 1
            curr_d.track_id = track.id
            extended += 1

    db.commit()
    return {
        "created_tracks": created,
        "linked_defects": extended,
        "skipped_needs_review": skipped,
        "total_candidates": plan["total_links"],
        "note": (
            "점수가 낮은 연결은 적용하지 않았습니다. 미리보기에서 근거를 확인한 뒤 "
            "include_review=true 로 다시 실행하면 포함됩니다."
            if skipped
            else "모든 후보를 적용했습니다."
        ),
    }


@router.delete("/auto/{building_id}")
def clear_auto(building_id: int, db: Session = Depends(get_db)) -> dict:
    """자동 생성한 추적만 지운다 — 사람이 만든 것은 남긴다."""
    tracks = db.scalars(
        select(CrackTrack).where(
            CrackTrack.building_id == building_id,
            CrackTrack.label.like("AUTO-%"),
        )
    ).all()
    n_def = 0
    for t in tracks:
        for d in db.scalars(select(Defect).where(Defect.track_id == t.id)).all():
            d.track_id = None
            n_def += 1
        db.delete(t)
    db.commit()
    return {"removed_tracks": len(tracks), "unlinked_defects": n_def}
