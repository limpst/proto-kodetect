"""균열 진행 시계열 분석 및 실시간 건전성 지수(MTM).

두 가지 시간축을 다룬다.

1. **점검 회차 축** — 정기점검마다 같은 균열(CrackTrack)을 재측정한 폭 이력.
   진행 속도(mm/년)를 추정하고, 허용균열폭 도달 시점을 외삽해 잔여 기간을
   산출한다. 이것이 보수 우선순위 결정의 근거가 된다.

2. **상시 계측 축** — IoT 채널이 초 단위로 올리는 값. 이를 건전성 지수로
   즉시 환산해 실시간으로 갱신한다(Mark-to-Market). 금융의 MTM처럼,
   "지금 이 순간의 구조 건전성"을 하나의 수치로 계속 재평가하는 개념이다.

진행 모델
---------
균열 진행은 초기에 빠르고 이후 완만해지는 경우가 많아 멱함수가 자주 맞는다.
표본이 3점 이상이면 선형/멱함수를 모두 적합해 잔차가 작은 쪽을 채택한다.
표본이 2점이면 선형만 쓰고, 1점이면 진행률을 산출하지 않는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..domain import ALLOWABLE_CRACK_WIDTH_MM, Environment

DAYS_PER_YEAR = 365.25


@dataclass
class ProgressionPoint:
    at: datetime
    width_mm: float
    inspection_id: int | None = None


@dataclass
class ProgressionResult:
    """균열 1개의 진행 분석 결과."""

    track_id: int
    label: str
    member_code: str
    points: list[ProgressionPoint]
    model: str = "insufficient"          # linear | power | insufficient
    rate_mm_per_year: float | None = None
    r_squared: float | None = None
    latest_width_mm: float | None = None
    allowable_mm: float | None = None
    years_to_allowable: float | None = None
    forecast: list[tuple[str, float]] = field(default_factory=list)
    verdict: str = ""


def _years_since(base: datetime, at: datetime) -> float:
    return (at - base).total_seconds() / (DAYS_PER_YEAR * 86400.0)


def _fit_linear(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """최소자승 직선 적합 → (기울기, 절편, R^2)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx > 1e-12 else 0.0
    intercept = my - slope * mx

    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0
    return slope, intercept, r2


def analyze_progression(
    track_id: int,
    label: str,
    member_code: str,
    points: list[ProgressionPoint],
    environment: Environment = Environment.HUMID,
    horizon_years: tuple[float, ...] = (1.0, 3.0, 5.0),
) -> ProgressionResult:
    """균열 폭 이력에서 진행 속도와 허용폭 도달 시점을 추정한다."""
    pts = sorted(points, key=lambda p: p.at)
    allowable = ALLOWABLE_CRACK_WIDTH_MM[environment]
    res = ProgressionResult(
        track_id=track_id,
        label=label,
        member_code=member_code,
        points=pts,
        allowable_mm=allowable,
        latest_width_mm=pts[-1].width_mm if pts else None,
    )

    if len(pts) < 2:
        res.verdict = "표본 부족 — 진행 판단을 위해 최소 2회차 측정이 필요합니다"
        return res

    base = pts[0].at
    xs = [_years_since(base, p.at) for p in pts]
    ys = [p.width_mm for p in pts]

    slope, intercept, r2 = _fit_linear(xs, ys)
    res.model, res.rate_mm_per_year, res.r_squared = "linear", slope, r2
    predict = lambda t: intercept + slope * t  # noqa: E731

    # 3점 이상이면 멱함수도 시도한다: w = c * (t + t0)^b
    if len(pts) >= 3 and min(ys) > 0:
        t0 = 0.5  # 최초 관측 이전의 경과기간 가정 (0 나눗셈 방지)
        lx = [math.log(x + t0) for x in xs]
        ly = [math.log(y) for y in ys]
        b, log_c, r2_pow = _fit_linear(lx, ly)
        if r2_pow > r2 and b > 0:
            c = math.exp(log_c)
            res.model, res.r_squared = "power", r2_pow
            predict = lambda t: c * (t + t0) ** b  # noqa: E731
            # 최근 시점의 순간 진행률
            t_last = xs[-1]
            res.rate_mm_per_year = c * b * (t_last + t0) ** (b - 1)

    latest = ys[-1]
    t_last = xs[-1]

    if latest >= allowable:
        res.years_to_allowable = 0.0
        res.verdict = (
            f"이미 허용균열폭({allowable:.2f}mm)을 초과 — 보수 대상입니다"
        )
    elif res.rate_mm_per_year and res.rate_mm_per_year > 1e-6:
        # 예측식을 전방 탐색해 허용폭 도달 시점을 찾는다
        t = t_last
        for _ in range(int(50 / 0.05)):
            t += 0.05
            if predict(t) >= allowable:
                break
        else:
            t = float("inf")
        res.years_to_allowable = None if math.isinf(t) else round(t - t_last, 2)
        if res.years_to_allowable is None:
            res.verdict = "진행이 매우 완만 — 정기점검 주기 유지"
        elif res.years_to_allowable <= 1.0:
            res.verdict = (
                f"1년 이내 허용폭 도달 예상 — 정밀안전진단 및 보수계획 수립 필요"
            )
        elif res.years_to_allowable <= 3.0:
            res.verdict = "3년 이내 허용폭 도달 예상 — 차기 정밀점검 시 중점관찰"
        else:
            res.verdict = "진행 완만 — 경과관찰"
    else:
        res.verdict = "진행 정지 또는 감소 — 경과관찰"

    res.forecast = [
        (
            (pts[-1].at + timedelta(days=int(h * DAYS_PER_YEAR))).date().isoformat(),
            round(max(predict(t_last + h), 0.0), 3),
        )
        for h in horizon_years
    ]
    return res


# ─── 실시간 건전성 지수 (MTM) ─────────────────────────────────
@dataclass
class HealthMark:
    """어느 한 시점의 건전성 평가 — 시세처럼 계속 갱신된다."""

    ts: datetime
    index: float             # 0~100, 높을수록 건전
    grade: str
    contributors: dict[str, float] = field(default_factory=dict)
    delta: float = 0.0


def defect_index_to_health(defect_index: float) -> float:
    """결함도 지수(0에 가까울수록 양호) → 건전성 지수(100에 가까울수록 양호)."""
    return round(max(0.0, min(100.0, 100.0 * (1.0 - defect_index))), 2)


def health_grade(index: float) -> str:
    """건전성 지수를 안전등급 문자로 환산 (SAFETY_GRADE_THRESHOLDS와 정합)."""
    if index >= 88.0:
        return "A"
    if index >= 75.0:
        return "B"
    if index >= 60.0:
        return "C"
    if index >= 40.0:
        return "D"
    return "E"


def sensor_stress(value: float, warn: float | None, critical: float | None) -> float:
    """계측값을 0~1 스트레스로 정규화한다.

    경보 임계 이하는 0에 가깝고, 위험 임계에서 1이 된다. 임계가 없으면 0.
    """
    if warn is None or critical is None or critical <= warn:
        return 0.0
    if value <= warn:
        return max(0.0, 0.35 * (value / warn)) if warn > 0 else 0.0
    return min(1.0, 0.35 + 0.65 * (value - warn) / (critical - warn))


def mark_to_market(
    base_defect_index: float,
    sensor_values: dict[str, tuple[float, float | None, float | None]],
    previous: HealthMark | None = None,
    sensor_weight: float = 0.45,
) -> HealthMark:
    """점검 기반 결함도에 실시간 계측 스트레스를 얹어 건전성을 재평가한다.

    점검은 수개월 단위로만 갱신되므로 그 사이의 변화는 계측이 메운다.
    두 정보원을 가중 결합해 "지금 이 순간"의 지수를 만든다.
    """
    stresses = {
        code: sensor_stress(v, warn, crit)
        for code, (v, warn, crit) in sensor_values.items()
    }
    # 최악 채널이 지배하되 전체 평균도 반영한다 (단일 채널 이상을 놓치지 않기 위함)
    if stresses:
        worst = max(stresses.values())
        mean = sum(stresses.values()) / len(stresses)
        sensor_term = 0.7 * worst + 0.3 * mean
    else:
        sensor_term = 0.0

    combined = (1.0 - sensor_weight) * base_defect_index + sensor_weight * sensor_term
    index = defect_index_to_health(combined)
    now = datetime.now()
    return HealthMark(
        ts=now,
        index=index,
        grade=health_grade(index),
        contributors={
            "inspection": round((1.0 - sensor_weight) * base_defect_index, 4),
            "sensors": round(sensor_weight * sensor_term, 4),
            **{f"ch:{k}": round(v, 4) for k, v in stresses.items()},
        },
        delta=round(index - previous.index, 2) if previous else 0.0,
    )
