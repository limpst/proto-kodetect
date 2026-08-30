"""API 입출력 스키마."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─── 인증 ──────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str


class SessionOut(BaseModel):
    authenticated: bool
    auth_enabled: bool
    user: str | None = None
    exp: int | None = None


# ─── 건축물 ────────────────────────────────────────────────────
class BuildingIn(BaseModel):
    name: str
    address: str = ""
    facility_class: str = "2종"
    structure_type: str = "철근콘크리트"
    completed_year: int | None = None
    floors_above: int | None = None
    floors_below: int | None = None
    gross_area_m2: float | None = None
    environment: str = "humid"
    latitude: float | None = None
    longitude: float | None = None


class BuildingOut(BuildingIn):
    id: int
    created_at: datetime
    latest_grade: str | None = None
    latest_index: float | None = None
    inspection_count: int = 0
    defect_count: int = 0

    model_config = {"from_attributes": True}


# ─── 점검 ──────────────────────────────────────────────────────
class InspectionIn(BaseModel):
    building_id: int
    kind: str = "regular"
    inspected_at: datetime | None = None
    inspector: str = ""
    notes: str = ""


class DefectOut(BaseModel):
    id: int
    defect_type: str
    member_code: str
    width_mm: float | None
    length_mm: float | None
    area_ratio: float | None
    grade: str
    severity: float
    repair_required: bool
    confidence: float
    basis: str
    bbox: str
    photo_id: int | None
    track_id: int | None

    model_config = {"from_attributes": True}


class MemberSummary(BaseModel):
    member_code: str
    member_label: str
    grade: str
    defect_index: float
    defect_count: int


class InspectionOut(BaseModel):
    id: int
    building_id: int
    kind: str
    kind_label: str
    inspected_at: datetime
    inspector: str
    safety_grade: str | None
    defect_index: float
    notes: str
    defect_count: int = 0
    repair_required_count: int = 0
    members: list[MemberSummary] = Field(default_factory=list)

    model_config = {"from_attributes": True}


# ─── 검출 ──────────────────────────────────────────────────────
class DetectionOptions(BaseModel):
    """업로드 시 함께 보내는 촬영 조건."""

    inspection_id: int
    member_code: str = "slab"
    source: str = "drone"
    distance_m: float | None = None
    gimbal_pitch_deg: float | None = None
    gsd_mm_per_px: float | None = None
    sensitivity: float = 1.0


class CrackOut(BaseModel):
    index: int
    bbox: list[int]
    # 중심선 좌표 — 프론트가 원본 위에 균열 형상을 직접 그린다.
    # 서버 오버레이 이미지만으로는 확대·선택·하이라이트가 불가능하다.
    polyline: list[list[int]] = Field(default_factory=list)
    length_px: float
    length_mm: float | None
    width_mm_p95: float | None
    width_mm_max: float | None
    confidence: float
    grade: str
    repair_required: bool
    basis: str


class DetectionOut(BaseModel):
    photo_id: int
    filename: str
    overlay_url: str
    image_size: list[int]
    mm_per_px: float | None
    gsd_source: str
    sharpness: float
    quality_ok: bool
    quality_note: str
    crack_count: int
    crack_area_ratio: float
    cracks: list[CrackOut]
    inspection_grade: str | None = None
    inspection_index: float | None = None


# ─── 시계열 ────────────────────────────────────────────────────
class ProgressionPointOut(BaseModel):
    at: datetime
    width_mm: float
    inspection_id: int | None


class ProgressionOut(BaseModel):
    track_id: int
    label: str
    member_code: str
    model: str
    rate_mm_per_year: float | None
    r_squared: float | None
    latest_width_mm: float | None
    allowable_mm: float | None
    years_to_allowable: float | None
    verdict: str
    points: list[ProgressionPointOut]
    forecast: list[list]


# ─── 계측 ──────────────────────────────────────────────────────
class ChannelOut(BaseModel):
    id: int
    code: str
    kind: str
    kind_label: str
    member_code: str
    unit: str
    warn_threshold: float | None
    critical_threshold: float | None
    position: list[float]
    latest: float | None = None
    status: str = "normal"


class TickOut(BaseModel):
    ts: int
    values: dict[str, float]
    health_index: float
    health_grade: str
    health_delta: float
    alerts: list[dict] = Field(default_factory=list)


# ─── 정책 ──────────────────────────────────────────────────────
class PolicyActionOut(BaseModel):
    member_code: str
    member_label: str
    action: str
    action_index: int
    expected_value: float
    cvar: float
    belief_grade: str
    rationale: str
