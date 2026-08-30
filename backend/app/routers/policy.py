"""유지관리 정책 API — 강화학습 결과 조회 및 조치 추천."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import MEMBER_CLASSES, ConditionGrade
from ..models import Building, Defect, Inspection

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rl.env import (  # noqa: E402
    ACTION_LABELS_KO,
    MANAGER_LABELS_KO,
    N_GRADES,
    N_MANAGER_ACTIONS,
    N_WORKER_ACTIONS,
    EnvConfig,
    MaintenanceEnv,
)

router = APIRouter(prefix="/api/policy", tags=["policy"])

MODEL_DIR = ROOT / "models" / "rl_v1"
_AGENT = None
_AGENT_TRIED = False

GRADE_ORDER = [g.value for g in ConditionGrade]


def _load_agent(obs_dim: int, n_members: int):
    """학습된 정책을 지연 로딩한다. 없으면 None."""
    global _AGENT, _AGENT_TRIED
    if _AGENT is not None or _AGENT_TRIED:
        return _AGENT
    _AGENT_TRIED = True
    if not (MODEL_DIR / "worker.pt").exists():
        return None
    try:
        from rl.agent import HierarchicalAgent

        agent = HierarchicalAgent(
            obs_dim=obs_dim,
            n_members=n_members,
            n_worker_actions=N_WORKER_ACTIONS,
            n_manager_actions=N_MANAGER_ACTIONS,
        )
        agent.load(MODEL_DIR)
        _AGENT = agent
    except Exception:
        _AGENT = None
    return _AGENT


@router.get("/report")
def policy_report() -> dict:
    """학습 리포트 — 기준정책 대비 성과 비교."""
    path = MODEL_DIR / "report.json"
    if not path.exists():
        return {
            "available": False,
            "message": "학습된 정책이 없습니다. `python -m rl.train` 을 먼저 실행하세요.",
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    data["available"] = True
    data["action_labels"] = ACTION_LABELS_KO
    data["manager_labels"] = MANAGER_LABELS_KO
    return data


def _belief_from_inspection(db: Session, building_id: int) -> tuple[list[str], np.ndarray]:
    """최신 점검의 부재별 등급을 신념 분포로 변환한다.

    점검 결과는 확정값이 아니라 관측이므로, 해당 등급에 질량을 몰되
    인접 등급에도 일부 남겨 관측 불확실성을 반영한다.
    """
    latest = db.scalars(
        select(Inspection)
        .where(Inspection.building_id == building_id)
        .order_by(Inspection.inspected_at.desc())
        .limit(1)
    ).first()
    if not latest:
        raise HTTPException(404, "점검 이력이 없습니다")

    defects = db.scalars(
        select(Defect).where(Defect.inspection_id == latest.id)
    ).all()
    worst: dict[str, int] = {}
    for d in defects:
        idx = GRADE_ORDER.index(d.grade) if d.grade in GRADE_ORDER else 0
        worst[d.member_code] = max(worst.get(d.member_code, 0), idx)

    if not worst:
        worst = {"slab": 0}

    codes = sorted(worst)
    belief = np.zeros((len(codes), N_GRADES), np.float32)
    for i, c in enumerate(codes):
        g = worst[c]
        belief[i, g] = 0.70
        if g > 0:
            belief[i, g - 1] += 0.16
        if g < N_GRADES - 1:
            belief[i, g + 1] += 0.14
        belief[i] /= belief[i].sum()
    return codes, belief


@router.get("/recommend/{building_id}")
def recommend(building_id: int, db: Session = Depends(get_db)) -> dict:
    """현재 상태에서의 부재별 권장 조치."""
    if not db.get(Building, building_id):
        raise HTTPException(404, "건축물을 찾을 수 없습니다")

    codes, belief = _belief_from_inspection(db, building_id)
    n = len(codes)

    cfg = EnvConfig(n_members=n, seed=1)
    env = MaintenanceEnv(cfg)
    env.belief = belief
    env.grades = belief.argmax(axis=1).astype(np.int64)
    env.age_since_inspection = np.zeros(n, np.float32)
    obs = env.observe()

    agent = _load_agent(env.obs_dim, n)
    expected_grade = belief @ np.arange(N_GRADES)

    if agent is None:
        # 정책 미학습 시 규칙 기반 대안 — 화면이 비지 않도록 한다
        actions = []
        for e in expected_grade:
            actions.append(3 if e >= 2.6 else 2 if e >= 1.8 else 1 if e >= 0.9 else 0)
        # 값을 0으로 채우면 화면에 "기대가치 0.000" 으로 찍혀 마치 학습된
        # 정책이 그 조치를 무가치로 평가한 것처럼 보인다. 없는 값은 없다고
        # 표시해야 한다 — None 을 내려 화면에서 '—' 로 그린다.
        values = None
        source = "규칙 기반 (강화학습 정책 미학습)"
        manager = 1
    else:
        actions = agent.act_worker(obs, greedy=True).tolist()
        values = agent.worker.action_values(obs)
        manager = agent.act_manager(obs, greedy=True)
        source = "강화학습 정책 (HRL + CVaR)"

    rows = []
    for i, code in enumerate(codes):
        a = int(actions[i])
        g = GRADE_ORDER[int(round(expected_grade[i]))]
        rows.append(
            {
                "member_code": code,
                "member_label": (
                    MEMBER_CLASSES[code].label_ko if code in MEMBER_CLASSES else code
                ),
                "action": ACTION_LABELS_KO[a],
                "action_index": a,
                "expected_value": (
                    round(float(values["expected"][i][a]), 3) if values else None
                ),
                "cvar": round(float(values["cvar"][i][a]), 3) if values else None,
                "belief_grade": g,
                "expected_grade_num": round(float(expected_grade[i]), 2),
                "rationale": _rationale(a, expected_grade[i]),
            }
        )
    rows.sort(key=lambda r: -r["expected_grade_num"])
    return {
        "source": source,
        "manager_action": MANAGER_LABELS_KO[int(manager)],
        "risk_alpha": getattr(getattr(agent, "worker", None), "cfg", None)
        and agent.worker.cfg.risk_alpha,
        "actions": rows,
    }


def _rationale(action: int, expected_grade: float) -> str:
    if action == 0:
        return "상태 양호 — 다음 정기점검까지 경과관찰"
    if action == 1:
        return "상태 불확실 — 점검으로 등급을 확정한 뒤 판단"
    if action == 2:
        return "표면 열화 단계 — 조기 표면보수가 비용 대비 효과가 큼"
    if action == 3:
        return (
            "내구성 저하가 진행 — 단면보수·보강으로 등급 회복 필요"
            if expected_grade < 3.5
            else "심각 열화 — 긴급 보강 대상"
        )
    return "회복 불가 수준 — 교체가 생애주기 비용 측면에서 유리"


@router.get("/benchmark")
def benchmark() -> dict:
    """검출기 벤치마크 결과 — 합성 정답과 대조한 최신 기록.

    성능을 화면에 띄우는 이유는 자랑이 아니라 **해석의 전제**를 주기 위함이다.
    재현율 0.7 인 검출기의 결과를 보면서 "AI가 다 찾았겠지"라고 생각하면
    안 되기 때문이다.
    """
    path = ROOT / "docs" / "benchmark_baseline.json"
    if not path.exists():
        return {
            "available": False,
            "message": "벤치마크 기록이 없습니다. `python -m datagen.evaluate` 로 생성하십시오.",
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    data["available"] = True
    data["caveat"] = (
        "합성 표본 기준입니다. 실촬영 분포와 다를 수 있으므로 현장 데이터로 "
        "재측정해야 합니다. 정밀도가 낮으므로 사람 검수가 전제입니다."
    )
    return data
