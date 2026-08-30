"""이미지 자동 정합 API.

사진 그룹의 사진들을 한 장의 정사영상으로 합치고, 그 위에서 결함을 다시
검출한다. 타일별로 따로 검출하면 겹친 영역의 같은 균열이 여러 번 세지는데,
정합 후 통합 검출은 그 중복을 원천적으로 없앤다.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..domain import DefectType, Environment
from ..grading import assess_defect
from ..models import Building, Defect, Inspection, Photo, PhotoGroup
from ..services.stitching import Stitcher, map_point
from ..services.vision import CrackDetector, render_overlay
from .detect import _recompute_inspection

router = APIRouter(prefix="/api/stitch", tags=["stitch"])

# 정합은 무겁다. 한 번에 다루는 장수를 제한해 요청이 몇 분씩 걸리는 것을 막는다.
MAX_IMAGES = 40


class StitchRequest(BaseModel):
    group_id: int
    ordered: bool = True          # 촬영 순서를 신뢰해 이웃만 매칭
    detect_on_panorama: bool = True
    replace_defects: bool = False  # 기존 AI 결함을 파노라마 결과로 교체


@router.post("/group")
def stitch_group(body: StitchRequest, db: Session = Depends(get_db)) -> dict:
    """사진 그룹을 정합해 파노라마를 만들고, 선택 시 통합 검출까지 수행한다."""
    group = db.get(PhotoGroup, body.group_id)
    if not group:
        raise HTTPException(404, "사진 그룹을 찾을 수 없습니다")

    photos = db.scalars(
        select(Photo).where(Photo.group_id == group.id).order_by(Photo.id)
    ).all()
    if len(photos) < 2:
        raise HTTPException(
            400, f"정합하려면 사진이 2장 이상 필요합니다 (현재 {len(photos)}장)"
        )
    if len(photos) > MAX_IMAGES:
        raise HTTPException(
            400,
            f"한 번에 {MAX_IMAGES}장까지 정합합니다 (현재 {len(photos)}장). "
            "그룹을 나누어 실행하십시오.",
        )

    images: list[np.ndarray] = []
    used: list[Photo] = []
    missing: list[str] = []
    for p in photos:
        f = settings.uploads_dir / p.filename
        img = cv2.imread(str(f)) if f.exists() else None
        if img is None:
            missing.append(p.filename)
            continue
        images.append(img)
        used.append(p)

    if len(images) < 2:
        raise HTTPException(400, "읽을 수 있는 사진이 2장 미만입니다")

    result = Stitcher().stitch(
        images, mm_per_px=[p.gsd_mm_per_px for p in used], ordered=body.ordered
    )
    if result.panorama is None:
        return {
            "ok": False,
            "group": {"id": group.id, "name": group.name},
            "placed": 0,
            "dropped": [p.filename for p in used],
            "warnings": result.warnings,
            "elapsed_sec": result.elapsed_sec,
        }

    stem = uuid.uuid4().hex
    pano_name = f"pano_{stem}.jpg"
    cv2.imwrite(
        str(settings.uploads_dir / pano_name),
        result.panorama,
        [int(cv2.IMWRITE_JPEG_QUALITY), 92],
    )

    payload: dict = {
        "ok": True,
        "group": {"id": group.id, "name": group.name},
        "panorama_url": f"/media/uploads/{pano_name}",
        "canvas": list(result.canvas_size),
        "mm_per_px": result.mm_per_px,
        "placed": len(result.placed),
        "total": len(used),
        "coverage": result.coverage,
        "dropped": [used[i].filename for i in result.dropped],
        "scale_drift": result.scale_drift,
        "pairs": len(result.pairs),
        "mean_confidence": (
            round(float(np.mean([p.confidence for p in result.pairs])), 3)
            if result.pairs else 0.0
        ),
        "elapsed_sec": result.elapsed_sec,
        "warnings": result.warnings + (
            [f"읽지 못한 사진 {len(missing)}장: {', '.join(missing[:3])}"]
            if missing else []
        ),
        # 정합된 사진이 파노라마 어디에 놓였는지 — 화면이 배치를 그린다
        "placements": [
            {
                "photo_id": used[i].id,
                "filename": used[i].filename,
                "corners": [
                    list(map_point(result.transforms[i], x, y))
                    for x, y in (
                        (0, 0),
                        (images[i].shape[1], 0),
                        (images[i].shape[1], images[i].shape[0]),
                        (0, images[i].shape[0]),
                    )
                ],
            }
            for i in result.placed
        ],
    }

    # 타일별로 이미 검출된 AI 결함 수. 정합 통합 검출과 비교해야
    # 중복 계상이 얼마나 줄었는지 화면이 보여줄 수 있다.
    payload["tile_defect_count"] = db.scalar(
        select(func.count())
        .select_from(Defect)
        .where(Defect.photo_id.in_([p.id for p in used]), Defect.source == "ai")
    ) or 0

    if not body.detect_on_panorama:
        return payload

    # ─── 파노라마 통합 검출 ────────────────────────────────────
    insp = db.get(Inspection, group.inspection_id)
    b = db.get(Building, insp.building_id)
    env = Environment(b.environment if b else "humid")

    det = CrackDetector()
    dr = det.detect(result.panorama, result.mm_per_px)
    assessments = [
        assess_defect(DefectType.CRACK, width_mm=c.width_mm_p95, environment=env)
        for c in dr.cracks
    ]
    overlay_name = f"pano_{stem}_overlay.jpg"
    render_overlay(
        result.panorama,
        dr.cracks,
        [a.grade.value for a in assessments],
        settings.overlays_dir / overlay_name,
    )

    # 파노라마를 하나의 Photo 로 등록한다. 원본 사진들은 그대로 두어
    # 근접 확인이 가능하게 하고, 파노라마는 통합 판정의 근거로 쓴다.
    pano_photo = Photo(
        inspection_id=group.inspection_id,
        group_id=group.id,
        filename=pano_name,
        overlay_filename=overlay_name,
        analysis_state="analyzed" if result.mm_per_px else "needs_scale",
        analysis_note=(
            "" if result.mm_per_px
            else "원본 사진에 스케일이 없어 파노라마 GSD를 산출하지 못했습니다."
        ),
        sharpness=dr.sharpness,
        width_px=result.canvas_size[0],
        height_px=result.canvas_size[1],
        member_code=group.member_code,
        source="panorama",
        gsd_mm_per_px=result.mm_per_px,
        captured_at=datetime.now(),
    )
    db.add(pano_photo)
    db.flush()

    if body.replace_defects:
        # 원본 사진들의 AI 결함을 지운다. 사람이 직접 그린 것은 남긴다 —
        # 자동 처리가 사람의 입력을 지우면 안 된다.
        old = db.scalars(
            select(Defect).where(
                Defect.photo_id.in_([p.id for p in used]), Defect.source == "ai"
            )
        ).all()
        for d in old:
            db.delete(d)
        payload["replaced_defects"] = len(old)

    for c, a in zip(dr.cracks, assessments):
        db.add(
            Defect(
                inspection_id=group.inspection_id,
                photo_id=pano_photo.id,
                defect_type=DefectType.CRACK.value,
                member_code=group.member_code,
                width_mm=c.width_mm_p95,
                length_mm=c.length_mm,
                grade=a.grade.value,
                severity=a.severity,
                repair_required=a.repair_required,
                confidence=c.confidence,
                basis=a.basis,
                bbox=",".join(str(v) for v in c.bbox),
                polyline=";".join(f"{x},{y}" for x, y in c.polyline),
                source="ai",
            )
        )
    db.commit()
    _recompute_inspection(db, insp)

    payload.update(
        {
            "photo_id": pano_photo.id,
            "overlay_url": f"/media/overlays/{overlay_name}",
            "crack_count": len(dr.cracks),
            "sharpness": dr.sharpness,
            "quality_ok": dr.quality_ok,
            "quality_note": dr.quality_note,
            "inspection_grade": insp.safety_grade,
            "cracks": [
                {
                    "index": i + 1,
                    "bbox": list(c.bbox),
                    "polyline": [list(p) for p in c.polyline],
                    "width_mm_p95": c.width_mm_p95,
                    "length_mm": c.length_mm,
                    "confidence": c.confidence,
                    "grade": a.grade.value,
                    "repair_required": a.repair_required,
                    "basis": a.basis,
                }
                for i, (c, a) in enumerate(zip(dr.cracks, assessments))
            ],
            "note": (
                "타일별로 따로 검출하면 겹친 영역의 같은 균열이 중복 계상됩니다. "
                "파노라마 통합 검출은 그 중복을 없앱니다."
            ),
        }
    )
    return payload


@router.get("/estimate")
def estimate(group_id: int, db: Session = Depends(get_db)) -> dict:
    """정합 가능성 사전 점검 — 실행 전에 무엇이 걸리는지 알려준다."""
    group = db.get(PhotoGroup, group_id)
    if not group:
        raise HTTPException(404, "사진 그룹을 찾을 수 없습니다")
    photos = db.scalars(select(Photo).where(Photo.group_id == group.id)).all()

    no_scale = [p.filename for p in photos if p.gsd_mm_per_px is None]
    pano = [p.filename for p in photos if p.source == "panorama"]

    blockers: list[str] = []
    if len(photos) < 2:
        blockers.append(f"사진이 {len(photos)}장입니다. 정합에는 2장 이상이 필요합니다.")
    if len(photos) > MAX_IMAGES:
        blockers.append(f"{len(photos)}장은 한 번에 처리할 수 없습니다 (최대 {MAX_IMAGES}장).")

    notes: list[str] = []
    if no_scale:
        notes.append(
            f"스케일(GSD) 없는 사진 {len(no_scale)}장 — 파노라마 mm 환산이 "
            "불가능할 수 있습니다."
        )
    if pano:
        notes.append(
            f"이미 파노라마 {len(pano)}장이 그룹에 있습니다. 재정합하면 중복됩니다."
        )
    # 대략적 소요 — 실측 기준 장당 약 4초 (특징 추출 + 매칭 + 합성)
    notes.append(f"예상 소요 약 {max(5, len(photos) * 4)}초")

    return {
        "group": {"id": group.id, "name": group.name},
        "photo_count": len(photos),
        "can_stitch": not blockers,
        "blockers": blockers,
        "notes": notes,
    }
