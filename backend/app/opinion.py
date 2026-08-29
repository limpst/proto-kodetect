"""건강소견서 문장 생성 및 검사 (BHC-STD-2026 §9.2).

표준은 소견 문장에 대해 두 가지를 규범적으로 요구한다.

1. **관측 → 해석 → 권고를 분리**하여 기술한다. 한 문장에 셋을 섞지 않는다.
2. **금지 표현**을 쓰지 않는다 — 근거 없는 단정, 측정값 없는 형용사,
   책임 회피형 서술, 원인 미상 상태의 원인 단정.

이 모듈이 LLM이 아니라 규칙 기반인 이유
----------------------------------------
F3(LLM 진단 의견서)의 완료 기준은 "기술용어 환각 0건"이다. 생성형 모델은
확률적으로 문장을 만들므로 0건을 보장할 수 없고, 검증하려면 결국 사람이 전수
확인해야 한다. 측정값과 판정 근거는 이미 구조화된 데이터로 존재하므로,
템플릿으로 조립하면 **환각이 원리적으로 불가능**하고 문장 구조도 표준을 항상
지킨다. 생성형 모델은 이 초안을 다듬는 후단 옵션으로 두는 것이 옳다.

`lint_opinion()` 은 사람이 직접 쓴 소견에도 적용할 수 있는 금지표현 검사기다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .bhc import (
    PRIORITIES,
    SEVERITY_LABELS_KO,
    SYSTEMS,
    CheckupResult,
    DefectObservation,
    PrescriptionDraft,
    Severity,
)
from .domain import (
    ALLOWABLE_CRACK_WIDTH_MM,
    DEFECT_LABELS_KO,
    ENVIRONMENT_LABELS_KO,
    MEMBER_CLASSES,
    DefectType,
    Environment,
)

# ─── §9.2 금지 표현 ────────────────────────────────────────────
@dataclass(frozen=True)
class ForbiddenPattern:
    pattern: str
    category: str
    reason: str
    suggestion: str


FORBIDDEN_PATTERNS: list[ForbiddenPattern] = [
    ForbiddenPattern(
        r"다수의|여러\s*개소의|상당수의",
        "측정값 없는 형용",
        "수량을 형용사로 대체했습니다",
        "개소 수 또는 비율로 바꾸십시오 (예: 3개소, 조사부재의 12%)",
    ),
    ForbiddenPattern(
        r"심각한|상당한|현저한|매우\s*큰",
        "측정값 없는 형용",
        "정도를 형용사로 대체했습니다",
        "측정치와 판정등급으로 바꾸십시오 (예: 최대폭 0.62mm, D4)",
    ),
    ForbiddenPattern(
        r"붕괴\s*위험이?\s*(있|존재)",
        "근거 없는 단정",
        "구조해석 근거 없이 붕괴를 단정했습니다",
        "관측 사실과 심각도 등급으로 기술하고, 필요하면 구조검토를 권고하십시오",
    ),
    ForbiddenPattern(
        r"안전에?\s*이상\s*(이)?\s*없",
        "근거 없는 단정",
        "미실시 항목이 있는 상태에서 전체 안전을 단정할 수 없습니다",
        "조사 범위를 명시하고 그 범위 내 소견으로 한정하십시오",
    ),
    ForbiddenPattern(
        r"관리주체의?\s*판단이?\s*필요",
        "책임 회피형 서술",
        "권고를 생략하고 판단을 넘겼습니다",
        "조치 내용과 기한을 명시한 권고를 함께 기술하십시오",
    ),
    ForbiddenPattern(
        r"(노후화|시공\s*불량|설계\s*오류)에?\s*(의한|따른|기인)",
        "원인 미상 상태의 원인 단정",
        "원인 규명 절차 없이 원인을 단정했습니다",
        "원인이 확인되지 않았으면 '원인 미상, 추가 조사 필요'로 기술하십시오",
    ),
]


@dataclass
class LintFinding:
    category: str
    matched: str
    reason: str
    suggestion: str
    position: int


def lint_opinion(text: str) -> list[LintFinding]:
    """소견 문장에서 표준 §9.2 금지표현을 찾는다."""
    out: list[LintFinding] = []
    for fp in FORBIDDEN_PATTERNS:
        for m in re.finditer(fp.pattern, text):
            out.append(
                LintFinding(
                    category=fp.category,
                    matched=m.group(0),
                    reason=fp.reason,
                    suggestion=fp.suggestion,
                    position=m.start(),
                )
            )
    out.sort(key=lambda f: f.position)
    return out


# ─── 문장 조립 ─────────────────────────────────────────────────
def _member_label(code: str) -> str:
    cls = MEMBER_CLASSES.get(code)
    return cls.label_ko if cls else code


def _defect_label(t: DefectType) -> str:
    return DEFECT_LABELS_KO.get(t, t.value)


@dataclass
class OpinionSentence:
    """§9.2가 요구하는 3요소 분리 소견."""

    defect_id: str
    observation: str      # 관측 — 측정값과 사실만
    interpretation: str   # 해석 — 기준과 대조
    recommendation: str   # 권고 — 조치와 기한

    def as_text(self) -> str:
        return f"{self.observation}\n{self.interpretation}\n{self.recommendation}"


def observation_sentence(
    obs: DefectObservation, *, tolerance_mm: float = 0.02
) -> str:
    """관측 — 측정값과 사실만 기술한다. 해석·권고를 섞지 않는다."""
    where = obs.location or f"{_member_label(obs.member_code)}"
    parts = [f"{where}에 {_defect_label(obs.defect_type)}"]

    if obs.width_mm is not None:
        parts.append(f"최대폭 {obs.width_mm:.2f}mm(±{tolerance_mm:.2f})")
    if obs.area_ratio is not None:
        parts.append(f"면적률 {obs.area_ratio * 100:.2f}%")
    if obs.extent > 0:
        parts.append(f"동종 부재 {obs.extent * 100:.0f}%에서 발현")

    return " · ".join(parts) + " 관측."


def interpretation_sentence(
    obs: DefectObservation, environment: Environment = Environment.HUMID
) -> str:
    """해석 — 관측이 의미하는 바를 기준과 대조하여 기술한다."""
    sev_label = SEVERITY_LABELS_KO[obs.severity]
    head = f"{obs.severity.value}({sev_label})에 해당한다"

    clauses = [head]
    if obs.defect_type is DefectType.CRACK and obs.width_mm is not None:
        allowable = ALLOWABLE_CRACK_WIDTH_MM[environment]
        env_label = ENVIRONMENT_LABELS_KO[environment]
        if obs.width_mm >= allowable:
            clauses.append(
                f"{env_label}의 허용균열폭 {allowable:.2f}mm(KDS 14 20 30)를 "
                f"{obs.width_mm - allowable:.2f}mm 초과한다"
            )
        else:
            clauses.append(
                f"{env_label}의 허용균열폭 {allowable:.2f}mm(KDS 14 20 30) 이내이다"
            )

    if obs.defect_type is DefectType.REBAR_EXPOSURE:
        clauses.append("피복 상실로 철근 부식이 진행 중일 수 있어 부식도 확인이 필요하다")
    elif obs.defect_type is DefectType.LEAKAGE:
        clauses.append("누수 경로가 확인되지 않았으면 원인 미상으로 보고 추가 조사가 필요하다")
    elif obs.defect_type is DefectType.EFFLORESCENCE:
        clauses.append("백태는 수분 이동의 흔적이므로 누수 경로 동반 여부를 확인해야 한다")

    cls = MEMBER_CLASSES.get(obs.member_code)
    if cls is not None and cls.is_primary and obs.severity in (Severity.D4, Severity.D5):
        clauses.append("주요부재에서 발생하여 계통 건강점수에 지배적으로 작용한다")

    return ". ".join(clauses) + "."


def recommendation_sentence(p: PrescriptionDraft) -> str:
    """권고 — 조치와 기한을 기술한다."""
    spec = PRIORITIES[p.priority]
    due = f"{p.due_date:%Y-%m-%d}까지" if p.due_date else spec.due_text
    return f"{p.priority.value}({spec.label_ko}). {due} {p.action_ko}."


def build_sentences(
    observations: list[DefectObservation],
    prescriptions: list[PrescriptionDraft],
    environment: Environment = Environment.HUMID,
) -> list[OpinionSentence]:
    by_id = {p.defect_id: p for p in prescriptions}
    out: list[OpinionSentence] = []
    for o in observations:
        p = by_id.get(o.defect_id)
        out.append(
            OpinionSentence(
                defect_id=o.defect_id,
                observation=observation_sentence(o),
                interpretation=interpretation_sentence(o, environment),
                recommendation=(
                    recommendation_sentence(p)
                    if p
                    else "조치 불요. 차기 검진 시 동일 위치 재확인."
                ),
            )
        )
    return out


# ─── 제1부 1면 요약 ────────────────────────────────────────────
@dataclass
class OpinionSummary:
    """§9.1 제1부 — 반드시 단일 페이지에 들어가야 하는 요약."""

    headline: str
    grade_line: str
    health_age_line: str
    rate_line: str
    red_flag_line: str
    prescription_line: str
    next_checkup_line: str
    caveats: list[str] = field(default_factory=list)


def build_summary(
    result: CheckupResult,
    *,
    building_name: str,
    statutory_grade: str | None = None,
    next_checkup: str | None = None,
) -> OpinionSummary:
    """1면 요약 문장. 적신호가 발동했으면 규칙 번호와 근거를 반드시 병기한다(§8.5)."""
    grade_line = (
        f"종합등급 {result.grade}({result.grade_label_ko}) · "
        f"BHI {result.bhi:.1f}점"
    )
    if result.bhi < result.bhi_raw:
        grade_line += f" (적신호 적용 전 {result.bhi_raw:.1f}점)"

    caveats: list[str] = []
    if statutory_grade and statutory_grade != result.grade:
        grade_line += f" · 시설물안전법 안전등급 {statutory_grade}"
        caveats.append(
            f"본 표준 등급({result.grade})과 법정 안전등급({statutory_grade})이 "
            "다릅니다. 법정 등급은 변경하지 않으며, 차이는 통상 방호·이력계통의 "
            "감점에서 발생합니다."
        )

    ha = result.health_age
    if ha.deviation is None:
        health_age_line = (
            f"건강나이 {ha.bha_years:.1f}년 (코호트 {ha.cohort_label_ko}, β={ha.beta})"
        )
    else:
        sign = "+" if ha.deviation >= 0 else ""
        health_age_line = (
            f"건강나이 {ha.bha_years:.1f}년 · 실제 경과 {ha.actual_years:.0f}년 · "
            f"노화편차 {sign}{ha.deviation:.1f}년 — {ha.interpretation}"
        )
    if ha.advisory_only:
        caveats.append(
            "건강나이는 코호트 표본이 30건에 미달하여 참고지표입니다. "
            "등급 판정이나 처방 우선순위 결정에 사용하지 않았습니다(§8.7)."
        )

    if result.rate.baseline:
        rate_line = "열화속도 — 기준선 검진(최초)이므로 산출하지 않았습니다"
    else:
        rate_line = (
            f"열화속도 {result.rate.value:+.2f}점/년 — {result.rate.verdict}. "
            f"{result.rate.action}"
        )

    if result.red_flags:
        codes = " · ".join(
            f"{f.code}({f.condition_ko})" for f in result.red_flags
        )
        red_flag_line = f"적신호 발동 {len(result.red_flags)}건 — {codes}"
    else:
        red_flag_line = "적신호 발동 없음"

    prescription_line = (
        f"처방 총 {len(result.prescriptions)}건 · "
        f"P0(응급) {result.p0_count}건 · P1(긴급) {result.p1_count}건"
    )
    if result.p0_count:
        caveats.append(
            "P0 처방은 소견서 완성 여부와 무관하게 즉시 통보하여야 합니다(§9.3). "
            "소견서 작성을 이유로 P0 통보를 지연하면 표준 위반입니다."
        )

    not_performed = [s for s in result.systems if not s.performed]
    if not_performed:
        caveats.append(
            "미실시 계통 "
            + ", ".join(f"{s.system.value} {s.label_ko}" for s in not_performed)
            + " 은 D3 상당(65점)으로 처리했습니다(§7.2). 해당 범위에 대해서는 "
            "안전 여부를 단정할 수 없습니다."
        )
    caveats.extend(result.weight_problems)
    caveats.append(
        "본 소견의 AI 산출물은 보조 참고자료이며, 자격 있는 책임기술자의 "
        "확인·승인을 거쳐야 효력을 갖습니다(HITL)."
    )

    return OpinionSummary(
        headline=f"{building_name} 건축물 건강소견서 ({result.standard})",
        grade_line=grade_line,
        health_age_line=health_age_line,
        rate_line=rate_line,
        red_flag_line=red_flag_line,
        prescription_line=prescription_line,
        next_checkup_line=(
            f"다음 검진 기한 {next_checkup}" if next_checkup else "다음 검진 기한 — 관리계획에 따름"
        ),
        caveats=caveats,
    )


def system_comment(score, previous: float | None = None) -> str:
    """§9.1 제2부 — 계통별 소견 한 줄. 전회 대비 증감을 함께 적는다."""
    spec = SYSTEMS[score.system]
    line = f"{score.system.value} {spec.label_ko} {score.score:.1f}점"
    if previous is not None:
        line += f" ({score.score - previous:+.1f})"
    if not score.performed:
        line += " — 미실시, D3 상당으로 처리"
    elif score.capped_by_d5:
        line += " — 계통 내 D5 결함으로 상한 30점 적용"
    elif score.defect_count == 0:
        line += " — 관측된 결함 없음"
    else:
        line += (
            f" — 결함 {score.defect_count}건, 최고 심각도 {score.worst_severity.value}"
            f"({SEVERITY_LABELS_KO[score.worst_severity]})"
        )
    return line
