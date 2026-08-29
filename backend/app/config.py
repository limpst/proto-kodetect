"""애플리케이션 설정 — 환경변수(.env)로 주입."""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "KO-Detect"
    app_title_ko: str = "건축물 안전진단 통합 플랫폼"
    version: str = "0.1.0"

    # ─── 인증 ───────────────────────────────────────────────
    auth_enabled: bool = True
    auth_user: str = "admin"
    auth_password: str = "kodetect"
    session_secret: str = secrets.token_urlsafe(32)
    session_max_age_sec: int = 8 * 3600
    login_max_attempts: int = 5
    login_lockout_sec: int = 300

    # ─── 저장소 ─────────────────────────────────────────────
    database_url: str = f"sqlite:///{(ROOT / 'kodetect.db').as_posix()}"
    storage_dir: Path = ROOT / "storage"

    # ─── 드론 카메라 기본 제원 (GSD 산정용) ─────────────────
    # 기본값은 DJI Mavic 3E 광각 카메라 기준.
    sensor_width_mm: float = 17.3
    focal_length_mm: float = 12.29
    image_width_px: int = 5280

    # ─── 검출 파라미터 ──────────────────────────────────────
    min_crack_length_px: int = 40
    detection_confidence_floor: float = 0.35

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def overlays_dir(self) -> Path:
        return self.storage_dir / "overlays"


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.overlays_dir.mkdir(parents=True, exist_ok=True)
