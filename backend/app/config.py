"""애플리케이션 설정 — 환경변수(.env)로 주입."""

from __future__ import annotations

import os
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

    # ─── 학습 모델 ──────────────────────────────────────────
    # 파일이 있으면 학습 검출기를, 없으면 고전 검출기를 쓴다.
    # 없다고 서비스가 멎으면 안 되므로 존재 여부로만 판단한다.
    segmenter_model: str = "models/seg_v1/segmenter.onnx"

    # ─── 검출 파라미터 ──────────────────────────────────────
    min_crack_length_px: int = 40
    detection_confidence_floor: float = 0.35

    @property
    def segmenter_path(self) -> Path | None:
        p = ROOT / self.segmenter_model
        return p if p.exists() else None

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

    @property
    def storage_summary(self) -> dict:
        """어디에 저장되고 있으며 재시작을 견디는가.

        무료 배포에서 DATABASE_URL 을 주지 않으면 SQLite 파일이 컨테이너
        파일시스템에 생기고, 배포할 때마다 통째로 사라진다. 현장 사진·그룹·
        도면·수기 손상이 예고 없이 날아가는데도 화면에는 '데이터가 없습니다'
        로만 보여 원인을 짚을 수 없다. 그래서 상태를 노출한다.
        """
        url = self.sqlalchemy_url
        scheme = url.split("://", 1)[0]
        is_sqlite = scheme.startswith("sqlite")

        # 경로 모양으로 영속성을 추측하면 틀린다. 지금 배포본의 저장소는
        # 'storage' 라는 평범한 상대 경로인데도 재배포마다 사라진다 —
        # 컨테이너째 교체되기 때문이다. 그래서 '여기가 교체되는 컨테이너인가'
        # 를 직접 본다. Render 는 RENDER 를, 디스크를 붙이면 마운트 경로를
        # RENDER_DISK_MOUNT_PATH 로 알려준다.
        on_container = bool(os.environ.get("RENDER"))
        mount = os.environ.get("RENDER_DISK_MOUNT_PATH")
        # 문자열 접두사로 비교하면 플랫폼마다 구분자가 달라 어긋난다.
        on_disk = bool(mount) and self.storage_dir.resolve().is_relative_to(
            Path(mount).resolve()
        )
        # 로컬(컨테이너 아님)에서는 파일이 그대로 남는다. 없는 위험을
        # 경고하면 진짜 경고까지 무시하게 된다.
        storage_persistent = on_disk or not on_container

        return {
            "database": "sqlite" if is_sqlite else scheme,
            # 관리형 Postgres 는 컨테이너와 수명이 분리돼 있다.
            "database_persistent": not is_sqlite or not on_container,
            "storage_dir": str(self.storage_dir),
            "storage_persistent": storage_persistent,
        }


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.overlays_dir.mkdir(parents=True, exist_ok=True)
