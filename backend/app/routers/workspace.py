"""사진 그룹 · 도면 · 위치 · 보고서 산출물 API.

KO-Detect Compact QuickGuide의 STEP 03~05를 그대로 구현한다.

    STEP 03  사진 등록 → 사진 그룹 → AI 분석
    STEP 04  도면 추가 → 위치 만들기 → 사진 연결 · 메모
    STEP 05  보고서 범위 선택 → 결과 파일 선택 → ZIP 다운로드
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..domain import DEFECT_LABELS_KO, MEMBER_CLASSES, DefectType
from ..models import Building, Defect, Drawing, Inspection, Photo, PhotoGroup, Spot
from ..services.deliverables import (
    DefectRow,
    ReportBundle,
    build_bundle_zip,
)

router = APIRouter(prefix="/api", tags=["workspace"])

DRAWING_EXTS = {"pdf", "dxf", "jpg", "jpeg", "png"}


# ─── 스키마 ────────────────────────────────────────────────────
class GroupIn(BaseModel):
    inspection_id: int
    name: str
    member_code: str = "slab"
    note: str = ""


class GroupAssignIn(BaseModel):
    photo_ids: list[int]
    group_id: int | None = None      # None 이면 미분류로 되돌린다


class DrawingIn(BaseModel):
    building_id: int
    name: str
    width_px: int = 1600
    height_px: int = 1200
    mm_per_px: float | None = None


class SpotIn(BaseModel):
    drawing_id: int
    x: float
    y: float
    group_id: int | None = None
    member_code: str = "slab"
    direction: str = ""
    note: str = ""


class SpotUpdateIn(BaseModel):
    group_id: int | None = None
    member_code: str | None = None
    direction: str | None = None
    note: str | None = None
    x: float | None = None
    y: float | None = None


class ManualDefectIn(BaseModel):
    """사용자가 직접 그린 손상 — QuickGuide '새 손상' 기능."""

    photo_id: int
    defect_type: str = "crack"
    member_code: str | None = None
    polyline: list[list[float]] = []      # [[x, y], ...] 픽셀 좌표
    width_mm: float | None = None
    note: str = ""






class ReportRequest(BaseModel):
    inspection_id: int
    scope: str = "drawing"                 # drawing | group
    drawing_ids: list[int] = []
    group_ids: list[int] = []
    kinds: list[str] = ["photo_sheet", "quantity", "survey_dxf"]


# ─── 사진 그룹 (STEP 03) ───────────────────────────────────────
@router.get("/groups")
def list_groups(inspection_id: int, db: Session = Depends(get_db)) -> list[dict]:
    groups = db.scalars(
        select(PhotoGroup)
        .where(PhotoGroup.inspection_id == inspection_id)
        .order_by(PhotoGroup.id)
    ).all()

    out = []
    for g in groups:
        n_photo = db.scalar(
            select(func.count(Photo.id)).where(Photo.group_id == g.id)
        )
        n_analyzed = db.scalar(
            select(func.count(Photo.id)).where(
                Photo.group_id == g.id, Photo.analysis_state == "analyzed"
            )
        )
        n_defect = db.scalar(
            select(func.count(Defect.id))
            .join(Photo, Defect.photo_id == Photo.id)
            .where(Photo.group_id == g.id)
        )
        member = MEMBER_CLASSES.get(g.member_code)
        out.append(
            {
                "id": g.id,
                "name": g.name,
                "member_code": g.member_code,
                "member_label": member.label_ko if member else g.member_code,
                "note": g.note,
                "photo_count": int(n_photo or 0),
                "analyzed_count": int(n_analyzed or 0),
                "defect_count": int(n_defect or 0),
            }
        )

    # 미분류 — 그룹에 속하지 않은 사진. 사용자가 놓치지 않도록 항상 내보낸다.
    n_un = db.scalar(
        select(func.count(Photo.id)).where(
            Photo.inspection_id == inspection_id, Photo.group_id.is_(None)
        )
    )
    out.append(
        {
            "id": None,
            "name": "미분류",
            "member_code": "",
            "member_label": "—",
            "note": "그룹에 속하지 않은 사진입니다. 그룹으로 옮기면 보고서가 정리됩니다.",
            "photo_count": int(n_un or 0),
            "analyzed_count": 0,
            "defect_count": 0,
        }
    )
    return out


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, db: Session = Depends(get_db)) -> dict:
    if not db.get(Inspection, body.inspection_id):
        raise HTTPException(404, "점검 회차를 찾을 수 없습니다")
    g = PhotoGroup(**body.model_dump())
    db.add(g)
    db.commit()
    db.refresh(g)
    return {"id": g.id, "name": g.name}


@router.post("/groups/assign")
def assign_photos(body: GroupAssignIn, db: Session = Depends(get_db)) -> dict:
    if body.group_id is not None and not db.get(PhotoGroup, body.group_id):
        raise HTTPException(404, "사진 그룹을 찾을 수 없습니다")
    photos = db.scalars(select(Photo).where(Photo.id.in_(body.photo_ids))).all()
    for p in photos:
        p.group_id = body.group_id
    db.commit()
    return {"moved": len(photos), "group_id": body.group_id}


@router.delete("/groups/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db)) -> dict:
    g = db.get(PhotoGroup, group_id)
    if not g:
        raise HTTPException(404, "사진 그룹을 찾을 수 없습니다")
    # 사진은 지우지 않는다 — 미분류로 되돌린다.
    # 그룹 삭제로 현장 사진이 사라지면 되돌릴 방법이 없다.
    for p in db.scalars(select(Photo).where(Photo.group_id == group_id)).all():
        p.group_id = None
    db.delete(g)
    db.commit()
    return {"ok": True, "photos_unassigned": True}


@router.get("/photos")
def list_photos(
    inspection_id: int,
    group_id: int | None = None,
    unassigned: bool = False,
    db: Session = Depends(get_db),
) -> list[dict]:
    stmt = select(Photo).where(Photo.inspection_id == inspection_id)
    if unassigned:
        stmt = stmt.where(Photo.group_id.is_(None))
    elif group_id is not None:
        stmt = stmt.where(Photo.group_id == group_id)

    out = []
    for p in db.scalars(stmt.order_by(Photo.id)).all():
        n_def = db.scalar(select(func.count(Defect.id)).where(Defect.photo_id == p.id))
        out.append(
            {
                "id": p.id,
                "filename": p.filename,
                "url": f"/media/uploads/{p.filename}",
                # 보정본이 있으면 그것이 현재 기준 이미지다. 목록에서 원본을 보여주면
                # 사용자는 보정이 안 된 줄 안다.
                "image_url": f"/media/uploads/{p.rectified_filename or p.filename}",
                "rectified": bool(p.rectified_filename),
                "overlay_url": (
                    f"/media/overlays/{p.overlay_filename}" if p.overlay_filename else None
                ),
                "group_id": p.group_id,
                "member_code": p.member_code,
                "analysis_state": p.analysis_state,
                "analysis_note": p.analysis_note,
                "sharpness": p.sharpness,
                "gsd_mm_per_px": p.gsd_mm_per_px,
                "size": [p.width_px, p.height_px],
                "defect_count": int(n_def or 0),
            }
        )
    return out


# ─── 도면 · 위치 (STEP 04) ─────────────────────────────────────
@router.get("/drawings")
def list_drawings(building_id: int, db: Session = Depends(get_db)) -> list[dict]:
    out = []
    for d in db.scalars(
        select(Drawing).where(Drawing.building_id == building_id).order_by(Drawing.id)
    ).all():
        spots = db.scalars(select(Spot).where(Spot.drawing_id == d.id)).all()
        n_photo = 0
        n_defect = 0
        for s in spots:
            if s.group_id:
                n_photo += int(
                    db.scalar(select(func.count(Photo.id)).where(Photo.group_id == s.group_id)) or 0
                )
                n_defect += int(
                    db.scalar(
                        select(func.count(Defect.id))
                        .join(Photo, Defect.photo_id == Photo.id)
                        .where(Photo.group_id == s.group_id)
                    ) or 0
                )
        out.append(
            {
                "id": d.id,
                "name": d.name,
                "file_kind": d.file_kind,
                "url": f"/media/uploads/{d.filename}" if d.filename else None,
                "size": [d.width_px, d.height_px],
                "mm_per_px": d.mm_per_px,
                "spot_count": len(spots),
                "photo_count": n_photo,
                "defect_count": n_defect,
            }
        )
    return out


@router.post("/drawings", status_code=201)
async def create_drawing(
    building_id: int = Form(...),
    name: str = Form(...),
    width_px: int = Form(1600),
    height_px: int = Form(1200),
    mm_per_px: float | None = Form(None),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
) -> dict:
    """도면 등록. 파일 없이 '빈 화면으로 시작'할 수 있다.

    현장에서는 도면을 나중에 받는 경우가 흔하다. 위치부터 찍어 두고 파일을
    나중에 교체할 수 있어야 실무 흐름이 끊기지 않는다.
    """
    if not db.get(Building, building_id):
        raise HTTPException(404, "건축물을 찾을 수 없습니다")

    filename = None
    kind = "blank"
    if file is not None and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext not in DRAWING_EXTS:
            raise HTTPException(
                400, f"지원하지 않는 도면 형식입니다: .{ext} (PDF·DXF·JPG·PNG)"
            )
        raw = await file.read()
        filename = f"dwg_{uuid.uuid4().hex}.{ext}"
        (settings.uploads_dir / filename).write_bytes(raw)
        kind = "jpg" if ext == "jpeg" else ext

        # 래스터 도면이면 실제 크기를 읽어 좌표계를 맞춘다
        if kind in ("jpg", "png"):
            import cv2
            import numpy as np

            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                height_px, width_px = img.shape[:2]

    d = Drawing(
        building_id=building_id,
        name=name,
        filename=filename,
        file_kind=kind,
        width_px=width_px,
        height_px=height_px,
        mm_per_px=mm_per_px,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return {"id": d.id, "name": d.name, "file_kind": d.file_kind,
            "size": [d.width_px, d.height_px]}


@router.get("/drawings/{drawing_id}/spots")
def list_spots(drawing_id: int, db: Session = Depends(get_db)) -> list[dict]:
    out = []
    for s in db.scalars(
        select(Spot).where(Spot.drawing_id == drawing_id).order_by(Spot.number)
    ).all():
        group = db.get(PhotoGroup, s.group_id) if s.group_id else None
        n_photo = (
            int(db.scalar(select(func.count(Photo.id)).where(Photo.group_id == s.group_id)) or 0)
            if s.group_id else 0
        )
        n_defect = (
            int(db.scalar(
                select(func.count(Defect.id))
                .join(Photo, Defect.photo_id == Photo.id)
                .where(Photo.group_id == s.group_id)
            ) or 0)
            if s.group_id else 0
        )
        member = MEMBER_CLASSES.get(s.member_code)
        out.append(
            {
                "id": s.id,
                "number": s.number,
                "x": s.x,
                "y": s.y,
                "group_id": s.group_id,
                "group_name": group.name if group else None,
                "member_code": s.member_code,
                "member_label": member.label_ko if member else s.member_code,
                "direction": s.direction,
                "note": s.note,
                "photo_count": n_photo,
                "defect_count": n_defect,
            }
        )
    return out


@router.post("/spots", status_code=201)
def create_spot(body: SpotIn, db: Session = Depends(get_db)) -> dict:
    if not db.get(Drawing, body.drawing_id):
        raise HTTPException(404, "도면을 찾을 수 없습니다")
    n = db.scalar(
        select(func.count(Spot.id)).where(Spot.drawing_id == body.drawing_id)
    )
    s = Spot(**body.model_dump(), number=int(n or 0) + 1)
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "number": s.number}


@router.patch("/spots/{spot_id}")
def update_spot(spot_id: int, body: SpotUpdateIn, db: Session = Depends(get_db)) -> dict:
    s = db.get(Spot, spot_id)
    if not s:
        raise HTTPException(404, "위치를 찾을 수 없습니다")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    db.commit()
    return {"ok": True, "id": s.id}


@router.delete("/spots/{spot_id}")
def delete_spot(spot_id: int, db: Session = Depends(get_db)) -> dict:
    s = db.get(Spot, spot_id)
    if not s:
        raise HTTPException(404, "위치를 찾을 수 없습니다")
    db.delete(s)
    db.commit()
    return {"ok": True}


# ─── 직접 손상 입력 (AI가 놓친 균열) ───────────────────────────
@router.post("/defects/manual", status_code=201)
def add_manual_defect(body: ManualDefectIn, db: Session = Depends(get_db)) -> dict:
    """사용자가 직접 그린 손상을 등록한다.

    QuickGuide: "AI가 놓친 균열은 [새 손상] → 균열로 직접 그려 넣습니다."
    `source='manual'` 로 표시해 재분석 시 보존한다 — 제품 문서가 경고하는
    "재분석하면 직접 고친 손상은 사라진다" 문제를 여기서 막는다.
    """
    photo = db.get(Photo, body.photo_id)
    if not photo:
        raise HTTPException(404, "사진을 찾을 수 없습니다")
    try:
        dtype = DefectType(body.defect_type)
    except ValueError:
        raise HTTPException(400, f"알 수 없는 결함 유형입니다: {body.defect_type}")

    # 폴리라인 길이 → 실치수 환산
    length_mm = None
    if len(body.polyline) >= 2 and photo.gsd_mm_per_px:
        total_px = sum(
            ((body.polyline[i + 1][0] - body.polyline[i][0]) ** 2
             + (body.polyline[i + 1][1] - body.polyline[i][1]) ** 2) ** 0.5
            for i in range(len(body.polyline) - 1)
        )
        length_mm = round(total_px * photo.gsd_mm_per_px, 1)

    from ..domain import Environment
    from ..grading import assess_defect

    insp = db.get(Inspection, photo.inspection_id)
    b = db.get(Building, insp.building_id) if insp else None
    env = Environment(b.environment) if b else Environment.HUMID
    a = assess_defect(dtype, width_mm=body.width_mm, environment=env)

    d = Defect(
        inspection_id=photo.inspection_id,
        photo_id=photo.id,
        defect_type=dtype.value,
        member_code=body.member_code or photo.member_code,
        width_mm=body.width_mm,
        length_mm=length_mm,
        grade=a.grade.value,
        severity=a.severity,
        repair_required=a.repair_required,
        confidence=1.0,                    # 사람이 그린 것 — 불확실성 없음
        basis=(a.basis + (" · " + body.note if body.note else "")),
        polyline=";".join(f"{int(x)},{int(y)}" for x, y in body.polyline),
        source="manual",
    )
    db.add(d)
    db.commit()
    db.refresh(d)

    from .detect import _recompute_inspection

    _recompute_inspection(db, insp)
    return {
        "id": d.id,
        "grade": d.grade,
        "length_mm": length_mm,
        "source": "manual",
        "note": (
            "직접 입력한 손상은 재분석해도 보존됩니다."
            if photo.gsd_mm_per_px
            else "스케일이 없어 길이를 산출하지 못했습니다. 치수 측정을 먼저 하십시오."
        ),
    }


# ─── 보고서 산출물 (STEP 05) ───────────────────────────────────
def _collect_rows(
    db: Session, inspection: Inspection, group_ids: set[int] | None
) -> list[DefectRow]:
    stmt = select(Defect).where(Defect.inspection_id == inspection.id)
    defects = db.scalars(stmt.order_by(Defect.severity.desc())).all()

    # 위치 라벨 — 그룹이 어느 도면 어느 핀에 붙어 있는지
    spot_label: dict[int, str] = {}
    for s in db.scalars(select(Spot)).all():
        if s.group_id:
            member = MEMBER_CLASSES.get(s.member_code)
            parts = [f"{s.number}번"]
            if member:
                parts.append(member.label_ko)
            if s.direction:
                parts.append(s.direction)
            spot_label[s.group_id] = " · ".join(parts)

    rows: list[DefectRow] = []
    serial: dict[str, int] = {}
    for d in defects:
        photo = db.get(Photo, d.photo_id) if d.photo_id else None
        gid = photo.group_id if photo else None
        if group_ids is not None and gid not in group_ids:
            continue
        group = db.get(PhotoGroup, gid) if gid else None

        key = f"{d.member_code}-{d.defect_type}"
        serial[key] = serial.get(key, 0) + 1
        from .. import bhc

        system = bhc.MEMBER_TO_SYSTEM.get(d.member_code, bhc.System.S1)
        did = bhc.defect_id(system, d.member_code, DefectType(d.defect_type), serial[key])

        overlay = (
            settings.overlays_dir / photo.overlay_filename
            if photo and photo.overlay_filename
            else None
        )
        rows.append(
            DefectRow(
                defect_id=did,
                defect_type=DefectType(d.defect_type),
                member_code=d.member_code,
                group_name=group.name if group else "미분류",
                spot_label=spot_label.get(gid, "") if gid else "",
                grade=d.grade,
                width_mm=d.width_mm,
                length_mm=d.length_mm,
                area_ratio=d.area_ratio,
                photo_name=photo.filename if photo else "—",
                overlay_path=overlay,
                source=d.source,
                confidence=d.confidence,
                basis=d.basis,
                mm_per_px=photo.gsd_mm_per_px if photo else None,
                photo_area_px=(photo.width_px * photo.height_px) if photo else 0,
            )
        )
    return rows


@router.post("/reports/build")
def build_report(body: ReportRequest, db: Session = Depends(get_db)) -> Response:
    """보고서 산출물을 만들어 ZIP으로 내려준다."""
    valid = {"photo_sheet", "quantity", "survey_dxf"}
    kinds = [k for k in body.kinds if k in valid]
    if not kinds:
        raise HTTPException(400, "결과 파일을 최소 1개 선택하십시오")

    insp = db.get(Inspection, body.inspection_id)
    if not insp:
        raise HTTPException(404, "점검 회차를 찾을 수 없습니다")
    b = db.get(Building, insp.building_id)

    # 범위 결정 — 도면 기준이면 그 도면의 위치에 연결된 그룹만
    group_ids: set[int] | None = None
    drawings_payload: list[dict] = []

    if body.scope == "drawing":
        stmt = select(Drawing).where(Drawing.building_id == b.id)
        if body.drawing_ids:
            stmt = stmt.where(Drawing.id.in_(body.drawing_ids))
        selected = db.scalars(stmt).all()
        group_ids = set()
        for d in selected:
            spots = db.scalars(select(Spot).where(Spot.drawing_id == d.id)).all()
            payload_spots = []
            for s in spots:
                if s.group_id:
                    group_ids.add(s.group_id)
                g = db.get(PhotoGroup, s.group_id) if s.group_id else None
                n_photo = (
                    int(db.scalar(select(func.count(Photo.id)).where(Photo.group_id == s.group_id)) or 0)
                    if s.group_id else 0
                )
                n_def = (
                    int(db.scalar(
                        select(func.count(Defect.id))
                        .join(Photo, Defect.photo_id == Photo.id)
                        .where(Photo.group_id == s.group_id)
                    ) or 0)
                    if s.group_id else 0
                )
                member = MEMBER_CLASSES.get(s.member_code)
                payload_spots.append(
                    {
                        "number": s.number, "x": s.x, "y": s.y,
                        "group_name": g.name if g else "",
                        "photo_count": n_photo, "defect_count": n_def,
                        "member_label": member.label_ko if member else s.member_code,
                        "direction": s.direction,
                    }
                )
            drawings_payload.append(
                {
                    "name": d.name,
                    "width": d.width_px,
                    "height": d.height_px,
                    "spots": payload_spots,
                }
            )
        if not group_ids:
            group_ids = None       # 도면에 연결된 그룹이 없으면 전체를 낸다
    elif body.scope == "group" and body.group_ids:
        group_ids = set(body.group_ids)

    rows = _collect_rows(db, insp, group_ids)
    if not rows:
        raise HTTPException(400, "선택한 범위에 결함이 없습니다")

    bundle = ReportBundle(
        building_name=b.name,
        facility_class=b.facility_class,
        inspected_at=insp.inspected_at,
        inspector=insp.inspector,
        safety_grade=insp.safety_grade,
        rows=rows,
        drawings=drawings_payload,
        scope_label="시설물·도면 기준" if body.scope == "drawing" else "사진 그룹 기준",
    )
    data, made = build_bundle_zip(bundle, kinds)

    stamp = insp.inspected_at.strftime("%Y%m%d")

    # HTTP 헤더는 latin-1만 담을 수 있다. 한글 시설물명을 그대로 넣으면
    # 응답 생성 단계에서 서버가 죽는다. ASCII 대체명과 RFC 5987 filename*을
    # 함께 보내, 최신 브라우저는 한글 이름으로 저장하고 구형은 대체명을 쓴다.
    korean = f"KO-Detect_{b.name}_{stamp}.zip"
    ascii_fallback = f"KO-Detect_{b.id}_{stamp}.zip"
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(korean)}"
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            # 파일 목록도 한글이므로 URL 인코딩해 보낸다
            "X-Report-Files": quote(", ".join(made)),
            "X-Report-Defects": str(len(rows)),
        },
    )


@router.get("/reports/preview")
def preview_report(
    inspection_id: int,
    scope: str = Query("drawing"),
    db: Session = Depends(get_db),
) -> dict:
    """다운로드 전에 무엇이 담기는지 보여준다.

    QuickGuide는 범위 선택 화면에서 도면별 위치·사진 수를 표시한다. 사용자가
    무엇을 받게 되는지 모르고 누르면, 빠진 것을 나중에야 알게 된다.
    """
    insp = db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "점검 회차를 찾을 수 없습니다")
    b = db.get(Building, insp.building_id)

    rows = _collect_rows(db, insp, None)
    by_type: dict[str, int] = {}
    no_qty = 0
    for r in rows:
        label = DEFECT_LABELS_KO.get(r.defect_type, r.defect_type.value)
        by_type[label] = by_type.get(label, 0) + 1
        if r.quantity is None:
            no_qty += 1

    drawings = [
        {
            "id": d.id,
            "name": d.name,
            "spot_count": int(
                db.scalar(select(func.count(Spot.id)).where(Spot.drawing_id == d.id)) or 0
            ),
        }
        for d in db.scalars(select(Drawing).where(Drawing.building_id == b.id)).all()
    ]

    return {
        "building": b.name,
        "inspection_at": insp.inspected_at.isoformat(),
        "scope": scope,
        "drawings": drawings,
        "groups": list_groups(inspection_id, db),
        "defect_total": len(rows),
        "defect_by_type": by_type,
        "manual_count": sum(1 for r in rows if r.source != "ai"),
        "quantity_unavailable": no_qty,
        "warnings": (
            [f"물량 산출 불가 {no_qty}건 — 스케일(GSD)이 없는 사진의 결함입니다"]
            if no_qty else []
        )
        + (
            ["도면이 없습니다. 외관조사망도는 빈 도면틀로 생성됩니다"]
            if not drawings else []
        ),
    }
