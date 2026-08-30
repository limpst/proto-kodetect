"""경량 스키마 보정 — 새로 생긴 컬럼을 기존 테이블에 더한다.

왜 필요한가
-----------
`create_all()` 은 없는 테이블만 만든다. 이미 있는 테이블에 컬럼이 추가되면
아무 일도 하지 않고, 앱은 다음 조회에서 "no such column" 으로 죽는다.
배포된 인스턴스가 영구 DB(Postgres)를 쓰면 이 문제가 실제로 터진다.

Alembic 을 쓰지 않는 이유
-------------------------
지금 필요한 변경은 "널 허용 컬럼 추가" 뿐이다. 이 범위에서는 마이그레이션
파일 관리 비용이 이득보다 크다. 컬럼 삭제·타입 변경·데이터 이관이 필요해지는
시점에 Alembic 으로 넘어가야 하며, 그때는 이 파일을 지우고 시작하면 된다.

한계를 분명히 해 둔다 — 이 함수는 **컬럼 추가만** 한다. 이름 변경, 타입 변경,
제약 추가는 하지 않는다. 조용히 반쯤 맞추는 것보다 아무것도 안 하는 편이 낫다.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from .db import Base, engine

log = logging.getLogger("kodetect.migrate")

# SQLAlchemy 타입 → DDL 문자열. 방언 차이가 있는 것만 갈라 적는다.
_DDL_OVERRIDES = {
    "sqlite": {"DATETIME": "DATETIME", "BOOLEAN": "BOOLEAN"},
}


def _column_ddl(column, dialect_name: str) -> str:
    type_sql = column.type.compile(dialect=engine.dialect)
    return f"{column.name} {type_sql}"


def sync_columns() -> list[str]:
    """모델에는 있고 테이블에는 없는 컬럼을 추가한다. 추가한 목록을 돌려준다."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    added: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all 이 만든다
            have = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    # NOT NULL 컬럼을 기본값 없이 추가하면 기존 행이 위반한다.
                    # 이런 변경은 사람이 판단해야 하므로 건너뛰고 로그만 남긴다.
                    log.warning(
                        "컬럼 %s.%s 는 NOT NULL 이고 기본값이 없어 자동 추가하지 않습니다",
                        table.name, column.name,
                    )
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {_column_ddl(column, engine.dialect.name)}"
                try:
                    conn.execute(text(ddl))
                    added.append(f"{table.name}.{column.name}")
                except Exception as exc:
                    log.warning("컬럼 추가 실패 %s.%s — %s", table.name, column.name, exc)

    if added:
        log.info("스키마 보정: %s", ", ".join(added))
    return added
