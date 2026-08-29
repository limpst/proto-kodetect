"""우선순위 경험 재현 버퍼 (PER) + n-step 반환.

* **PER** — 예산 제약 하의 장기 문제에서는 드물게 발생하는 '심각 열화' 전이가
  결정적으로 중요한데, 균등 샘플링으로는 거의 뽑히지 않는다. TD 오차가 큰
  전이를 우선 학습하되, 중요도 가중치로 편향을 보정한다.
* **n-step** — 보수의 효과는 여러 해에 걸쳐 나타난다. 1-step만 보면 신호가
  너무 희석되므로 n-step 반환으로 크레딧 전파를 빠르게 한다.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class SumTree:
    """구간합 트리 — O(log n) 우선순위 비례 샘플링."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.tree = np.zeros(2 * capacity, np.float64)

    def total(self) -> float:
        return float(self.tree[1])

    def update(self, idx: int, priority: float) -> None:
        i = idx + self.capacity
        self.tree[i] = priority
        i //= 2
        while i >= 1:
            self.tree[i] = self.tree[2 * i] + self.tree[2 * i + 1]
            i //= 2

    def sample(self, value: float) -> int:
        i = 1
        while i < self.capacity:
            left = 2 * i
            if value <= self.tree[left]:
                i = left
            else:
                value -= self.tree[left]
                i = left + 1
        return i - self.capacity


class PrioritizedReplay:
    """n-step 반환을 누적해 저장하는 우선순위 재현 버퍼."""

    def __init__(
        self,
        capacity: int,
        obs_dim: int,
        n_branches: int,
        *,
        n_step: int = 3,
        gamma: float = 0.97,
        alpha: float = 0.6,
        beta0: float = 0.4,
        beta_steps: int = 100_000,
        eps: float = 1e-3,
    ) -> None:
        self.capacity = capacity
        self.n_step = n_step
        self.gamma = gamma
        self.alpha = alpha
        self.beta0 = beta0
        self.beta_steps = beta_steps
        self.eps = eps

        self.obs = np.zeros((capacity, obs_dim), np.float32)
        self.next_obs = np.zeros((capacity, obs_dim), np.float32)
        self.actions = np.zeros((capacity, n_branches), np.int64)
        self.rewards = np.zeros(capacity, np.float32)
        self.dones = np.zeros(capacity, np.float32)

        self.tree = SumTree(capacity)
        self.pos = 0
        self.size = 0
        self.max_priority = 1.0
        self._nstep: deque = deque(maxlen=n_step)

    def _push(self, obs, action, reward, next_obs, done) -> None:
        i = self.pos
        self.obs[i] = obs
        self.actions[i] = action
        self.rewards[i] = reward
        self.next_obs[i] = next_obs
        self.dones[i] = float(done)
        self.tree.update(i, self.max_priority**self.alpha)
        self.pos = (self.pos + 1) % self.capacity
        self.size = min(self.size + 1, self.capacity)

    def add(self, obs, action, reward, next_obs, done) -> None:
        """전이를 넣고, n-step이 채워지면 누적 반환으로 커밋한다."""
        self._nstep.append((obs, action, reward, next_obs, done))
        if len(self._nstep) < self.n_step and not done:
            return

        # n-step 누적 반환 계산
        R = 0.0
        for k, (_, _, r, _, d) in enumerate(self._nstep):
            R += (self.gamma**k) * r
            if d:
                break
        obs0, act0 = self._nstep[0][0], self._nstep[0][1]
        last_next, last_done = self._nstep[-1][3], self._nstep[-1][4]
        self._push(obs0, act0, R, last_next, last_done)

        if done:
            # 에피소드 종료 시 남은 꼬리도 모두 커밋
            while len(self._nstep) > 1:
                self._nstep.popleft()
                R = 0.0
                for k, (_, _, r, _, d) in enumerate(self._nstep):
                    R += (self.gamma**k) * r
                    if d:
                        break
                self._push(
                    self._nstep[0][0], self._nstep[0][1], R,
                    self._nstep[-1][3], self._nstep[-1][4],
                )
            self._nstep.clear()

    def beta(self, step: int) -> float:
        """중요도 보정 지수 — 학습이 진행될수록 1로 수렴시킨다."""
        return min(1.0, self.beta0 + (1.0 - self.beta0) * step / self.beta_steps)

    def sample(self, batch_size: int, step: int):
        assert self.size > 0, "빈 버퍼에서 샘플링할 수 없습니다"
        total = self.tree.total()
        segment = total / batch_size
        idxs = np.empty(batch_size, np.int64)
        for i in range(batch_size):
            v = np.random.uniform(segment * i, segment * (i + 1))
            idxs[i] = self.tree.sample(v)
        idxs = np.clip(idxs, 0, self.size - 1)

        priorities = self.tree.tree[idxs + self.capacity]
        probs = priorities / max(total, 1e-12)
        weights = (self.size * np.maximum(probs, 1e-12)) ** (-self.beta(step))
        weights = (weights / weights.max()).astype(np.float32)

        return (
            self.obs[idxs],
            self.actions[idxs],
            self.rewards[idxs],
            self.next_obs[idxs],
            self.dones[idxs],
            weights,
            idxs,
        )

    def update_priorities(self, idxs: np.ndarray, td_errors: np.ndarray) -> None:
        p = np.abs(td_errors) + self.eps
        self.max_priority = max(self.max_priority, float(p.max()))
        for i, pi in zip(idxs, p):
            self.tree.update(int(i), float(pi) ** self.alpha)

    def __len__(self) -> int:
        return self.size
