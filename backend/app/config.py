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

    @property
    def sqlalchemy_url(self) -> str:
        """관리형 Postgres가 주는 URL을 SQLAlchemy 드라이버 형식으로 정규화한다.

        Render·Heroku 등은 `postgres://...` 를 주는데 SQLAlchemy 2.x는 이 스킴을
        받지 않는다. 배포할 때마다 손으로 고치면 반드시 한 번은 빠뜨리므로
        여기서 한 번에 흡수한다.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.overlays_dir.mkdir(parents=True, exist_ok=True)
