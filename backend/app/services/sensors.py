"""상시 계측(IoT) 시뮬레이션 엔진.

실계측기를 연결하기 전까지 대시보드·경보·건전성 지수를 실제와 같은 파형으로
구동하기 위한 물리 기반 합성기다. 채널 종류마다 실제 계측에서 관찰되는 성분을
그대로 재현한다.

* 균열게이지 — 장기 진행(추세) + 일일 온도 신축 + 계측 잡음 + 드문 계단 변화
* 경사계     — 평균회귀(OU) 과정 + 완만한 부등침하 추세
* 진동       — 배경 잡음 위의 간헐적 버스트(장비·교통·공사)
* 침하계     — 단조 증가, 압밀 곡선 형태
* 온습도     — 일주기 정현파 + 잡음

시드를 고정하면 항상 같은 파형이 나오므로, 화면 시연과 회귀 테스트가 재현된다.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

# 채널 종류별 기본 제원: (단위, 기준값, 경보임계, 위험임계)
CHANNEL_SPECS: dict[str, tuple[str, float, float, float]] = {
    "crack_gauge": ("mm", 0.25, 0.30, 1.00),
    "tilt": ("deg", 0.05, 0.20, 0.50),
    "vibration": ("mm/s", 0.60, 2.50, 5.00),
    "settlement": ("mm", 2.00, 10.00, 25.00),
    "strain": ("ue", 120.0, 400.0, 800.0),
    "temperature": ("degC", 16.0, 35.0, 45.0),
    "humidity": ("%RH", 55.0, 85.0, 95.0),
}

CHANNEL_LABELS_KO: dict[str, str] = {
    "crack_gauge": "균열게이지",
    "tilt": "경사계",
    "vibration": "진동",
    "settlement": "침하계",
    "strain": "변형률",
    "temperature": "온도",
    "humidity": "습도",
}


@dataclass
class ChannelState:
    code: str
    kind: str
    unit: str
    baseline: float
    warn: float
    critical: float
    trend_per_year: float
    value: float
    seed: int


def _seed_of(code: str) -> int:
    return int(hashlib.blake2b(code.encode(), digest_size=4).hexdigest(), 16)


def make_channel(code: str, kind: str, *, trend_per_year: float | None = None) -> ChannelState:
    unit, baseline, warn, critical = CHANNEL_SPECS.get(
        kind, CHANNEL_SPECS["crack_gauge"]
    )
    rng = np.random.default_rng(_seed_of(code))
    if trend_per_year is None:
        # 채널마다 고유한 장기 추세 — 대부분 안정적이고 일부만 뚜렷한 진행을 보인다.
        trend_per_year = float(rng.choice([0.0, 0.004, 0.012, 0.030]) * baseline * 4)
    return ChannelState(
        code=code,
        kind=kind,
        unit=unit,
        baseline=baseline * float(rng.uniform(0.85, 1.15)),
        warn=warn,
        critical=critical,
        trend_per_year=trend_per_year,
        value=baseline,
        seed=_seed_of(code),
    )


def _daily_phase(ts: datetime) -> float:
    """하루를 0~2pi로 사상 — 일주기 성분 생성용."""
    sec = ts.hour * 3600 + ts.minute * 60 + ts.second
    return 2.0 * math.pi * sec / 86400.0


def sample(ch: ChannelState, ts: datetime, *, years_elapsed: float = 0.0) -> float:
    """특정 시각의 계측값을 결정론적으로 생성한다.

    시각 자체를 난수 시드에 섞으므로, 같은 시각을 다시 물어도 같은 값이 나온다.
    (차트 새로고침 시 과거 구간이 흔들리지 않게 하기 위함)
    """
    rng = np.random.default_rng(ch.seed ^ int(ts.timestamp()))
    phase = _daily_phase(ts)
    base = ch.baseline + ch.trend_per_year * years_elapsed

    if ch.kind == "crack_gauge":
        # 온도가 오르면 콘크리트가 팽창해 균열이 닫힌다 → 위상 반전
        thermal = -0.018 * math.sin(phase - math.pi / 3)
        noise = float(rng.normal(0, 0.004))
        return max(0.0, base + thermal + noise)

    if ch.kind == "tilt":
        # 평균회귀 — 순간값은 흔들리지만 장기 평균은 추세를 따른다
        noise = float(rng.normal(0, 0.012))
        return max(0.0, base + 0.006 * math.sin(phase) + noise)

    if ch.kind == "vibration":
        burst = float(rng.random() < 0.04) * float(rng.uniform(1.0, 3.5))
        # 주간 시간대에 배경 진동이 높다
        day_factor = 1.0 + 0.6 * max(0.0, math.sin(phase - math.pi / 2))
        return max(0.0, base * day_factor * float(rng.uniform(0.6, 1.4)) + burst)

    if ch.kind == "settlement":
        # 압밀 침하 — 시간의 제곱근에 비례해 수렴
        consolidated = ch.baseline + ch.trend_per_year * 3.0 * math.sqrt(max(years_elapsed, 0.0))
        return max(0.0, consolidated + float(rng.normal(0, 0.08)))

    if ch.kind == "strain":
        thermal = 45.0 * math.sin(phase - math.pi / 3)
        live_load = float(rng.normal(0, 18.0))
        return base + thermal + live_load

    if ch.kind == "temperature":
        return base + 9.0 * math.sin(phase - math.pi / 2) + float(rng.normal(0, 0.6))

    if ch.kind == "humidity":
        return float(
            np.clip(base - 12.0 * math.sin(phase - math.pi / 2) + rng.normal(0, 2.0),
                    5.0, 100.0)
        )

    return base + float(rng.normal(0, 0.01))


def history(
    ch: ChannelState,
    end: datetime,
    *,
    points: int = 720,
    interval_sec: int = 60,
    reference: datetime | None = None,
) -> list[tuple[datetime, float]]:
    """차트 초기 적재용 과거 시계열."""
    ref = reference or (end - timedelta(days=365))
    out = []
    for i in range(points, 0, -1):
        ts = end - timedelta(seconds=interval_sec * i)
        years = (ts - ref).total_seconds() / (365.25 * 86400.0)
        out.append((ts, round(sample(ch, ts, years_elapsed=years), 4)))
    return out


def to_ohlc(
    series: list[tuple[datetime, float]], bucket_sec: int = 300
) -> list[dict]:
    """시계열을 캔들(OHLC)로 집계한다 — TradingView 차트 입력 형식.

    건전성 지수를 시세처럼 보여줄 때, 구간 내 변동폭(고가-저가)이 곧
    구조 응답의 진폭이므로 캔들이 선보다 정보량이 많다.
    """
    buckets: dict[int, list[float]] = {}
    for ts, v in series:
        key = int(ts.timestamp()) // bucket_sec * bucket_sec
        buckets.setdefault(key, []).append(v)

    out = []
    for key in sorted(buckets):
        vals = buckets[key]
        out.append(
            {
                "time": key,
                "open": round(vals[0], 4),
                "high": round(max(vals), 4),
                "low": round(min(vals), 4),
                "close": round(vals[-1], 4),
            }
        )
    return out


def default_channels(building_id: int) -> list[tuple[str, str, str, tuple[float, float, float]]]:
    """건축물 1동에 기본 배치하는 계측 채널 구성.

    반환: (코드, 종류, 부재코드, (x, y, z) 상대 위치)
    위치는 3D 뷰에서 센서를 구조물 위에 표시하기 위한 정규화 좌표다.
    """
    return [
        (f"B{building_id}-CG-01", "crack_gauge", "column", (-0.35, 0.15, 0.35)),
        (f"B{building_id}-CG-02", "crack_gauge", "girder", (0.30, 0.55, -0.20)),
        (f"B{building_id}-CG-03", "crack_gauge", "wall_shear", (0.00, 0.80, 0.40)),
        (f"B{building_id}-TL-01", "tilt", "column", (-0.40, 0.90, -0.40)),
        (f"B{building_id}-VB-01", "vibration", "slab", (0.10, 0.45, 0.10)),
        (f"B{building_id}-ST-01", "settlement", "foundation", (-0.20, -0.05, 0.20)),
        (f"B{building_id}-SR-01", "strain", "girder", (0.35, 0.35, 0.30)),
        (f"B{building_id}-TH-01", "temperature", "slab", (0.45, 0.70, -0.35)),
        (f"B{building_id}-HM-01", "humidity", "wall_non", (-0.45, 0.40, 0.15)),
    ]
