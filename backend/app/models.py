"""ORM 모델 — 건축물 · 부재 · 점검 · 사진 · 결함 · 균열추적 · 센서."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Building(Base):
    """진단 대상 건축물/시설물."""

    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    address: Mapped[str] = mapped_column(String(400), default="")
    # 시특법 시설물 종별 — 1종 / 2종 / 3종 / 기타
    facility_class: Mapped[str] = mapped_column(String(20), default="2종")
    structure_type: Mapped[str] = mapped_column(String(60), default="철근콘크리트")
    completed_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floors_above: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floors_below: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gross_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    environment: Mapped[str] = mapped_column(String(20), default="humid")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    inspections: Mapped[list["Inspection"]] = relationship(
        back_populates="building", cascade="all, delete-orphan"
    )
    channels: Mapped[list["SensorChannel"]] = relationship(
        back_populates="building", cascade="all, delete-orphan"
    )
    tracks: Mapped[list["CrackTrack"]] = relationship(
        back_populates="building", cascade="all, delete-orphan"
    )


class Inspection(Base):
    """점검·진단 1회차."""

    __tablename__ = "inspections"

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(
        ForeignKey("buildings.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="regular")
    inspected_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    inspector: Mapped[str] = mapped_column(String(100), default="")
    safety_grade: Mapped[str | None] = mapped_column(String(2), nullable=True)
    defect_index: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    building: Mapped[Building] = relationship(back_populates="inspections")
    photos: Mapped[list["Photo"]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan"
    )
    defects: Mapped[list["Defect"]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan"
    )


class Photo(Base):
    """점검 사진 — 드론 촬영 메타데이터 포함."""

    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(300))
    overlay_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    width_px: Mapped[int] = mapped_column(Integer, default=0)
    height_px: Mapped[int] = mapped_column(Integer, default=0)
    member_code: Mapped[str] = mapped_column(String(40), default="slab")

    # 드론 메타 — GSD(mm/px)는 고도/초점거리/센서폭으로 산정하거나 직접 입력
    source: Mapped[str] = mapped_column(String(20), default="drone")
    altitude_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    gimbal_pitch_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    gsd_mm_per_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    inspection: Mapped[Inspection] = relationship(back_populates="photos")
    defects: Mapped[list["Defect"]] = relationship(
        back_populates="photo", cascade="all, delete-orphan"
    )


class CrackTrack(Base):
    """동일 균열의 회차 간 연속 추적 단위 — 시계열 진행 분석의 키."""

    __tablename__ = "crack_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(
        ForeignKey("buildings.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(60))
    member_code: Mapped[str] = mapped_column(String(40), default="slab")
    location_note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    building: Mapped[Building] = relationship(back_populates="tracks")
    defects: Mapped[list["Defect"]] = relationship(back_populates="track")


class Defect(Base):
    """검출된 결함 1건."""

    __tablename__ = "defects"

    id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), index=True
    )
    photo_id: Mapped[int | None] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), nullable=True, index=True
    )
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("crack_tracks.id", ondelete="SET NULL"), nullable=True, index=True
    )

    defect_type: Mapped[str] = mapped_column(String(30), index=True)
    member_code: Mapped[str] = mapped_column(String(40), default="slab")

    width_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    length_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    area_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    grade: Mapped[str] = mapped_column(String(2), default="a")
    severity: Mapped[float] = mapped_column(Float, default=0.0)
    repair_required: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    basis: Mapped[str] = mapped_column(String(200), default="")

    bbox: Mapped[str] = mapped_column(String(80), default="")
    polyline: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    inspection: Mapped[Inspection] = relationship(back_populates="defects")
    photo: Mapped[Photo | None] = relationship(back_populates="defects")
    track: Mapped[CrackTrack | None] = relationship(back_populates="defects")


class SensorChannel(Base):
    """상시 계측 채널 (IoT)."""

    __tablename__ = "sensor_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(
        ForeignKey("buildings.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    member_code: Mapped[str] = mapped_column(String(40), default="slab")
    unit: Mapped[str] = mapped_column(String(16), default="mm")
    warn_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    critical_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_x: Mapped[float] = mapped_column(Float, default=0.0)
    position_y: Mapped[float] = mapped_column(Float, default=0.0)
    position_z: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    building: Mapped[Building] = relationship(back_populates="channels")


class SensorReading(Base):
    """계측 시계열 표본."""

    __tablename__ = "sensor_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("sensor_channels.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[float] = mapped_column(Float)


Index("ix_reading_channel_ts", SensorReading.channel_id, SensorReading.ts)
