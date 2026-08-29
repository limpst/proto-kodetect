"""SQLAlchemy 엔진 · 세션 · Base."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_url = settings.sqlalchemy_url
_is_sqlite = _url.startswith("sqlite")
engine = create_engine(
    _url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # 관리형 Postgres는 유휴 커넥션을 끊는다. 끊긴 커넥션을 그대로 쓰면
    # 첫 요청이 산발적으로 실패하므로 사용 전에 확인한다.
    pool_pre_ping=not _is_sqlite,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  -- 테이블 등록

    Base.metadata.create_all(bind=engine)
