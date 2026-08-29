"""건축물 건강검진 표준 (BHC-STD-2026:0.9) 판정 엔진.

시설물안전법의 A~E 안전등급은 이산적이라 "C에서 C로" 머무는 동안 진행된 악화를
드러내지 못한다. 본 표준은 그 산출물을 입력으로 받아 **연속 건강지수(BHI)**,
**건강나이(BHA)**, **폐루프 사후관리(CAPA)** 를 부가한다. 법정 등급을 대체하지
않으며, 두 값을 나란히 표기하고 차이 사유를 기술한다.

구현 범위 (표준 조항 대응)
--------------------------
* §4.2  6대 계통 S1~S6과 가중치, 하한 제약
* §8.1  결함 심각도 D1~D5와 항목점수
* §8.2  확산도 보정 (D5 제외)
* §8.3  계통 건강점수 — 중요도 가중평균, D5 상한 30
* §8.4  종합 건강지수 BHI
* §8.5  적신호 RF-1~RF-8 (가중합 이후 강제 적용)
* §8.6  종합등급 A~E
* §8.7  건강나이 BHA와 노화편차 (참고지표)
* §8.8  열화속도 v
* §9.3  처방 P0~P4
* §10   CAPA 상태 모델과 에스컬레이션
* §11.1 식별자 체계

주의
----
§8.1의 균열폭 임계값은 표준 초안이 "확정 전 재보정 대상"으로 명시한 값이다.
보통 노출환경의 실내 RC 부재를 전제하며, 환경등급별 보정은 노출환경 계수로
반영한다(`severity_from_crack_width` 의 `environment` 인자).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from .domain import ALLOWABLE_CRACK_WIDTH_MM, DefectType, Environment

STANDARD_ID = "BHC-STD-2026:0.9"


# ─── §4.2 6대 계통 ─────────────────────────────────────────────
class System(str, Enum):
    S1 = "S1"  # 구조계통 — 골격계
    S2 = "S2"  # 외피계통 — 피부계
    S3 = "S3"  # 순환계통 — 순환계
    S4 = "S4"  # 방호계통 — 신경·면역계
    S5 = "S5"  # 대사계통 — 대사계
    S6 = "S6"  # 이력계통 — 문진·병력


@dataclass(frozen=True)
class SystemSpec:
    code: System
    label_ko: str
    human_analogue: str
    scope: str
    weight: float
    weight_floor: float | None = None


SYSTEMS: dict[System, SystemSpec] = {
    System.S1: SystemSpec(
        System.S1, "구조계통", "골격계",
        "기초·기둥·보·슬래브·내력벽·계단·옹벽·지하구조", 0.32, weight_floor=0.30,
    ),
    System.S2: SystemSpec(
        System.S2, "외피계통", "피부계",
        "외벽마감·창호·지붕방수·지하방수·실링·단열", 0.14,
    ),
    System.S3: SystemSpec(
        System.S3, "순환계통", "순환계",
        "급수·급탕·배수·오수·난방·환기·승강기", 0.12,
    ),
    System.S4: SystemSpec(
        System.S4, "방호계통", "신경·면역계",
        "전기·피뢰·소방설비·방화구획·피난동선·비상조명", 0.24, weight_floor=0.20,
    ),
    System.S5: SystemSpec(
        System.S5, "대사계통", "대사계",
        "에너지 사용량·결로·곰팡이·실내공기질·열교", 0.06,
    ),
    System.S6: SystemSpec(
        System.S6, "이력계통", "문진·병력",
        "도면 정합성·보수이력·민원이력·법정점검 이행·관리계획", 0.12,
    ),
}

# 부재코드 → 계통. 구조부재는 S1, 마감·방수는 S2로 간다.
MEMBER_TO_SYSTEM: dict[str, System] = {
    "column": System.S1,
    "girder": System.S1,
    "slab": System.S1,
    "wall_shear": System.S1,
    "foundation": System.S1,
    "retaining_wall": System.S1,
    "wall_non": System.S2,
    "parapet": System.S2,
    "finish": System.S2,
}

# 부재 중요도 v — §8.3 (1 보통 · 2 중요 · 3 핵심)
MEMBER_IMPORTANCE: dict[str, int] = {
    "column": 3,
    "girder": 3,
    "foundation": 3,
    "wall_shear": 3,
    "slab": 2,
    "retaining_wall": 2,
    "wall_non": 1,
    "parapet": 1,
    "finish": 1,
}
DEFAULT_IMPORTANCE = 1

# 미실시 항목의 처리 — §7.2에 따라 D3 상당으로 본다
NOT_PERFORMED_SCORE = 65.0


# ─── §8.1 결함 심각도 ──────────────────────────────────────────
class Severity(str, Enum):
    D1 = "D1"  # 정상
    D2 = "D2"  # 관찰
    D3 = "D3"  # 주의
    D4 = "D4"  # 위험
    D5 = "D5"  # 긴급


SEVERITY_SCORE: dict[Severity, float] = {
    Severity.D1: 100.0,
    Severity.D2: 85.0,
    Severity.D3: 65.0,
    Severity.D4: 40.0,
    Severity.D5: 10.0,
}

SEVERITY_LABELS_KO: dict[Severity, str] = {
    Severity.D1: "정상",
    Severity.D2: "관찰",
    Severity.D3: "주의",
    Severity.D4: "위험",
    Severity.D5: "긴급",
}

SEVERITY_ORDER = [Severity.D1, Severity.D2, Severity.D3, Severity.D4, Severity.D5]

# §8.1 균열폭 임계 (보통 노출환경 실내 RC 전제). 상한 미만이면 해당 등급.
CRACK_WIDTH_SEVERITY: list[tuple[float, Severity]] = [
    (0.10, Severity.D1),
    (0.30, Severity.D2),
    (0.50, Severity.D3),
    (1.00, Severity.D4),
    (float("inf"), Severity.D5),
]

# 균열 외 결함의 면적률 기준 — 표준이 계통별 예시만 두어, 운용값으로 정한다.
AREA_RATIO_SEVERITY: list[tuple[float, Severity]] = [
    (0.005, Severity.D1),
    (0.020, Severity.D2),
    (0.080, Severity.D3),
    (0.200, Severity.D4),
    (float("inf"), Severity.D5),
]

# 관측만으로 이미 진행된 손상을 뜻하는 결함의 심각도 하한.
MINIMUM_SEVERITY: dict[DefectType, Severity] = {
    DefectType.REBAR_EXPOSURE: Severity.D4,   # §8.1 D4 예시: 철근 노출 및 부식
    DefectType.SPALLING: Severity.D3,         # §8.1 D3 예시: 철근노출 없는 박리
}


def _worse(a: Severity, b: Severity) -> Severity:
    return a if SEVERITY_ORDER.index(a) >= SEVERITY_ORDER.index(b) else b


def severity_from_crack_width(
    width_mm: float, environment: Environment = Environment.HUMID
) -> Severity:
    """균열폭 → 심각도.

    표준 §8.1의 폭 기준은 보통 노출환경 전제다. 부식성 환경에서는 같은 폭이라도
    철근 부식 위험이 커지므로, 허용균열폭 비(습윤환경 대비)로 폭을 환산해
    엄격하게 판정한다. 이것이 표준이 요구한 "환경등급별 보정"의 구현이다.
    """
    ref = ALLOWABLE_CRACK_WIDTH_MM[Environment.HUMID]
    allowable = ALLOWABLE_CRACK_WIDTH_MM[environment]
    effective = width_mm * (ref / allowable) if allowable > 0 else width_mm

    for upper, sev in CRACK_WIDTH_SEVERITY:
        if effective < upper:
            return sev
    return Severity.D5


def severity_from_area_ratio(area_ratio: float) -> Severity:
    for upper, sev in AREA_RATIO_SEVERITY:
        if area_ratio < upper:
            return sev
    return Severity.D5


def classify_severity(
    defect_type: DefectType,
    *,
    width_mm: float | None = None,
    area_ratio: float | None = None,
    environment: Environment = Environment.HUMID,
) -> tuple[Severity, str]:
    """결함 1건의 심각도와 판정 근거 문자열."""
    candidates: list[Severity] = []
    reasons: list[str] = []

    if width_mm is not None:
        candidates.append(severity_from_crack_width(width_mm, environment))
        reasons.append(f"균열폭 {width_mm:.2f}mm")
    if area_ratio is not None:
        candidates.append(severity_from_area_ratio(area_ratio))
        reasons.append(f"면적률 {area_ratio * 100:.2f}%")
    if not candidates:
        candidates.append(Severity.D3)
        reasons.append("정량값 미측정 — 미실시 항목 처리")

    sev = candidates[0]
    for c in candidates[1:]:
        sev = _worse(sev, c)

    floor = MINIMUM_SEVERITY.get(defect_type)
    if floor is not None and _worse(sev, floor) is not sev:
        sev = floor
        reasons.append(f"{defect_type.value} 심각도 하한 적용")

    return sev, " · ".join(reasons)


# ─── §8.2 확산도 보정 ──────────────────────────────────────────
def adjusted_score(severity: Severity, extent: float) -> float:
    """s' = 100 − (100 − s)(0.5 + 0.5ρ).

    D5에는 적용하지 않는다 — 붕괴 위험은 1개소만으로 성립하기 때문이다(§8.2).
    """
    s = SEVERITY_SCORE[severity]
    if severity is Severity.D5:
        return s
    rho = min(max(extent, 0.0), 1.0)
    return 100.0 - (100.0 - s) * (0.5 + 0.5 * rho)


# ─── 결함 입력 ─────────────────────────────────────────────────
@dataclass
class DefectObservation:
    """계통 점수 산정에 들어가는 결함 1건."""

    defect_type: DefectType
    member_code: str
    severity: Severity
    extent: float = 0.0          # ρ — 동종 부재 중 결함 발현 비율
    width_mm: float | None = None
    area_ratio: float | None = None
    basis: str = ""
    location: str = ""
    defect_id: str = ""

    @property
    def system(self) -> System:
        return MEMBER_TO_SYSTEM.get(self.member_code, System.S1)

    @property
    def importance(self) -> int:
        return MEMBER_IMPORTANCE.get(self.member_code, DEFAULT_IMPORTANCE)

    @property
    def score(self) -> float:
        return adjusted_score(self.severity, self.extent)


# ─── §8.3 계통 건강점수 ────────────────────────────────────────
@dataclass
class SystemScore:
    system: System
    label_ko: str
    score: float
    weight: float
    defect_count: int
    worst_severity: Severity
    capped_by_d5: bool = False
    performed: bool = True


def system_scores(
    observations: list[DefectObservation],
    *,
    performed_systems: set[System] | None = None,
    weights: dict[System, float] | None = None,
) -> list[SystemScore]:
    """계통별 건강점수. 결함이 없는 계통은 100점, 미실시 계통은 65점."""
    w = weights or {s: spec.weight for s, spec in SYSTEMS.items()}
    performed = performed_systems if performed_systems is not None else set(SYSTEMS)

    grouped: dict[System, list[DefectObservation]] = {s: [] for s in SYSTEMS}
    for o in observations:
        grouped[o.system].append(o)

    out: list[SystemScore] = []
    for sys_code, spec in SYSTEMS.items():
        obs = grouped[sys_code]
        is_performed = sys_code in performed

        if not is_performed:
            score, worst, capped = NOT_PERFORMED_SCORE, Severity.D3, False
        elif not obs:
            score, worst, capped = 100.0, Severity.D1, False
        else:
            num = sum(o.importance * o.score for o in obs)
            den = sum(o.importance for o in obs)
            score = num / den if den else 100.0
            worst = obs[0].severity
            for o in obs[1:]:
                worst = _worse(worst, o.severity)
            # §8.3 — 계통 내 D5가 1건이라도 있으면 계통점수는 30을 넘을 수 없다
            capped = worst is Severity.D5 and score > 30.0
            if worst is Severity.D5:
                score = min(score, 30.0)

        out.append(
            SystemScore(
                system=sys_code,
                label_ko=spec.label_ko,
                score=round(score, 1),
                weight=w.get(sys_code, spec.weight),
                defect_count=len(obs),
                worst_severity=worst,
                capped_by_d5=capped,
                performed=is_performed,
            )
        )
    return out


def validate_weights(weights: dict[System, float]) -> list[str]:
    """§4.2 가중치 제약 검증 — 합 1.00, S1 ≥ 0.30, S4 ≥ 0.20."""
    problems: list[str] = []
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        problems.append(f"가중치 합이 1.00이 아닙니다 (현재 {total:.4f})")
    for code, spec in SYSTEMS.items():
        if spec.weight_floor is not None and weights.get(code, 0.0) < spec.weight_floor:
            problems.append(
                f"{code.value} {spec.label_ko} 가중치는 {spec.weight_floor:.2f} 미만으로 "
                f"낮출 수 없습니다 (현재 {weights.get(code, 0.0):.2f})"
            )
    return problems


# ─── §8.5 적신호 ───────────────────────────────────────────────
@dataclass(frozen=True)
class RedFlagSpec:
    code: str
    condition_ko: str
    bhi_cap: float
    forced_grade: str


RED_FLAGS: dict[str, RedFlagSpec] = {
    "RF-1": RedFlagSpec("RF-1", "어느 계통에서든 D5 결함 1건 이상", 39.9, "E"),
    "RF-2": RedFlagSpec("RF-2", "S1 구조계통에서 D4 결함 1건 이상", 59.9, "D"),
    "RF-3": RedFlagSpec(
        "RF-3", "소방 주요설비(수신기·가압송수·스프링클러) 기능정지 또는 임의 차단", 49.9, "E"
    ),
    "RF-4": RedFlagSpec("RF-4", "피난계단·피난동선이 폐쇄·잠금·적치로 미확보", 49.9, "E"),
    "RF-5": RedFlagSpec(
        "RF-5", "방화구획 관통부 미충전 또는 방화문 폐쇄불능이 전체 구획의 30% 이상", 59.9, "D"
    ),
    "RF-6": RedFlagSpec("RF-6", "구조검토 없이 시행된 내력벽 제거·하중 증가 확인", 59.9, "D"),
    "RF-7": RedFlagSpec("RF-7", "직전 검진의 P0 처방이 기한 초과 미이행", 64.9, "C"),
    "RF-8": RedFlagSpec(
        "RF-8", "가연성 외장재가 확인되고 피난·소방 결함이 동시에 존재", 49.9, "E"
    ),
}


@dataclass
class RedFlagHit:
    code: str
    condition_ko: str
    bhi_cap: float
    forced_grade: str
    evidence: list[str] = field(default_factory=list)


def detect_red_flags(
    observations: list[DefectObservation],
    *,
    manual_flags: dict[str, list[str]] | None = None,
) -> list[RedFlagHit]:
    """자동 판정 가능한 적신호(RF-1·RF-2)와 수동 입력 적신호를 합친다.

    RF-3~RF-8은 영상만으로 판정할 수 없다(소방설비 기능정지, 피난동선 적치,
    내력벽 제거 등). 점검자가 현장에서 확인해 `manual_flags` 로 넘긴다.
    자동으로 채울 수 없는 항목을 채운 척하지 않는 것이 이 함수의 요점이다.
    """
    hits: list[RedFlagHit] = []

    d5 = [o for o in observations if o.severity is Severity.D5]
    if d5:
        hits.append(
            RedFlagHit(
                **{k: getattr(RED_FLAGS["RF-1"], k)
                   for k in ("code", "condition_ko", "bhi_cap", "forced_grade")},
                evidence=[
                    f"{o.defect_id or o.member_code}: {o.basis}" for o in d5[:5]
                ],
            )
        )

    s1_d4 = [
        o for o in observations
        if o.system is System.S1 and o.severity is Severity.D4
    ]
    if s1_d4:
        hits.append(
            RedFlagHit(
                **{k: getattr(RED_FLAGS["RF-2"], k)
                   for k in ("code", "condition_ko", "bhi_cap", "forced_grade")},
                evidence=[
                    f"{o.defect_id or o.member_code}: {o.basis}" for o in s1_d4[:5]
                ],
            )
        )

    for code, evidence in (manual_flags or {}).items():
        spec = RED_FLAGS.get(code)
        if spec is None:
            continue
        hits.append(
            RedFlagHit(
                code=spec.code,
                condition_ko=spec.condition_ko,
                bhi_cap=spec.bhi_cap,
                forced_grade=spec.forced_grade,
                evidence=list(evidence),
            )
        )
    return hits


# ─── §8.6 종합등급 ─────────────────────────────────────────────
BHI_GRADE_BOUNDS: list[tuple[float, str, str]] = [
    (90.0, "A", "우수"),
    (80.0, "B", "양호"),
    (65.0, "C", "보통"),
    (50.0, "D", "미흡"),
    (0.0, "E", "불량"),
]


def grade_from_bhi(bhi: float) -> tuple[str, str]:
    for lower, grade, label in BHI_GRADE_BOUNDS:
        if bhi >= lower:
            return grade, label
    return "E", "불량"


# ─── §8.7 건강나이 ─────────────────────────────────────────────
COHORT_BETA: dict[str, float] = {
    "rc_apartment": 1.2,       # RC 공동주택 (기본값)
    "office": 1.1,             # 업무시설·근린생활시설
    "school": 1.3,             # 학교·공공청사
    "masonry": 2.0,            # 조적조·무근 구조
    "open_parking": 1.7,       # 개방형 주차장·외기 노출
}
COHORT_LABELS_KO: dict[str, str] = {
    "rc_apartment": "RC 공동주택",
    "office": "업무·근린생활시설",
    "school": "학교·공공청사",
    "masonry": "조적조·무근 구조",
    "open_parking": "개방형 주차장·외기노출",
}
DEFAULT_COHORT = "rc_apartment"


def cohort_of(structure_type: str, facility_class: str = "") -> str:
    """구조형식 문자열에서 코호트를 추정한다."""
    s = (structure_type or "") + " " + (facility_class or "")
    if "조적" in s or "무근" in s:
        return "masonry"
    if "주차" in s or "옹벽" in s:
        return "open_parking"
    if "학교" in s or "청사" in s:
        return "school"
    if "업무" in s or "근린" in s or "창고" in s or "PC" in s:
        return "office"
    return DEFAULT_COHORT


@dataclass
class HealthAge:
    bha_years: float
    actual_years: float | None
    deviation: float | None       # Δ = BHA − 실제경과연수
    beta: float
    cohort: str
    cohort_label_ko: str
    interpretation: str
    advisory_only: bool = True    # §8.7 — 표본 30건 미만이면 참고지표


def health_age(
    bhi: float, actual_years: float | None, cohort: str = DEFAULT_COHORT
) -> HealthAge:
    """BHA = (100 − BHI) / β, Δ = BHA − 실제경과연수."""
    beta = COHORT_BETA.get(cohort, COHORT_BETA[DEFAULT_COHORT])
    bha = max(0.0, (100.0 - bhi) / beta)
    delta = None if actual_years is None else bha - actual_years

    if delta is None:
        interp = "실제 경과연수를 알 수 없어 노화편차를 산출하지 않았습니다"
    elif delta <= -5.0:
        interp = "건강노화 — 관리 성과가 코호트 평균을 상회합니다"
    elif delta < 5.0:
        interp = "정상노화 — 코호트 평균 수준입니다"
    elif delta < 10.0:
        interp = "가속노화 — 원인 규명 및 관리방식 재검토가 필요합니다"
    else:
        interp = "조기노화 경보 — 심화검진(L2)으로 승급하는 것이 좋습니다"

    return HealthAge(
        bha_years=round(bha, 1),
        actual_years=actual_years,
        deviation=None if delta is None else round(delta, 1),
        beta=beta,
        cohort=cohort,
        cohort_label_ko=COHORT_LABELS_KO.get(cohort, cohort),
        interpretation=interp,
    )


# ─── §8.8 열화속도 ─────────────────────────────────────────────
@dataclass
class DeteriorationRate:
    value: float | None           # v = (BHI_직전 − BHI_금회) / Δt
    baseline: bool
    verdict: str
    action: str


def deterioration_rate(
    bhi_now: float,
    bhi_prev: float | None,
    years_elapsed: float | None,
    beta: float,
) -> DeteriorationRate:
    if bhi_prev is None or not years_elapsed or years_elapsed <= 0:
        return DeteriorationRate(None, True, "기준선", "최초 검진 — 열화속도를 산출하지 않습니다")

    v = (bhi_prev - bhi_now) / years_elapsed
    if v <= 0:
        verdict, action = "개선", "보수 효과 확인 · 사례 기록"
    elif v <= beta:
        verdict, action = "정상 열화", "검진 주기 유지"
    elif v <= 3.0:
        verdict, action = "가속 열화", "원인 계통 특정 · 다음 검진 주기 1단계 단축"
    else:
        verdict, action = "급속 열화 경보", "등급과 무관하게 심화검진 즉시 실시"

    return DeteriorationRate(round(v, 2), False, verdict, action)


# ─── §9.3 처방 ─────────────────────────────────────────────────
class Priority(str, Enum):
    P0 = "P0"  # 응급
    P1 = "P1"  # 긴급
    P2 = "P2"  # 계획보수
    P3 = "P3"  # 중장기
    P4 = "P4"  # 추적관찰


@dataclass(frozen=True)
class PrioritySpec:
    code: Priority
    label_ko: str
    trigger_ko: str
    due_days: int          # 조치 기한 (P0는 1일 = 24시간 이내 착수)
    due_text: str
    notify_ko: str


PRIORITIES: dict[Priority, PrioritySpec] = {
    Priority.P0: PrioritySpec(
        Priority.P0, "응급", "D5 결함 · 적신호 RF-1·RF-3·RF-4",
        1, "인지 즉시 응급조치, 24시간 이내 착수", "현장 구두 통보 후 당일 서면 통보",
    ),
    Priority.P1: PrioritySpec(
        Priority.P1, "긴급", "D4 결함 · 적신호 RF-2·RF-5·RF-6·RF-8",
        30, "30일 이내", "소견서 교부 시 별도 표지",
    ),
    Priority.P2: PrioritySpec(
        Priority.P2, "계획보수", "D3 결함", 90, "90일 이내", "소견서",
    ),
    Priority.P3: PrioritySpec(
        Priority.P3, "중장기", "D3 중 대규모·예산 수반",
        365, "1년 이내 (장기수선계획 반영)", "소견서 · 장기수선계획 연계",
    ),
    Priority.P4: PrioritySpec(
        Priority.P4, "추적관찰", "D2 결함", 0, "차기 검진 시 재확인", "소견서",
    ),
}

SEVERITY_TO_PRIORITY: dict[Severity, Priority | None] = {
    Severity.D5: Priority.P0,
    Severity.D4: Priority.P1,
    Severity.D3: Priority.P2,
    Severity.D2: Priority.P4,
    Severity.D1: None,
}

RED_FLAG_PRIORITY: dict[str, Priority] = {
    "RF-1": Priority.P0, "RF-3": Priority.P0, "RF-4": Priority.P0,
    "RF-2": Priority.P1, "RF-5": Priority.P1, "RF-6": Priority.P1,
    "RF-8": Priority.P1, "RF-7": Priority.P1,
}


def priority_for(severity: Severity, *, large_scale: bool = False) -> Priority | None:
    """심각도 → 처방 우선순위. D3 중 대규모·예산 수반은 P3."""
    p = SEVERITY_TO_PRIORITY[severity]
    if p is Priority.P2 and large_scale:
        return Priority.P3
    return p


def due_date(priority: Priority, issued: date) -> date | None:
    spec = PRIORITIES[priority]
    if priority is Priority.P4:
        return None            # 차기 검진 시 재확인 — 고정 기한이 없다
    return issued + timedelta(days=spec.due_days)


# ─── §10 CAPA 폐루프 ───────────────────────────────────────────
class CapaState(str, Enum):
    ISSUED = "issued"                # 발행
    ACKNOWLEDGED = "acknowledged"    # 접수
    PLANNED = "planned"              # 계획
    EXECUTED = "executed"            # 이행
    VERIFIED = "verified"            # 검증
    CLOSED = "closed"                # 종결
    ESCALATED = "escalated"          # 에스컬레이션


CAPA_LABELS_KO: dict[CapaState, str] = {
    CapaState.ISSUED: "발행",
    CapaState.ACKNOWLEDGED: "접수",
    CapaState.PLANNED: "계획",
    CapaState.EXECUTED: "이행",
    CapaState.VERIFIED: "검증",
    CapaState.CLOSED: "종결",
    CapaState.ESCALATED: "에스컬레이션",
}

# §10.1 — 상태 전이는 근거 기록 없이 발생할 수 없다. 필수 증빙을 함께 둔다.
CAPA_TRANSITIONS: dict[CapaState, list[CapaState]] = {
    CapaState.ISSUED: [CapaState.ACKNOWLEDGED, CapaState.ESCALATED],
    CapaState.ACKNOWLEDGED: [CapaState.PLANNED, CapaState.ESCALATED],
    CapaState.PLANNED: [CapaState.EXECUTED, CapaState.ESCALATED],
    CapaState.EXECUTED: [CapaState.VERIFIED, CapaState.ESCALATED],
    CapaState.VERIFIED: [CapaState.CLOSED, CapaState.PLANNED],  # 부적정 시 재시공
    CapaState.ESCALATED: [CapaState.PLANNED],
    CapaState.CLOSED: [],
}

CAPA_EVIDENCE_KO: dict[CapaState, str] = {
    CapaState.ACKNOWLEDGED: "담당자 · 접수 일시",
    CapaState.PLANNED: "공사 계획 · 예산 근거",
    CapaState.EXECUTED: "시공 전·중·후 사진 · 자재 성적서",
    CapaState.VERIFIED: "검증자 서명 · 재측정값",
    CapaState.CLOSED: "종결 일시 · 종결자",
    CapaState.ESCALATED: "초과 사유 · 통지 이력",
}


def can_transition(current: CapaState, target: CapaState) -> bool:
    return target in CAPA_TRANSITIONS.get(current, [])


@dataclass
class Escalation:
    level: str
    trigger_ko: str
    action_ko: str


def escalation_for(
    priority: Priority, state: CapaState, days_overdue: int, days_since_issue: int
) -> Escalation | None:
    """§10.3 에스컬레이션 E1~E4."""
    if state in (CapaState.CLOSED, CapaState.VERIFIED):
        return None

    if priority is Priority.P0 and days_since_issue > 7 and state in (
        CapaState.ISSUED, CapaState.ACKNOWLEDGED
    ):
        return Escalation(
            "E4", "P0 처방 7일 초과 미착수",
            "관계 행정기관 통보 검토 · 사용제한 권고 서면 발행",
        )
    if days_overdue >= 30:
        return Escalation(
            "E3", "기한 초과 30일", "차기 검진 S6에 D4 결함 계상 · 적신호 RF-7 검토"
        )
    if days_overdue >= 15 or (priority is Priority.P0 and days_since_issue >= 1):
        return Escalation(
            "E2", "기한 초과 15일 또는 P0 24시간 초과",
            "관리주체 대표에게 서면 통지 · 사유서 요구",
        )
    if days_overdue >= 1:
        return Escalation("E1", "기한 초과 1일", "담당자 및 관리책임자에게 자동 통지")
    return None


# ─── §11.1 식별자 ──────────────────────────────────────────────
CHECKUP_KINDS: dict[str, str] = {
    "BAS": "기본검진", "ADV": "심화검진", "TGT": "표적검진", "FUP": "추적검진",
}
CHECKUP_LEVELS: dict[str, str] = {
    "L1": "기본검진 (Screening)",
    "L2": "심화검진 (Diagnostic)",
    "L3": "정밀검진 (Definitive)",
}
KIND_TO_LEVEL: dict[str, str] = {"BAS": "L1", "ADV": "L2", "TGT": "L2", "FUP": "L1"}

DEFECT_TYPE_CODE: dict[DefectType, str] = {
    DefectType.CRACK: "CRK",
    DefectType.SPALLING: "SPL",
    DefectType.EFFLORESCENCE: "EFL",
    DefectType.LEAKAGE: "LEK",
    DefectType.REBAR_EXPOSURE: "RBR",
    DefectType.SEGREGATION: "SEG",
    DefectType.DAMAGE: "DMG",
}
MEMBER_CODE_ABBR: dict[str, str] = {
    "column": "COL", "girder": "GIR", "slab": "SLB", "wall_shear": "SHW",
    "foundation": "FND", "wall_non": "WAL", "parapet": "PRP",
    "finish": "FIN", "retaining_wall": "RTW",
}


def checkup_id(building_ref: str, year: int, kind: str, serial: int) -> str:
    """BHC-{건축물관리번호}-{검진연도}-{종류코드}-{일련번호}."""
    return f"BHC-{building_ref}-{year}-{kind}-{serial:03d}"


def defect_id(system: System, member_code: str, dtype: DefectType, serial: int) -> str:
    """{계통}-{부위}-{유형}-{일련번호}  예) S1-COL-CRK-0012."""
    return (
        f"{system.value}-{MEMBER_CODE_ABBR.get(member_code, 'ETC')}-"
        f"{DEFECT_TYPE_CODE.get(dtype, 'ETC')}-{serial:04d}"
    )


# ─── 종합 판정 ─────────────────────────────────────────────────
@dataclass
class PrescriptionDraft:
    defect_id: str
    system: System
    member_code: str
    defect_type: DefectType
    severity: Severity
    priority: Priority
    due_text: str
    due_date: date | None
    action_ko: str
    basis_ko: str


@dataclass
class CheckupResult:
    """검진 1회의 종합 판정 — 소견서 제1부 요약에 그대로 대응한다."""

    standard: str
    bhi: float
    bhi_raw: float                       # 적신호 적용 전
    grade: str
    grade_label_ko: str
    systems: list[SystemScore]
    red_flags: list[RedFlagHit]
    health_age: HealthAge
    rate: DeteriorationRate
    prescriptions: list[PrescriptionDraft]
    defect_count: int
    severity_counts: dict[str, int]
    p0_count: int
    p1_count: int
    weight_problems: list[str] = field(default_factory=list)


REPAIR_ACTION_KO: dict[Severity, str] = {
    Severity.D5: "응급 안전조치(가설 지지·출입통제) 후 구조 검토 및 보강 설계",
    Severity.D4: "단면복구 및 방청처리, 시공 전 철근 부식도 확인",
    Severity.D3: "표면 보수 및 방수·보호도장, 진행 여부 재측정",
    Severity.D2: "경과관찰 — 차기 검진 시 동일 위치 재측정",
    Severity.D1: "조치 불요",
}


def assess_checkup(
    observations: list[DefectObservation],
    *,
    actual_years: float | None = None,
    cohort: str = DEFAULT_COHORT,
    bhi_prev: float | None = None,
    years_since_prev: float | None = None,
    performed_systems: set[System] | None = None,
    weights: dict[System, float] | None = None,
    manual_flags: dict[str, list[str]] | None = None,
    issued: date | None = None,
) -> CheckupResult:
    """관측 결함 목록으로부터 BHI·등급·적신호·처방을 산정한다."""
    w = weights or {s: spec.weight for s, spec in SYSTEMS.items()}
    problems = validate_weights(w)

    scores = system_scores(observations, performed_systems=performed_systems, weights=w)
    bhi_raw = sum(s.weight * s.score for s in scores)

    flags = detect_red_flags(observations, manual_flags=manual_flags)
    bhi = bhi_raw
    for f in flags:                       # §8.5 — 복수 조건이면 가장 낮은 상한
        bhi = min(bhi, f.bhi_cap)

    grade, label = grade_from_bhi(bhi)
    ha = health_age(bhi, actual_years, cohort)
    rate = deterioration_rate(bhi, bhi_prev, years_since_prev, ha.beta)

    issued_on = issued or date.today()
    drafts: list[PrescriptionDraft] = []
    for o in observations:
        p = priority_for(o.severity)
        if p is None:
            continue
        drafts.append(
            PrescriptionDraft(
                defect_id=o.defect_id,
                system=o.system,
                member_code=o.member_code,
                defect_type=o.defect_type,
                severity=o.severity,
                priority=p,
                due_text=PRIORITIES[p].due_text,
                due_date=due_date(p, issued_on),
                action_ko=REPAIR_ACTION_KO[o.severity],
                basis_ko=o.basis,
            )
        )
    order = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2, Priority.P3: 3, Priority.P4: 4}
    drafts.sort(key=lambda d: (order[d.priority], d.member_code))

    counts: dict[str, int] = {s.value: 0 for s in SEVERITY_ORDER}
    for o in observations:
        counts[o.severity.value] += 1

    return CheckupResult(
        standard=STANDARD_ID,
        bhi=round(bhi, 1),
        bhi_raw=round(bhi_raw, 1),
        grade=grade,
        grade_label_ko=label,
        systems=scores,
        red_flags=flags,
        health_age=ha,
        rate=rate,
        prescriptions=drafts,
        defect_count=len(observations),
        severity_counts=counts,
        p0_count=sum(1 for d in drafts if d.priority is Priority.P0),
        p1_count=sum(1 for d in drafts if d.priority is Priority.P1),
        weight_problems=problems,
    )
