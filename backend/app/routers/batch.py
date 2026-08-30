"""사진 일괄 등록 · AI 일괄 분석 (진행 표시줄).

QuickGuide STEP 03 의 흐름은 **사진 등록 → 그룹 → AI 분석** 세 단계다.
등록 즉시 분석하면 이 흐름이 무너진다.

  - 현장에서 100장을 올리는 동안 사용자를 기다리게 만든다
  - 그룹을 나누기 전에 분석되어, 부재별로 감도를 달리 줄 수 없다
  - 실패한 한 장 때문에 업로드 전체가 되돌려진다

그래서 등록은 파일을 받아 `pending` 상태로만 남기고, 분석은 별도 작업으로
돌린다. 작업은 백그라운드 스레드에서 진행하고 화면은 진척을 조회한다
("상단 진행표시줄(예: 0/9)로 진척 확인").

작업 상태를 메모리에 두는 이유
------------------------------
분석 진척은 재시작 후까지 살아 있을 필요가 없는 휘발성 정보다. DB에 넣으면
분석 한 장마다 쓰기가 생겨 오히려 느려진다. 프로세스가 죽으면 사진 상태
(`pending`)가 남으므로 다시 돌리면 그만이다.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import Inspection, Photo, PhotoGroup
from .photo_ops import _run_detection

router = APIRouter(prefix="/api", tags=["batch"])

ALLOWED_EXT = {"jpg", "jpeg", "png", "bmp", "webp"}
MAX_BYTES = 30 * 1024 * 1024


# ─── 작업 레지스트리 ───────────────────────────────────────────
@dataclass
class BatchJob:
    id: str
    total: int
    done: int = 0
    failed: int = 0
    detected: int = 0
    state: str = "running"            # running | finished | error
    current: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    items: list[dict] = field(default_factory=list)
    error: str = ""

    def payload(self) -> dict:
        elapsed = (
            (self.finished_at or datetime.now()) - self.started_at
        ).total_seconds()
        per = elapsed / self.done if self.done else None
        return {
            "job_id": self.id,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "detected": self.detected,
            "current": self.current,
            "elapsed_sec": round(elapsed, 1),
            # 남은 시간을 알려주지 않으면 사용자는 멈춘 줄 안다
            "eta_sec": round(per * (self.total - self.done), 1) if per else None,
            "items": self.items,
            "error": self.error,
        }


_JOBS: dict[str, BatchJob] = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kd-batch")


# ─── 사진 일괄 등록 ────────────────────────────────────────────
@router.post("/photos/upload")
async def upload_photos(
    files: list[UploadFile] = File(...),
    inspection_id: int = Form(...),
    member_code: str = Form("slab"),
    source: str = Form("drone"),
    group_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> dict:
    """사진을 등록만 한다 — 분석은 하지 않는다.

    한 장이 잘못돼도 나머지는 살린다. 현장에서 수십 장을 올리는데 한 장 때문에
    전부 되돌아가면 그 자체가 사고다.
    """
    if not db.get(Inspection, inspection_id):
        raise HTTPException(404, "점검 회차를 찾을 수 없습니다")
    if group_id is not None and not db.get(PhotoGroup, group_id):
        raise HTTPException(404, "사진 그룹을 찾을 수 없습니다")

    added, skipped = [], []
    for f in files:
        raw = await f.read()
        name = f.filename or "upload.jpg"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_EXT:
            skipped.append({"filename": name, "reason": "지원하지 않는 형식"})
            continue
        if not raw:
            skipped.append({"filename": name, "reason": "빈 파일"})
            continue
        if len(raw) > MAX_BYTES:
            skipped.append({"filename": name, "reason": f"{MAX_BYTES // 1024 // 1024}MB 초과"})
            continue

        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            skipped.append({"filename": name, "reason": "이미지를 해석할 수 없음"})
            continue

        stored = f"{uuid.uuid4().hex}.{ext}"
        (settings.uploads_dir / stored).write_bytes(raw)

        p = Photo(
            inspection_id=inspection_id,
            filename=stored,
            member_code=member_code,
            source=source,
            group_id=group_id,
            width_px=image.shape[1],
            height_px=image.shape[0],
            analysis_state="pending",
            analysis_note="분석 대기 — [AI 분석하기]를 누르십시오",
            captured_at=datetime.now(),
        )
        db.add(p)
        db.flush()
        added.append({"id": p.id, "filename": name, "stored": stored})

    db.commit()
    return {
        "added": len(added),
        "skipped": len(skipped),
        "photos": added,
        "rejected": skipped,
    }


# ─── AI 일괄 분석 ──────────────────────────────────────────────
class BatchIn(BaseModel):
    inspection_id: int | None = None
    photo_ids: list[int] = []
    group_id: int | None = None
    sensitivity: float = 1.0
    only_pending: bool = True


def _resolve_targets(db: Session, body: BatchIn) -> list[int]:
    stmt = select(Photo.id)
    if body.photo_ids:
        stmt = stmt.where(Photo.id.in_(body.photo_ids))
    elif body.group_id is not None:
        stmt = stmt.where(Photo.group_id == body.group_id)
    elif body.inspection_id is not None:
        stmt = stmt.where(Photo.inspection_id == body.inspection_id)
    else:
        raise HTTPException(400, "분석 대상을 지정하십시오 (사진·그룹·점검 회차)")

    if body.only_pending and not body.photo_ids:
        # 이미 분석한 사진을 다시 돌리면 사람이 고친 감도 설정이 덮인다
        stmt = stmt.where(Photo.analysis_state == "pending")
    return list(db.scalars(stmt.order_by(Photo.id)).all())


def _worker(job: BatchJob, photo_ids: list[int], sensitivity: float) -> None:
    """백그라운드 분석 — 스레드마다 새 세션을 쓴다."""
    try:
        for pid in photo_ids:
            with SessionLocal() as db:
                p = db.get(Photo, pid)
                if p is None:
                    job.failed += 1
                    job.done += 1
                    continue
                job.current = p.filename
                try:
                    out = _run_detection(db, p, sensitivity)
                    job.detected += out["detected"]
                    job.items.append(
                        {
                            "photo_id": pid,
                            "detected": out["detected"],
                            "state": out["analysis_state"],
                            "sharpness": out["sharpness"],
                        }
                    )
                except Exception as exc:  # 한 장 실패가 전체를 멈추면 안 된다
                    p.analysis_state = "failed"
                    p.analysis_note = f"분석 실패: {exc}"[:300]
                    db.commit()
                    job.failed += 1
                    job.items.append({"photo_id": pid, "error": str(exc)[:200]})
                finally:
                    job.done += 1
        job.state = "finished"
    except Exception as exc:
        job.state = "error"
        job.error = str(exc)[:400]
    finally:
        job.current = ""
        job.finished_at = datetime.now()


@router.post("/detect/batch")
def start_batch(body: BatchIn, db: Session = Depends(get_db)) -> dict:
    """AI 일괄 분석 시작 — 작업 번호를 돌려주고 즉시 반환한다."""
    targets = _resolve_targets(db, body)
    if not targets:
        raise HTTPException(400, "분석할 사진이 없습니다 (이미 모두 분석되었을 수 있습니다)")

    job = BatchJob(id=uuid.uuid4().hex[:12], total=len(targets))
    with _LOCK:
        _JOBS[job.id] = job
        # 오래된 작업 정리 — 무한히 쌓이지 않게 한다
        if len(_JOBS) > 40:
            for k in sorted(_JOBS, key=lambda k: _JOBS[k].started_at)[:20]:
                if _JOBS[k].state != "running":
                    _JOBS.pop(k, None)

    _POOL.submit(_worker, job, targets, body.sensitivity)
    return job.payload()


@router.get("/detect/batch/{job_id}")
def batch_status(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다 (재시작되었을 수 있습니다)")
    return job.payload()


@router.get("/detect/batch")
def list_batches() -> list[dict]:
    with _LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j.started_at, reverse=True)
    return [
        {k: v for k, v in j.payload().items() if k != "items"} for j in jobs[:10]
    ]
