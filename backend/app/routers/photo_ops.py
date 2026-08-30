"""사진 상세 · 치수 측정 · 원근 보정 · 재분석.

QuickGuide / 테스트 실행계획서 18항 "분석 결과 검토·보정" 을 구현한다.

  - '정보 부족' 사진은 [치수 측정]으로 길이 기준을 잡아야 분석된다
  - 비뚤게 찍힌 사진은 [사진 보정](4점 원근) 후 [다시 사진 분석하기]
  - AI가 놓친 균열은 [새 손상]으로 직접 그려 넣는다 (workspace 라우터)

제품 문서는 "재분석하면 직접 고친 손상은 사라진다"고 경고한다. 여기서는
`Defect.source` 로 자동/수동을 구분해 **수동 입력분을 보존**한다. 사람이 들인
노동을 기계가 지우는 동작은 그 자체로 결함이다.
"""

from __future__ import annotations

import uuid

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..domain import DEFECT_LABELS_KO, DefectType, Environment
from ..grading import assess_defect
from ..models import Building, Defect, Inspection, Photo
from ..services.photo_edit import map_points, rectify_quad, scale_from_reference
from ..services.vision import CrackDetector, render_overlay
from .detect import _analysis_state, _recompute_inspection

router = APIRouter(prefix="/api/photos", tags=["photo-ops"])


# ─── 요청 스키마 ───────────────────────────────────────────────
class ScaleIn(BaseModel):
    """치수 측정 — 사진 위 두 점과 그 사이의 실제 길이."""

    p1: list[float] = Field(..., min_length=2, max_length=2)
    p2: list[float] = Field(..., min_length=2, max_length=2)
    real_mm: float = Field(..., gt=0)
    reanalyze: bool = True


class RectifyIn(BaseModel):
    """4점 원근 보정 — 직사각형인 것을 아는 영역의 네 모서리."""

    quad: list[list[float]] = Field(..., min_length=4, max_length=4)
    real_width_mm: float | None = None
    real_height_mm: float | None = None
    reanalyze: bool = True


class ReanalyzeIn(BaseModel):
    sensitivity: float = 1.0


# ─── 공통 ─────────────────────────────────────────────────────
def _photo_or_404(db: Session, photo_id: int) -> Photo:
    p = db.get(Photo, photo_id)
    if not p:
        raise HTTPException(404, "사진을 찾을 수 없습니다")
    return p


def _source_path(p: Photo):
    """분석에 쓸 이미지 — 보정본이 있으면 그것이 기준이다."""
    name = p.rectified_filename or p.filename
    path = settings.uploads_dir / name
    if not path.exists():
        raise HTTPException(410, "원본 파일이 없습니다. 다시 업로드하십시오.")
    return path


def _environment(db: Session, p: Photo) -> Environment:
    insp = db.get(Inspection, p.inspection_id)
    b = db.get(Building, insp.building_id) if insp else None
    return Environment(b.environment) if b else Environment.HUMID


def _run_detection(db: Session, p: Photo, sensitivity: float = 1.0) -> dict:
    """자동 결함만 지우고 다시 검출한다. 수동 입력분은 건드리지 않는다."""
    image = cv2.imread(str(_source_path(p)))
    if image is None:
        raise HTTPException(400, "이미지를 읽을 수 없습니다")

    removed = 0
    for d in db.scalars(
        select(Defect).where(Defect.photo_id == p.id, Defect.source == "ai")
    ).all():
        db.delete(d)
        removed += 1

    env = _environment(db, p)
    result = CrackDetector(sensitivity=sensitivity).detect(image, p.gsd_mm_per_px)
    assessments = [
        assess_defect(DefectType.CRACK, width_mm=c.width_mm_p95, environment=env)
        for c in result.cracks
    ]

    overlay_name = f"{uuid.uuid4().hex}_overlay.jpg"
    render_overlay(
        image,
        result.cracks,
        [a.grade.value for a in assessments],
        settings.overlays_dir / overlay_name,
    )
    p.overlay_filename = overlay_name
    p.width_px, p.height_px = image.shape[1], image.shape[0]
    p.sharpness = result.sharpness
    p.analysis_state, p.analysis_note = _analysis_state(result, p.gsd_mm_per_px)

    for c, a in zip(result.cracks, assessments):
        db.add(
            Defect(
                inspection_id=p.inspection_id,
                photo_id=p.id,
                defect_type=DefectType.CRACK.value,
                member_code=p.member_code,
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

    insp = db.get(Inspection, p.inspection_id)
    _recompute_inspection(db, insp)
    kept = db.scalar(
        select(func.count(Defect.id)).where(
            Defect.photo_id == p.id, Defect.source == "manual"
        )
    ) or 0
    return {
        "detected": len(result.cracks),
        "removed_auto": removed,
        "manual_preserved": int(kept),
        "sharpness": result.sharpness,
        "quality_ok": result.quality_ok,
        "quality_note": result.quality_note,
        "analysis_state": p.analysis_state,
        "inspection_grade": insp.safety_grade if insp else None,
    }


def _photo_payload(db: Session, p: Photo) -> dict:
    defects = db.scalars(
        select(Defect).where(Defect.photo_id == p.id).order_by(Defect.severity.desc())
    ).all()
    return {
        "id": p.id,
        "inspection_id": p.inspection_id,
        "filename": p.filename,
        "image_url": f"/media/uploads/{p.rectified_filename or p.filename}",
        "original_url": f"/media/uploads/{p.filename}",
        "overlay_url": f"/media/overlays/{p.overlay_filename}" if p.overlay_filename else None,
        "rectified": bool(p.rectified_filename),
        "size": [p.width_px, p.height_px],
        "member_code": p.member_code,
        "group_id": p.group_id,
        "mm_per_px": p.gsd_mm_per_px,
        "sharpness": p.sharpness,
        "analysis_state": p.analysis_state,
        "analysis_note": p.analysis_note,
        "defects": [
            {
                "id": d.id,
                "type": d.defect_type,
                "type_label": DEFECT_LABELS_KO.get(DefectType(d.defect_type), d.defect_type),
                "grade": d.grade,
                "width_mm": d.width_mm,
                "length_mm": d.length_mm,
                "confidence": d.confidence,
                "repair_required": d.repair_required,
                "source": d.source,
                "basis": d.basis,
                "bbox": [int(v) for v in d.bbox.split(",")] if d.bbox else [],
                "polyline": [
                    [int(float(v)) for v in seg.split(",")]
                    for seg in d.polyline.split(";")
                    if seg
                ],
            }
            for d in defects
        ],
    }


# ─── 엔드포인트 ───────────────────────────────────────────────
@router.get("/{photo_id}")
def photo_detail(photo_id: int, db: Session = Depends(get_db)) -> dict:
    """사진 상세 — 이미지·스케일·상태와 결함 목록(자동/수동 구분)."""
    return _photo_payload(db, _photo_or_404(db, photo_id))


@router.post("/{photo_id}/scale")
def set_scale(photo_id: int, body: ScaleIn, db: Session = Depends(get_db)) -> dict:
    """치수 측정 — 기준물 두 점으로 스케일을 확정한다.

    '정보 부족' 사진을 되살리는 유일한 수단이다. 스케일이 정해지면 기존 결함의
    길이·폭도 함께 환산해야 하므로 기본으로 재분석한다.
    """
    p = _photo_or_404(db, photo_id)
    try:
        res = scale_from_reference(tuple(body.p1), tuple(body.p2), body.real_mm)
    except ValueError as e:
        raise HTTPException(400, str(e))

    p.gsd_mm_per_px = res.mm_per_px
    db.commit()

    out = {
        "mm_per_px": res.mm_per_px,
        "pixel_distance": res.pixel_distance,
        "real_mm": res.real_mm,
        "note": res.note,
    }
    if body.reanalyze:
        out["reanalysis"] = _run_detection(db, p)
    return out


@router.post("/{photo_id}/rectify")
def rectify(photo_id: int, body: RectifyIn, db: Session = Depends(get_db)) -> dict:
    """4점 원근 보정 — 비스듬히 찍힌 면을 정면 시점으로 편다."""
    p = _photo_or_404(db, photo_id)
    image = cv2.imread(str(settings.uploads_dir / p.filename))
    if image is None:
        raise HTTPException(400, "원본 이미지를 읽을 수 없습니다")

    try:
        res = rectify_quad(
            image,
            [tuple(q) for q in body.quad],
            real_width_mm=body.real_width_mm,
            real_height_mm=body.real_height_mm,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    name = f"{uuid.uuid4().hex}_rect.jpg"
    cv2.imwrite(str(settings.uploads_dir / name), res.image,
                [int(cv2.IMWRITE_JPEG_QUALITY), 95])

    # 보정 전에 사람이 그려 둔 손상은 좌표를 옮겨 살린다
    moved = 0
    for d in db.scalars(
        select(Defect).where(Defect.photo_id == p.id, Defect.source == "manual")
    ).all():
        pts = [
            (float(v.split(",")[0]), float(v.split(",")[1]))
            for v in d.polyline.split(";")
            if v
        ]
        if not pts:
            continue
        mapped = map_points([tuple(q) for q in body.quad], res.out_size, pts)
        d.polyline = ";".join(f"{int(x)},{int(y)}" for x, y in mapped)
        moved += 1

    p.rectified_filename = name
    p.width_px, p.height_px = res.out_size
    if res.mm_per_px:
        p.gsd_mm_per_px = res.mm_per_px
    db.commit()

    out = {
        "rectified_url": f"/media/uploads/{name}",
        "size": list(res.out_size),
        "mm_per_px": res.mm_per_px,
        "manual_defects_moved": moved,
        "note": res.note,
    }
    if body.reanalyze:
        out["reanalysis"] = _run_detection(db, p)
    return out


@router.post("/{photo_id}/reanalyze")
def reanalyze(photo_id: int, body: ReanalyzeIn, db: Session = Depends(get_db)) -> dict:
    """다시 사진 분석하기 — 자동 결함만 새로 뽑고 수동 입력분은 보존한다."""
    p = _photo_or_404(db, photo_id)
    return _run_detection(db, p, body.sensitivity)


@router.delete("/{photo_id}/rectify")
def undo_rectify(photo_id: int, db: Session = Depends(get_db)) -> dict:
    """보정 취소 — 원본으로 되돌린다."""
    p = _photo_or_404(db, photo_id)
    if not p.rectified_filename:
        raise HTTPException(400, "보정된 사진이 아닙니다")
    p.rectified_filename = None
    img = cv2.imread(str(settings.uploads_dir / p.filename))
    if img is not None:
        p.width_px, p.height_px = img.shape[1], img.shape[0]
    db.commit()
    return {"ok": True, "reanalysis": _run_detection(db, p)}
