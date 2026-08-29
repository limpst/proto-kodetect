"""시설물 유지관리 의사결정 환경 (POMDP).

문제 정의
---------
관리자는 매년 한정된 예산으로 여러 부재의 점검·보수를 결정한다. 부재의 실제
열화 상태는 직접 볼 수 없고(잠재 상태), 점검을 해야만 잡음 섞인 관측을 얻는다.
점검에도 비용이 들고, 방치하면 열화가 진행되어 보수비가 급증하며, 최악의 경우
사용 제한·붕괴 위험(E등급)에 이른다.

이는 전형적인 POMDP이며 인프라 유지관리 강화학습의 표준 문제 설정이다.

상태 (부재별)
-------------
* 열화 등급 d in {0..4}  — 시특법 상태평가 a~e에 대응
* 마지막 점검 이후 경과 연수
* 신념(belief) — 등급에 대한 확률분포. 에이전트가 실제로 보는 것은 이 신념이다.

행동 (부재별, Worker 수준)
--------------------------
0 무조치 / 1 점검 / 2 표면보수 / 3 단면보수·보강 / 4 교체

행동 (Manager 수준)
-------------------
0 관찰 위주 / 1 예방보수 / 2 집중보수 / 3 긴급대응
Manager는 예산 배분 기조를 정하고, Worker의 행동 비용·가용 예산에 영향을 준다.

보상
----
-(점검비 + 보수비) - 위험비용(등급이 높을수록 급증) + 잔존 건전성 보너스
안전이 걸린 문제이므로 위험비용은 등급에 대해 볼록(convex)하게 준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

N_GRADES = 5           # a b c d e
N_WORKER_ACTIONS = 5
N_MANAGER_ACTIONS = 4

ACTION_LABELS_KO = ["무조치", "점검", "표면보수", "단면보수·보강", "교체"]
MANAGER_LABELS_KO = ["관찰 위주", "예방보수", "집중보수", "긴급대응"]

# 행동별 비용 (백만원). 열화가 진행될수록 보수비가 급증하는 구조를 반영한다.
INSPECT_COST = 1.2
REPAIR_COST = np.array(
    [
        # d=a     b      c      d      e
        [0.0,   0.0,   0.0,   0.0,   0.0],    # 무조치
        [0.0,   0.0,   0.0,   0.0,   0.0],    # 점검 (별도 계상)
        [2.0,   2.5,   4.0,   7.0,  12.0],    # 표면보수
        [8.0,   9.0,  14.0,  22.0,  34.0],    # 단면보수·보강
        [40.0, 42.0,  48.0,  58.0,  72.0],    # 교체
    ],
    dtype=np.float32,
)

# 조치 후 등급이 개선되는 정도(등급 단계). 심하게 열화된 부재는 표면보수로 회복 불가.
REPAIR_EFFECT = np.array(
    [
        [0, 0, 0, 0, 0],   # 무조치
        [0, 0, 0, 0, 0],   # 점검
        [0, 1, 1, 1, 0],   # 표면보수 — e등급에는 효과 없음
        [0, 1, 2, 2, 2],   # 단면보수·보강
        [0, 4, 4, 4, 4],   # 교체 — 신품 상태로
    ],
    dtype=np.int64,
)

# 등급별 연간 위험비용 (백만원 상당). d·e에서 급격히 커진다.
RISK_COST = np.array([0.0, 0.5, 2.5, 12.0, 45.0], dtype=np.float32)

# 점검 관측 혼동행렬 — 육안점검은 인접 등급을 자주 혼동한다.
OBSERVATION_MODEL = np.array(
    [
        [0.80, 0.16, 0.04, 0.00, 0.00],
        [0.14, 0.70, 0.14, 0.02, 0.00],
        [0.03, 0.15, 0.66, 0.14, 0.02],
        [0.00, 0.03, 0.16, 0.68, 0.13],
        [0.00, 0.00, 0.04, 0.18, 0.78],
    ],
    dtype=np.float32,
)


@dataclass
class EnvConfig:
    n_members: int = 6
    horizon: int = 30                    # 연 단위
    annual_budget: float = 40.0          # 백만원/년
    discount: float = 0.97
    environment_severity: float = 1.0    # 노출환경 가혹도 (해안·동결융해 등)
    seed: int = 0
    member_weights: np.ndarray | None = None   # 주요부재 가중치


@dataclass
class StepInfo:
    cost: float = 0.0
    risk: float = 0.0
    budget_left: float = 0.0
    true_grades: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    worst_grade: int = 0
    failures: int = 0


class MaintenanceEnv:
    """부재 다수를 동시에 관리하는 예산 제약 POMDP."""

    def __init__(self, cfg: EnvConfig | None = None) -> None:
        self.cfg = cfg or EnvConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        n = self.cfg.n_members
        self.weights = (
            self.cfg.member_weights
            if self.cfg.member_weights is not None
            else np.linspace(1.0, 0.55, n).astype(np.float32)
        )
        self._build_transition()
        self.reset()

    # ─── 열화 전이 ────────────────────────────────────────────
    def _build_transition(self) -> None:
        """연간 열화 전이행렬. 등급이 높을수록 다음 등급으로 갈 확률이 커진다."""
        sev = self.cfg.environment_severity
        p_progress = np.clip(np.array([0.035, 0.055, 0.075, 0.105]) * sev, 0.0, 0.85)
        T = np.zeros((N_GRADES, N_GRADES), np.float32)
        for d in range(N_GRADES - 1):
            T[d, d] = 1.0 - p_progress[d]
            T[d, d + 1] = p_progress[d]
        T[N_GRADES - 1, N_GRADES - 1] = 1.0
        self.T = T

    # ─── 에피소드 제어 ────────────────────────────────────────
    def reset(self, seed: int | None = None) -> np.ndarray:
        if seed is not None:
            self.rng = np.random.default_rng(seed)
        n = self.cfg.n_members
        # 초기 상태는 대체로 양호하나 일부는 이미 진행된 상태로 시작
        self.grades = self.rng.choice(
            N_GRADES, size=n, p=[0.45, 0.30, 0.15, 0.08, 0.02]
        ).astype(np.int64)
        self.age_since_inspection = np.zeros(n, np.float32)
        self.belief = np.tile(
            np.array([0.45, 0.30, 0.15, 0.08, 0.02], np.float32), (n, 1)
        )
        self.t = 0
        self.budget_left = self.cfg.annual_budget
        self.manager_action = 1
        return self.observe()

    def observe(self) -> np.ndarray:
        """에이전트가 보는 관측 — 신념 + 경과연수 + 예산 + 시간 + 기조."""
        n = self.cfg.n_members
        parts = [
            self.belief.reshape(-1),
            np.clip(self.age_since_inspection / 10.0, 0, 1),
            self.weights,
            np.array(
                [
                    self.budget_left / max(self.cfg.annual_budget, 1e-6),
                    self.t / self.cfg.horizon,
                    self.manager_action / (N_MANAGER_ACTIONS - 1),
                ],
                np.float32,
            ),
        ]
        return np.concatenate(parts).astype(np.float32)

    @property
    def obs_dim(self) -> int:
        return self.cfg.n_members * (N_GRADES + 2) + 3

    # ─── Manager 수준 ─────────────────────────────────────────
    def set_manager_action(self, a: int) -> None:
        """예산 기조 설정. 긴급대응은 예산을 늘리되 비용 효율이 나쁘다."""
        self.manager_action = int(a)
        multiplier = [0.7, 1.0, 1.35, 1.8][self.manager_action]
        self.budget_left = self.cfg.annual_budget * multiplier

    # ─── Worker 수준 ──────────────────────────────────────────
    def step(self, actions: np.ndarray) -> tuple[np.ndarray, float, bool, StepInfo]:
        """1년 진행. actions: 부재별 행동 인덱스 배열."""
        n = self.cfg.n_members
        actions = np.asarray(actions, np.int64).reshape(n)
        info = StepInfo()

        # 긴급대응 기조에서는 같은 공사도 할증이 붙는다(긴급 발주·야간작업)
        cost_multiplier = [1.0, 1.0, 1.08, 1.30][self.manager_action]

        spent = 0.0
        for i in range(n):
            a = int(actions[i])
            if a == 1:  # 점검
                cost = INSPECT_COST
            else:
                cost = float(REPAIR_COST[a, self.grades[i]])
            cost *= cost_multiplier

            # 예산 초과 시 조치를 수행하지 못한다 (무조치로 강등)
            if cost > self.budget_left + 1e-9:
                actions[i] = 0
                continue

            self.budget_left -= cost
            spent += cost

            if a == 1:
                obs_grade = int(
                    self.rng.choice(N_GRADES, p=OBSERVATION_MODEL[self.grades[i]])
                )
                self._update_belief(i, obs_grade)
                self.age_since_inspection[i] = 0.0
            elif a >= 2:
                improve = int(REPAIR_EFFECT[a, self.grades[i]])
                self.grades[i] = max(0, self.grades[i] - improve)
                # 보수 후에는 상태를 확실히 안다
                self.belief[i] = 0.0
                self.belief[i, self.grades[i]] = 1.0
                self.age_since_inspection[i] = 0.0

        # 위험비용 — 실제 등급 기준, 부재 가중치 반영
        risk = float((RISK_COST[self.grades] * self.weights).sum())

        # 열화 진행
        for i in range(n):
            self.grades[i] = int(
                self.rng.choice(N_GRADES, p=self.T[self.grades[i]])
            )
            self.belief[i] = self.belief[i] @ self.T
            self.belief[i] /= max(self.belief[i].sum(), 1e-9)
            self.age_since_inspection[i] += 1.0

        self.t += 1
        self.budget_left = self.cfg.annual_budget
        done = self.t >= self.cfg.horizon

        # 보상: 지출과 위험을 모두 비용으로 본다. 건전성 유지에 보너스.
        health_bonus = float((N_GRADES - 1 - self.grades).mean()) * 0.8
        reward = -(spent + risk) / 10.0 + health_bonus

        info.cost = round(spent, 3)
        info.risk = round(risk, 3)
        info.budget_left = round(self.budget_left, 3)
        info.true_grades = self.grades.copy()
        info.worst_grade = int(self.grades.max())
        info.failures = int((self.grades >= 4).sum())
        return self.observe(), float(reward), done, info

    def _update_belief(self, i: int, observed: int) -> None:
        """베이즈 갱신 — 관측 모델의 우도를 사전분포에 곱한다."""
        likelihood = OBSERVATION_MODEL[:, observed]
        post = self.belief[i] * likelihood
        s = post.sum()
        self.belief[i] = post / s if s > 1e-9 else np.full(N_GRADES, 1.0 / N_GRADES)


# ─── 비교용 기준 정책 ─────────────────────────────────────────
def policy_reactive(env: MaintenanceEnv) -> np.ndarray:
    """사후 보수 — 심각해 보일 때만 고친다 (현행 관행의 하한선)."""
    a = np.zeros(env.cfg.n_members, np.int64)
    expected = env.belief @ np.arange(N_GRADES)
    for i, e in enumerate(expected):
        if e >= 3.2:
            a[i] = 3
        elif env.age_since_inspection[i] >= 4:
            a[i] = 1
    return a


def policy_periodic(env: MaintenanceEnv, period: int = 3) -> np.ndarray:
    """정기 보수 — 주기적으로 점검하고 c등급 이상이면 보수한다."""
    a = np.zeros(env.cfg.n_members, np.int64)
    expected = env.belief @ np.arange(N_GRADES)
    for i, e in enumerate(expected):
        if e >= 2.6:
            a[i] = 3
        elif e >= 1.8:
            a[i] = 2
        elif env.age_since_inspection[i] >= period:
            a[i] = 1
    return a
