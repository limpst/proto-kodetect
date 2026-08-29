"""상시 계측 · 실시간 건전성 지수(MTM) API.

차트는 TradingView lightweight-charts를 쓰므로, 시계열은 모두
`{time: <epoch seconds>, value|open/high/low/close}` 형태로 내보낸다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import Building, Inspection, SensorChannel
from ..schemas import ChannelOut
from ..services.sensors import (
    CHANNEL_LABELS_KO,
    ChannelState,
    history,
    make_channel,
    sample,
    to_ohlc,
)
from ..services.timeseries import HealthMark, mark_to_market

router = APIRouter(prefix="/api/live", tags=["live"])

# 채널 상태 캐시 — 시뮬레이터 상태를 요청 간에 유지한다
_STATES: dict[str, ChannelState] = {}
_LAST_MARK: dict[int, HealthMark] = {}


def _state(ch: SensorChannel) -> ChannelState:
    st = _STATES.get(ch.code)
    if st is None:
        st = make_channel(ch.code, ch.kind)
        st.warn = ch.warn_threshold if ch.warn_threshold is not None else st.warn
        st.critical = (
            ch.critical_threshold if ch.critical_threshold is not None else st.critical
        )
        _STATES[ch.code] = st
    return st


def _channels(db: Session, building_id: int) -> list[SensorChannel]:
    rows = db.scalars(
        select(SensorChannel)
        .where(SensorChannel.building_id == building_id)
        .order_by(SensorChannel.id)
    ).all()
    if not rows:
        raise HTTPException(404, "계측 채널이 없습니다")
    return rows


def _base_defect_index(db: Session, building_id: int) -> float:
    latest = db.scalars(
        select(Inspection)
        .where(Inspection.building_id == building_id)
        .order_by(Inspection.inspected_at.desc())
        .limit(1)
    ).first()
    return float(latest.defect_index) if latest else 0.0


def _status(value: float, warn: float | None, crit: float | None) -> str:
    if crit is not None and value >= crit:
        return "critical"
    if warn is not None and value >= warn:
        return "warn"
    return "normal"


# 계측기 설치 시점 — 장기 드리프트의 기준. 상시계측은 대개 최근에 설치되므로
# 준공연도를 기준으로 잡으면 수십 년치 추세가 누적돼 값이 비현실적으로 커진다.
SENSOR_INSTALL_YEARS_AGO = 3.0


def _reference(db: Session, building_id: int) -> datetime:
    """장기 추세 기준 시점 = 계측기 설치 시점."""
    return datetime.now() - timedelta(days=int(SENSOR_INSTALL_YEARS_AGO * 365.25))


@router.get("/{building_id}/channels", response_model=list[ChannelOut])
def channels(building_id: int, db: Session = Depends(get_db)) -> list[ChannelOut]:
    now = datetime.now()
    ref = _reference(db, building_id)
    out = []
    for ch in _channels(db, building_id):
        st = _state(ch)
        years = (now - ref).total_seconds() / (365.25 * 86400.0)
        v = round(sample(st, now, years_elapsed=years), 4)
        out.append(
            ChannelOut(
                id=ch.id,
                code=ch.code,
                kind=ch.kind,
                kind_label=CHANNEL_LABELS_KO.get(ch.kind, ch.kind),
                member_code=ch.member_code,
                unit=st.unit,
                warn_threshold=st.warn,
                critical_threshold=st.critical,
                position=[ch.position_x, ch.position_y, ch.position_z],
                latest=v,
                status=_status(v, st.warn, st.critical),
            )
        )
    return out


@router.get("/{building_id}/history")
def channel_history(
    building_id: int,
    code: str,
    points: int = 480,
    interval_sec: int = 60,
    db: Session = Depends(get_db),
) -> dict:
    ch = next((c for c in _channels(db, building_id) if c.code == code), None)
    if ch is None:
        raise HTTPException(404, f"채널 {code} 를 찾을 수 없습니다")
    st = _state(ch)
    ref = _reference(db, building_id)
    series = history(
        st, datetime.now(), points=points, interval_sec=interval_sec, reference=ref
    )
    return {
        "code": code,
        "unit": st.unit,
        "kind_label": CHANNEL_LABELS_KO.get(ch.kind, ch.kind),
        "warn": st.warn,
        "critical": st.critical,
        "series": [
            {"time": int(ts.timestamp()), "value": v} for ts, v in series
        ],
    }


def _health_series(
    db: Session, building_id: int, points: int, interval_sec: int
) -> list[tuple[datetime, float]]:
    """구간 전체에 대해 건전성 지수를 재평가한 시계열."""
    chans = _channels(db, building_id)
    states = [(_state(c), c) for c in chans]
    base = _base_defect_index(db, building_id)
    ref = _reference(db, building_id)
    now = datetime.now()

    out: list[tuple[datetime, float]] = []
    prev: HealthMark | None = None
    for i in range(points, 0, -1):
        ts = now - timedelta(seconds=interval_sec * i)
        years = (ts - ref).total_seconds() / (365.25 * 86400.0)
        values = {
            st.code: (sample(st, ts, years_elapsed=years), st.warn, st.critical)
            for st, _ in states
        }
        mark = mark_to_market(base, values, prev)
        out.append((ts, mark.index))
        prev = mark
    return out


@router.get("/{building_id}/health/ohlc")
def health_ohlc(
    building_id: int,
    points: int = 720,
    interval_sec: int = 60,
    bucket_sec: int = 300,
    db: Session = Depends(get_db),
) -> dict:
    """건전성 지수 캔들 — 구간 내 진폭이 구조 응답의 변동성을 보여준다."""
    series = _health_series(db, building_id, points, interval_sec)
    return {
        "building_id": building_id,
        "bucket_sec": bucket_sec,
        "candles": to_ohlc(series, bucket_sec),
        "line": [{"time": int(t.timestamp()), "value": v} for t, v in series],
    }


@router.get("/{building_id}/tick")
def tick(building_id: int, db: Session = Depends(get_db)) -> dict:
    return _build_tick(db, building_id)


def _build_tick(db: Session, building_id: int) -> dict:
    chans = _channels(db, building_id)
    ref = _reference(db, building_id)
    now = datetime.now()
    years = (now - ref).total_seconds() / (365.25 * 86400.0)
    base = _base_defect_index(db, building_id)

    values: dict[str, float] = {}
    payload: dict[str, tuple[float, float | None, float | None]] = {}
    alerts = []
    for c in chans:
        st = _state(c)
        v = round(sample(st, now, years_elapsed=years), 4)
        values[c.code] = v
        payload[c.code] = (v, st.warn, st.critical)
        status = _status(v, st.warn, st.critical)
        if status != "normal":
            alerts.append(
                {
                    "code": c.code,
                    "kind_label": CHANNEL_LABELS_KO.get(c.kind, c.kind),
                    "value": v,
                    "unit": st.unit,
                    "threshold": st.critical if status == "critical" else st.warn,
                    "status": status,
                    "member_code": c.member_code,
                }
            )

    mark = mark_to_market(base, payload, _LAST_MARK.get(building_id))
    _LAST_MARK[building_id] = mark
    return {
        "ts": int(now.timestamp()),
        "values": values,
        "statuses": {
            c.code: _status(values[c.code], _state(c).warn, _state(c).critical)
            for c in chans
        },
        "health_index": mark.index,
        "health_grade": mark.grade,
        "health_delta": mark.delta,
        "contributors": mark.contributors,
        "alerts": alerts,
    }


@router.websocket("/ws/{building_id}")
async def live_ws(websocket: WebSocket, building_id: int) -> None:
    """실시간 스트림 — 1초 간격으로 계측값과 건전성 지수를 밀어준다."""
    await websocket.accept()
    try:
        while True:
            with SessionLocal() as db:
                try:
                    payload = _build_tick(db, building_id)
                except HTTPException as exc:
                    await websocket.send_json({"error": exc.detail})
                    return
            await websocket.send_json(payload)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except Exception:
        # 클라이언트가 끊어진 경우 조용히 종료한다
        return
