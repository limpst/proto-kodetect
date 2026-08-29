"""KO-Detect 도메인 상수 및 판정 기준.

근거 기준
---------
* 시설물의 안전 및 유지관리에 관한 특별법(시특법) 및 동 시행령
* 「안전점검 및 정밀안전진단 세부지침」(국토교통부) — 상태평가 기준
* KDS 14 20 30 콘크리트구조 사용성 설계기준 — 허용균열폭
* KDS 14 20 40 / KCS 14 20 — 콘크리트 균열 보수 판정

주의: 본 모듈의 산출값은 보조 참고자료이며, 법적 효력이 있는 진단 결과로
사용하려면 책임기술자(구조기술사 등)의 검토·서명이 필요하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DefectType(str, Enum):
    """Mask R-CNN 7종 결함 분류 체계."""

    CRACK = "crack"                    # 균열
    SPALLING = "spalling"              # 박리·박락
    EFFLORESCENCE = "efflorescence"    # 백태
    LEAKAGE = "leakage"                # 누수
    REBAR_EXPOSURE = "rebar_exposure"  # 철근노출
    SEGREGATION = "segregation"        # 재료분리
    DAMAGE = "damage"                  # 손상(파손·마모)


DEFECT_LABELS_KO: dict[DefectType, str] = {
    DefectType.CRACK: "균열",
    DefectType.SPALLING: "박리·박락",
    DefectType.EFFLORESCENCE: "백태",
    DefectType.LEAKAGE: "누수",
    DefectType.REBAR_EXPOSURE: "철근노출",
    DefectType.SEGREGATION: "재료분리",
    DefectType.DAMAGE: "손상",
}


class Environment(str, Enum):
    """KDS 14 20 30 노출환경 구분 — 허용균열폭 산정에 사용."""

    DRY = "dry"                  # 건조환경
    HUMID = "humid"              # 습윤환경
    CORROSIVE = "corrosive"      # 부식성 환경
    HIGH_CORROSIVE = "high_corrosive"  # 고부식성 환경


# KDS 14 20 30 — 철근콘크리트 부재의 내구성 확보를 위한 허용균열폭 (mm)
ALLOWABLE_CRACK_WIDTH_MM: dict[Environment, float] = {
    Environment.DRY: 0.40,
    Environment.HUMID: 0.30,
    Environment.CORROSIVE: 0.20,
    Environment.HIGH_CORROSIVE: 0.10,
}

ENVIRONMENT_LABELS_KO: dict[Environment, str] = {
    Environment.DRY: "건조환경",
    Environment.HUMID: "습윤환경",
    Environment.CORROSIVE: "부식성 환경",
    Environment.HIGH_CORROSIVE: "고부식성 환경",
}


class ConditionGrade(str, Enum):
    """부재 단위 상태평가 등급 (세부지침 a~e)."""

    A = "a"  # 우수
    B = "b"  # 양호
    C = "c"  # 보통
    D = "d"  # 미흡
    E = "e"  # 불량


CONDITION_GRADE_LABELS_KO: dict[ConditionGrade, str] = {
    ConditionGrade.A: "우수",
    ConditionGrade.B: "양호",
    ConditionGrade.C: "보통",
    ConditionGrade.D: "미흡",
    ConditionGrade.E: "불량",
}

# 세부지침 상태평가 등급별 결함도 점수(대표값). 0에 가까울수록 양호.
CONDITION_GRADE_SCORE: dict[ConditionGrade, float] = {
    ConditionGrade.A: 0.0,
    ConditionGrade.B: 0.1,
    ConditionGrade.C: 0.2,
    ConditionGrade.D: 0.5,
    ConditionGrade.E: 0.8,
}


class SafetyGrade(str, Enum):
    """시설물 종합 안전등급 (시특법 A~E)."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"


SAFETY_GRADE_DESCRIPTION_KO: dict[SafetyGrade, str] = {
    SafetyGrade.A: "우수 — 문제점이 없는 최상의 상태",
    SafetyGrade.B: "양호 — 보조부재에 경미한 결함이 있으나 기능 발휘에 지장이 없으며, "
                   "내구성 증진을 위하여 일부의 보수가 필요한 상태",
    SafetyGrade.C: "보통 — 주요부재에 경미한 결함 또는 보조부재에 광범위한 결함이 있으나 "
                   "전체적인 시설물의 안전에는 지장이 없으며, 주요부재에 내구성·기능성 "
                   "저하 방지를 위한 보수가 필요한 상태",
    SafetyGrade.D: "미흡 — 주요부재에 결함이 발생하여 긴급한 보수·보강이 필요하며 "
                   "사용제한 여부를 결정하여야 하는 상태",
    SafetyGrade.E: "불량 — 주요부재에 발생한 심각한 결함으로 인하여 시설물의 안전에 위험이 "
                   "있어 즉각 사용을 금지하고 보강 또는 개축을 하여야 하는 상태",
}

# 세부지침: 종합 상태평가 결과(결함도 지수)에 따른 안전등급 경계값.
# 지수가 낮을수록 양호하다.
SAFETY_GRADE_THRESHOLDS: list[tuple[float, SafetyGrade]] = [
    (0.12, SafetyGrade.A),
    (0.25, SafetyGrade.B),
    (0.40, SafetyGrade.C),
    (0.60, SafetyGrade.D),
    (float("inf"), SafetyGrade.E),
]


@dataclass(frozen=True)
class MemberClass:
    """부재 분류 — 주요부재/보조부재에 따라 결함 가중치가 달라진다."""

    code: str
    label_ko: str
    is_primary: bool
    weight: float


MEMBER_CLASSES: dict[str, MemberClass] = {
    "column": MemberClass("column", "기둥", True, 1.00),
    "girder": MemberClass("girder", "거더·보", True, 1.00),
    "slab": MemberClass("slab", "슬래브", True, 0.90),
    "wall_shear": MemberClass("wall_shear", "전단벽", True, 1.00),
    "foundation": MemberClass("foundation", "기초", True, 1.00),
    "wall_non": MemberClass("wall_non", "비내력벽", False, 0.50),
    "parapet": MemberClass("parapet", "난간·파라펫", False, 0.40),
    "finish": MemberClass("finish", "마감재", False, 0.30),
    "retaining_wall": MemberClass("retaining_wall", "옹벽", True, 0.95),
}

DEFAULT_MEMBER_CODE = "slab"


class InspectionKind(str, Enum):
    """시특법 점검 종류."""

    REGULAR = "regular"          # 정기안전점검
    PRECISE = "precise"          # 정밀안전점검
    DIAGNOSIS = "diagnosis"      # 정밀안전진단
    EMERGENCY = "emergency"      # 긴급안전점검


INSPECTION_KIND_LABELS_KO: dict[InspectionKind, str] = {
    InspectionKind.REGULAR: "정기안전점검",
    InspectionKind.PRECISE: "정밀안전점검",
    InspectionKind.DIAGNOSIS: "정밀안전진단",
    InspectionKind.EMERGENCY: "긴급안전점검",
}
