"""상태평가·안전등급 판정 엔진.

균열 폭 → 부재 상태등급(a~e) → 결함도 지수 → 시설물 종합 안전등급(A~E).
모든 경계값은 domain.py에 상수로 분리되어 있어 지침 개정 시 한 곳만 고치면 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import (
    ALLOWABLE_CRACK_WIDTH_MM,
    CONDITION_GRADE_SCORE,
    DEFAULT_MEMBER_CODE,
    MEMBER_CLASSES,
    SAFETY_GRADE_THRESHOLDS,
    ConditionGrade,
    DefectType,
    Environment,
    SafetyGrade,
)

# 세부지침 콘크리트 균열 상태평가 — 균열폭(mm) 상한과 등급의 대응.
CRACK_WIDTH_GRADE_BOUNDS: list[tuple[float, ConditionGrade]] = [
    (0.10, ConditionGrade.A),
    (0.20, ConditionGrade.B),
    (0.30, ConditionGrade.C),
    (1.00, ConditionGrade.D),
    (float("inf"), ConditionGrade.E),
]

# 균열 외 결함의 상태평가 — 면적률(결함 면적 / 부재 조사면적) 상한.
AREA_RATIO_GRADE_BOUNDS: list[tuple[float, ConditionGrade]] = [
    (0.005, ConditionGrade.A),
    (0.020, ConditionGrade.B),
    (0.100, ConditionGrade.C),
    (0.250, ConditionGrade.D),
    (float("inf"), ConditionGrade.E),
]

# 결함 유형별 심각도 가중치. 철근노출·박락은 내하력에 직결되므로 높게 준다.
DEFECT_SEVERITY_WEIGHT: dict[DefectType, float] = {
    DefectType.CRACK: 1.00,
    DefectType.SPALLING: 1.10,
    DefectType.EFFLORESCENCE: 0.55,
    DefectType.LEAKAGE: 0.70,
    DefectType.REBAR_EXPOSURE: 1.30,
    DefectType.SEGREGATION: 0.80,
    DefectType.DAMAGE: 0.90,
}

# 일부 결함은 관측되는 것 자체가 이미 진행된 손상을 뜻하므로 하한 등급을 둔다.
# 다만 하한을 과하게 잡으면 극소 면적의 결함 하나로 시설물 전체가 D로 떨어져
# 등급이 변별력을 잃는다. 하한은 '보통(c)'까지만 두고, 그 이상은 면적률이
# 결정하도록 한다.
MINIMUM_GRADE_BY_DEFECT: dict[DefectType, ConditionGrade] = {
    DefectType.REBAR_EXPOSURE: ConditionGrade.C,
    DefectType.SPALLING: ConditionGrade.B,
}

_GRADE_ORDER: list[ConditionGrade] = [
    ConditionGrade.A,
    ConditionGrade.B,
    ConditionGrade.C,
    ConditionGrade.D,
    ConditionGrade.E,
]


def _worse(a: ConditionGrade, b: ConditionGrade) -> ConditionGrade:
    """두 등급 중 더 나쁜(뒤쪽) 등급을 반환."""
    return a if _GRADE_ORDER.index(a) >= _GRADE_ORDER.index(b) else b


def grade_by_crack_width(width_mm: float) -> ConditionGrade:
    """균열폭으로 상태등급을 판정한다."""
    for upper, grade in CRACK_WIDTH_GRADE_BOUNDS:
        if width_mm < upper:
            return grade
    return ConditionGrade.E


def grade_by_area_ratio(area_ratio: float) -> ConditionGrade:
    """결함 면적률로 상태등급을 판정한다."""
    for upper, grade in AREA_RATIO_GRADE_BOUNDS:
        if area_ratio < upper:
            return grade
    return ConditionGrade.E


def needs_repair(width_mm: float, environment: Environment) -> bool:
    """KDS 14 20 30 허용균열폭 초과 여부 — 보수 필요 판단의 1차 기준."""
    return width_mm >= ALLOWABLE_CRACK_WIDTH_MM[environment]


@dataclass
class DefectAssessment:
    """결함 1건에 대한 판정 결과."""

    defect_type: DefectType
    grade: ConditionGrade
    severity: float
    repair_required: bool
    basis: str


def assess_defect(
    defect_type: DefectType,
    *,
    width_mm: float | None = None,
    area_ratio: float | None = None,
    environment: Environment = Environment.HUMID,
) -> DefectAssessment:
    """결함 1건을 상태등급으로 환산한다.

    균열은 폭 기준을, 그 외 결함은 면적률 기준을 우선 적용한다.
    두 값이 모두 주어지면 더 나쁜 등급을 채택한다(보수적 판정).
    """
    grades: list[ConditionGrade] = []
    reasons: list[str] = []

    if width_mm is not None:
        grades.append(grade_by_crack_width(width_mm))
        reasons.append(f"균열폭 {width_mm:.2f}mm")
    if area_ratio is not None:
        grades.append(grade_by_area_ratio(area_ratio))
        reasons.append(f"면적률 {area_ratio * 100:.2f}%")

    if not grades:
        # 검출은 되었으나 정량값이 없으면 보통(c)으로 둔다.
        grades.append(ConditionGrade.C)
        reasons.append("정량값 미측정")

    grade = grades[0]
    for g in grades[1:]:
        grade = _worse(grade, g)

    floor_grade = MINIMUM_GRADE_BY_DEFECT.get(defect_type)
    if floor_grade is not None:
        if _worse(grade, floor_grade) is floor_grade and grade is not floor_grade:
            reasons.append(f"{defect_type.value} 최저등급 적용")
        grade = _worse(grade, floor_grade)

    severity = CONDITION_GRADE_SCORE[grade] * DEFECT_SEVERITY_WEIGHT[defect_type]
    repair = (
        needs_repair(width_mm, environment)
        if (defect_type is DefectType.CRACK and width_mm is not None)
        else _GRADE_ORDER.index(grade) >= _GRADE_ORDER.index(ConditionGrade.C)
    )

    return DefectAssessment(
        defect_type=defect_type,
        grade=grade,
        severity=severity,
        repair_required=repair,
        basis=" · ".join(reasons),
    )


@dataclass
class MemberAssessment:
    """부재 단위 집계 결과."""

    member_code: str
    grade: ConditionGrade
    defect_index: float
    defect_count: int


@dataclass
class InspectionAssessment:
    """점검 1회 전체에 대한 종합 판정."""

    safety_grade: SafetyGrade
    defect_index: float
    members: list[MemberAssessment] = field(default_factory=list)
    repair_required_count: int = 0
    total_defects: int = 0


def _grade_from_index(index: float) -> SafetyGrade:
    for upper, grade in SAFETY_GRADE_THRESHOLDS:
        if index < upper:
            return grade
    return SafetyGrade.E


def assess_member(
    member_code: str, assessments: list[DefectAssessment]
) -> MemberAssessment:
    """한 부재에서 나온 결함들을 부재 등급으로 집계한다.

    최악 결함을 지배값으로 두고(0.7), 나머지 결함의 평균을 누적 가산(0.3)한다.
    결함이 많을수록 지수가 올라가되 단일 심각 결함이 희석되지 않도록 한 구성이다.
    """
    if not assessments:
        return MemberAssessment(member_code, ConditionGrade.A, 0.0, 0)

    severities = sorted((a.severity for a in assessments), reverse=True)
    worst = severities[0]
    rest_mean = sum(severities[1:]) / len(severities[1:]) if len(severities) > 1 else 0.0
    index = 0.7 * worst + 0.3 * rest_mean

    grade = assessments[0].grade
    for a in assessments[1:]:
        grade = _worse(grade, a.grade)

    return MemberAssessment(member_code, grade, round(index, 4), len(assessments))


def assess_inspection(
    defects_by_member: dict[str, list[DefectAssessment]],
) -> InspectionAssessment:
    """부재별 결함을 시설물 종합 안전등급으로 환산한다.

    부재 가중치(주요부재 > 보조부재)를 적용한 가중평균에, 주요부재 최악값을
    하한으로 걸어 보조부재 다수가 주요부재 심각결함을 가리지 못하게 한다.
    """
    members = [assess_member(code, ds) for code, ds in defects_by_member.items()]
    if not members:
        return InspectionAssessment(SafetyGrade.A, 0.0, [], 0, 0)

    weighted_sum = 0.0
    weight_total = 0.0
    primary_worst = 0.0
    for m in members:
        cls = MEMBER_CLASSES.get(m.member_code, MEMBER_CLASSES[DEFAULT_MEMBER_CODE])
        weighted_sum += m.defect_index * cls.weight
        weight_total += cls.weight
        if cls.is_primary:
            primary_worst = max(primary_worst, m.defect_index)

    index = weighted_sum / weight_total if weight_total else 0.0
    index = max(index, 0.85 * primary_worst)

    all_defects = [d for ds in defects_by_member.values() for d in ds]
    return InspectionAssessment(
        safety_grade=_grade_from_index(index),
        defect_index=round(index, 4),
        members=sorted(members, key=lambda m: -m.defect_index),
        repair_required_count=sum(1 for d in all_defects if d.repair_required),
        total_defects=len(all_defects),
    )
