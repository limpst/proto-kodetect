"""사진 업로드 → 균열 검출 → 등급 판정 → 저장."""

from __future__ import annotations

import uuid
from datetime import datetime

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..domain import DefectType, Environment
from ..grading import DefectAssessment, assess_defect, assess_inspection
from ..models import Building, Defect, Inspection, Photo
from ..schemas import CrackOut, DetectionOut
from ..services.vision import CrackDetector, gsd_mm_per_px, render_overlay

router = APIRouter(prefix="/api/detect", tags=["detect"])


def _resolve_scale(
    gsd_mm_per_px_in: float | None,
    distance_m: float | None,
    gimbal_pitch_deg: float | None,
    image_width_px: int,
) -> tuple[float | None, str]:
    """픽셀 스케일 결정 — 직접 입력이 우선, 없으면 촬영거리로 산정."""
    if gsd_mm_per_px_in and gsd_mm_per_px_in > 0:
        return gsd_mm_per_px_in, "직접 입력"
    if distance_m and distance_m > 0:
        value = gsd_mm_per_px(
            distance_m=distance_m,
            focal_length_mm=settings.focal_length_mm,
            sensor_width_mm=settings.sensor_width_mm,
            image_width_px=image_width_px or settings.image_width_px,
            gimbal_pitch_deg=gimbal_pitch_deg,
        )
        return value, (
            f"촬영거리 {distance_m:.1f}m · f={settings.focal_length_mm}mm · "
            f"센서폭 {settings.sensor_width_mm}mm 기준 산정"
        )
    return None, "스케일 미확정 — 폭은 픽셀 단위로만 산출됩니다"


def _recompute_inspection(db: Session, inspection: Inspection) -> None:
    """점검 전체 결함을 다시 집계해 종합 안전등급을 갱신한다."""
    from ..domain import ConditionGrade

    defects = db.scalars(
        select(Defect).where(Defect.inspection_id == inspection.id)
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
    inspection.safety_grade = result.safety_grade.value
    inspection.defect_index = result.defect_index
    db.commit()


def _analysis_state(result, mm_per_px: float | None) -> tuple[str, str]:
    """사진의 분석 상태와 안내 문구.

    QuickGuide는 분석 완료 사진에 초록 표시를 붙인다. '정보 부족'은 스케일이
    없어 mm 환산이 불가능한 상태로, 실패와 구분해야 사용자가 무엇을 해야 할지
    안다 — 전자는 치수 측정, 후자는 재촬영이다.
    """
    if mm_per_px is None:
        return "needs_scale", (
            "스케일(GSD)이 없어 균열폭을 mm로 환산하지 못했습니다. "
            "촬영거리를 입력하거나 치수 측정을 먼저 하십시오."
        )
    if not result.quality_ok:
        return "analyzed", result.quality_note
    return "analyzed", ""


@router.post("", response_model=DetectionOut)
async def detect_image(
    file: UploadFile = File(...),
    inspection_id: int = Form(...),
    member_code: str = Form("slab"),
    source: str = Form("drone"),
    distance_m: float | None = Form(None),
    gimbal_pitch_deg: float | None = Form(None),
    gsd_mm_per_px_in: float | None = Form(None),
    sensitivity: float = Form(1.0),
    db: Session = Depends(get_db),
) -> DetectionOut:
    inspection = db.get(Inspection, inspection_id)
    if not inspection:
        raise HTTPException(404, "점검 회차를 찾을 수 없습니다")
    building = db.get(Building, inspection.building_id)
    environment = Environment(building.environment if building else "humid")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "빈 파일입니다")
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(400, "이미지를 해석할 수 없습니다")

    h, w = image.shape[:2]
    stem = f"{uuid.uuid4().hex}"
    ext = (file.filename or "upload.jpg").rsplit(".", 1)[-1].lower()
    if ext not in {"jpg", "jpeg", "png", "bmp", "webp"}:
        ext = "jpg"
    filename = f"{stem}.{ext}"
    (settings.uploads_dir / filename).write_bytes(raw)

    mm_per_px, gsd_source = _resolve_scale(
        gsd_mm_per_px_in, distance_m, gimbal_pitch_deg, w
    )

    detector = CrackDetector(sensitivity=sensitivity)
    result = detector.detect(image, mm_per_px)

    # 등급 판정
    assessments = [
        assess_defect(
            DefectType.CRACK,
            width_mm=c.width_mm_p95,
            environment=environment,
        )
        for c in result.cracks
    ]
    grades = [a.grade.value for a in assessments]

    overlay_name = f"{stem}_overlay.jpg"
    render_overlay(image, result.cracks, grades, settings.overlays_dir / overlay_name)

    state, state_note = _analysis_state(result, mm_per_px)
    photo = Photo(
        inspection_id=inspection_id,
        filename=filename,
        analysis_state=state,
        analysis_note=state_note,
        sharpness=result.sharpness,
        overlay_filename=overlay_name,
        width_px=w,
        height_px=h,
        member_code=member_code,
        source=source,
        distance_m=distance_m,
        gimbal_pitch_deg=gimbal_pitch_deg,
        gsd_mm_per_px=mm_per_px,
        captured_at=datetime.now(),
    )
    db.add(photo)
    db.flush()

    for c, a in zip(result.cracks, assessments):
        db.add(
            Defect(
                inspection_id=inspection_id,
                photo_id=photo.id,
                defect_type=DefectType.CRACK.value,
                member_code=member_code,
                width_mm=c.width_mm_p95,
                length_mm=c.length_mm,
                area_ratio=None,
                grade=a.grade.value,
                severity=a.severity,
                repair_required=a.repair_required,
                confidence=c.confidence,
                basis=a.basis,
                bbox=",".join(str(v) for v in c.bbox),
                polyline=";".join(f"{x},{y}" for x, y in c.polyline),
            )
        )
    db.commit()
    _recompute_inspection(db, inspection)

    return DetectionOut(
        photo_id=photo.id,
        filename=filename,
        overlay_url=f"/media/overlays/{overlay_name}",
        image_size=[h, w],
        mm_per_px=mm_per_px,
        gsd_source=gsd_source,
        sharpness=result.sharpness,
        quality_ok=result.quality_ok,
        quality_note=result.quality_note,
        crack_count=len(result.cracks),
        crack_area_ratio=round(result.crack_area_ratio, 6),
        cracks=[
            CrackOut(
                index=i + 1,
                bbox=list(c.bbox),
                polyline=[[int(x), int(y)] for x, y in c.polyline],
                length_px=c.length_px,
                length_mm=c.length_mm,
                width_mm_p95=c.width_mm_p95,
                width_mm_max=c.width_mm_max,
                confidence=c.confidence,
                grade=a.grade.value,
                repair_required=a.repair_required,
                basis=a.basis,
            )
            for i, (c, a) in enumerate(zip(result.cracks, assessments))
        ],
        inspection_grade=inspection.safety_grade,
        inspection_index=inspection.defect_index,
    )


@router.post("/demo", response_model=DetectionOut)
def detect_demo(
    inspection_id: int,
    seed: int = 42,
    member_code: str = "slab",
    db: Session = Depends(get_db),
) -> DetectionOut:
    """합성 표본 1장을 즉석 생성해 검출을 시연한다.

    실제 드론 사진이 없어도 파이프라인 전체(검출 -> 판정 -> 저장 -> 등급 갱신)를
    그대로 확인할 수 있다.
    """
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from datagen.synth import generate_sample  # noqa: E402

    inspection = db.get(Inspection, inspection_id)
    if not inspection:
        raise HTTPException(404, "점검 회차를 찾을 수 없습니다")
    building = db.get(Building, inspection.building_id)
    environment = Environment(building.environment if building else "humid")

    sample = generate_sample(seed, size=(768, 768))
    image = sample.image
    h, w = image.shape[:2]

    stem = uuid.uuid4().hex
    filename = f"{stem}.jpg"
    cv2.imwrite(str(settings.uploads_dir / filename), image)

    detector = CrackDetector()
    result = detector.detect(image, sample.mm_per_px)
    assessments = [
        assess_defect(DefectType.CRACK, width_mm=c.width_mm_p95, environment=environment)
        for c in result.cracks
    ]
    grades = [a.grade.value for a in assessments]
    overlay_name = f"{stem}_overlay.jpg"
    render_overlay(image, result.cracks, grades, settings.overlays_dir / overlay_name)

    state, state_note = _analysis_state(result, sample.mm_per_px)
    photo = Photo(
        inspection_id=inspection_id,
        filename=filename,
        analysis_state=state,
        analysis_note=state_note,
        sharpness=result.sharpness,
        overlay_filename=overlay_name,
        width_px=w,
        height_px=h,
        member_code=member_code,
        source="synthetic",
        distance_m=sample.meta.get("distance_m"),
        gimbal_pitch_deg=sample.meta.get("gimbal_pitch_deg"),
        gsd_mm_per_px=sample.mm_per_px,
        captured_at=datetime.now(),
    )
    db.add(photo)
    db.flush()
    for c, a in zip(result.cracks, assessments):
        db.add(
            Defect(
                inspection_id=inspection_id,
                photo_id=photo.id,
                defect_type=DefectType.CRACK.value,
                member_code=member_code,
                width_mm=c.width_mm_p95,
                length_mm=c.length_mm,
                grade=a.grade.value,
                severity=a.severity,
                repair_required=a.repair_required,
                confidence=c.confidence,
                basis=a.basis,
                bbox=",".join(str(v) for v in c.bbox),
                polyline=";".join(f"{x},{y}" for x, y in c.polyline),
            )
        )
    db.commit()
    _recompute_inspection(db, inspection)

    n_gt = sample.meta.get("n_cracks", 0)
    return DetectionOut(
        photo_id=photo.id,
        filename=filename,
        overlay_url=f"/media/overlays/{overlay_name}",
        image_size=[h, w],
        mm_per_px=sample.mm_per_px,
        gsd_source=f"합성 표본 (정답 균열 {n_gt}건, seed={seed})",
        sharpness=result.sharpness,
        quality_ok=result.quality_ok,
        quality_note=result.quality_note,
        crack_count=len(result.cracks),
        crack_area_ratio=round(result.crack_area_ratio, 6),
        cracks=[
            CrackOut(
                index=i + 1,
                bbox=list(c.bbox),
                polyline=[[int(x), int(y)] for x, y in c.polyline],
                length_px=c.length_px,
                length_mm=c.length_mm,
                width_mm_p95=c.width_mm_p95,
                width_mm_max=c.width_mm_max,
                confidence=c.confidence,
                grade=a.grade.value,
                repair_required=a.repair_required,
                basis=a.basis,
            )
            for i, (c, a) in enumerate(zip(result.cracks, assessments))
        ],
        inspection_grade=inspection.safety_grade,
        inspection_index=inspection.defect_index,
    )
