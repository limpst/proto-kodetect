"""분기형 분포 Q 네트워크 (Branching Dueling C51 + NoisyNet).

왜 이 조합인가
--------------
* **분기(Branching)** — 부재 N개에 각각 5가지 조치를 고르므로 결합 행동공간이
  5^N 으로 폭발한다. 부재별 분기 출력으로 선형 규모(N x 5)로 낮춘다.
  (Action Branching Architectures, Tavakoli et al.)
* **분포형(C51)** — 안전 문제에서는 기댓값보다 꼬리가 중요하다. 수익(=음의 비용)의
  분포를 직접 학습하면, 행동 선택 시 CVaR 같은 위험회피 기준을 쓸 수 있다.
* **Dueling** — 대부분의 해에는 어떤 조치를 하든 큰 차이가 없다. 상태가치와
  행동이득을 분리하면 그런 상태에서 학습이 안정된다.
* **NoisyNet** — epsilon-greedy는 예산 제약이 있는 장기 문제에서 탐색이 비효율적이다.
  파라미터 공간에 잡음을 넣으면 상태 의존적이고 일관된 탐색이 된다.
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn
import torch.nn.functional as F


class NoisyLinear(nn.Module):
    """인수분해 가우시안 잡음을 갖는 선형층 (Fortunato et al., 2018)."""

    def __init__(self, in_features: int, out_features: int, sigma0: float = 0.5) -> None:
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.sigma0 = sigma0

        self.weight_mu = nn.Parameter(torch.empty(out_features, in_features))
        self.weight_sigma = nn.Parameter(torch.empty(out_features, in_features))
        self.register_buffer("weight_eps", torch.empty(out_features, in_features))

        self.bias_mu = nn.Parameter(torch.empty(out_features))
        self.bias_sigma = nn.Parameter(torch.empty(out_features))
        self.register_buffer("bias_eps", torch.empty(out_features))

        self.reset_parameters()
        self.reset_noise()

    def reset_parameters(self) -> None:
        bound = 1.0 / math.sqrt(self.in_features)
        self.weight_mu.data.uniform_(-bound, bound)
        self.bias_mu.data.uniform_(-bound, bound)
        self.weight_sigma.data.fill_(self.sigma0 / math.sqrt(self.in_features))
        self.bias_sigma.data.fill_(self.sigma0 / math.sqrt(self.out_features))

    @staticmethod
    def _scaled_noise(size: int, device) -> torch.Tensor:
        x = torch.randn(size, device=device)
        return x.sign() * x.abs().sqrt()

    def reset_noise(self) -> None:
        eps_in = self._scaled_noise(self.in_features, self.weight_mu.device)
        eps_out = self._scaled_noise(self.out_features, self.weight_mu.device)
        self.weight_eps.copy_(eps_out.outer(eps_in))
        self.bias_eps.copy_(eps_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.training:
            w = self.weight_mu + self.weight_sigma * self.weight_eps
            b = self.bias_mu + self.bias_sigma * self.bias_eps
        else:
            w, b = self.weight_mu, self.bias_mu
        return F.linear(x, w, b)


class BranchingC51(nn.Module):
    """관측 -> 분기별 행동가치 분포.

    출력 형상: (batch, n_branches, n_actions, n_atoms) — 각 (분기, 행동)마다
    수익 분포의 확률질량.
    """

    def __init__(
        self,
        obs_dim: int,
        n_branches: int,
        n_actions: int,
        n_atoms: int = 51,
        v_min: float = -30.0,
        v_max: float = 30.0,
        hidden: int = 256,
    ) -> None:
        super().__init__()
        self.n_branches = n_branches
        self.n_actions = n_actions
        self.n_atoms = n_atoms
        self.register_buffer("support", torch.linspace(v_min, v_max, n_atoms))
        self.delta_z = (v_max - v_min) / (n_atoms - 1)

        self.trunk = nn.Sequential(
            nn.Linear(obs_dim, hidden),
            nn.ReLU(inplace=True),
            nn.Linear(hidden, hidden),
            nn.ReLU(inplace=True),
        )
        # 상태가치는 분기 공통 — 전체 시설물의 가치를 하나로 본다
        self.value = nn.Sequential(
            NoisyLinear(hidden, hidden // 2),
            nn.ReLU(inplace=True),
            NoisyLinear(hidden // 2, n_atoms),
        )
        self.advantage = nn.Sequential(
            NoisyLinear(hidden, hidden),
            nn.ReLU(inplace=True),
            NoisyLinear(hidden, n_branches * n_actions * n_atoms),
        )

    def forward(self, obs: torch.Tensor) -> torch.Tensor:
        """반환: log 확률 (batch, B, A, atoms)."""
        h = self.trunk(obs)
        b = obs.shape[0]
        v = self.value(h).view(b, 1, 1, self.n_atoms)
        a = self.advantage(h).view(b, self.n_branches, self.n_actions, self.n_atoms)
        # 분기별로 행동 평균을 빼 식별성을 확보한다
        a = a - a.mean(dim=2, keepdim=True)
        return F.log_softmax(v + a, dim=-1)

    def probabilities(self, obs: torch.Tensor) -> torch.Tensor:
        return self.forward(obs).exp()

    def reset_noise(self) -> None:
        for m in self.modules():
            if isinstance(m, NoisyLinear):
                m.reset_noise()


def expected_value(probs: torch.Tensor, support: torch.Tensor) -> torch.Tensor:
    """분포의 기댓값 — 형상 (..., atoms) -> (...)."""
    return (probs * support).sum(-1)


def cvar(probs: torch.Tensor, support: torch.Tensor, alpha: float) -> torch.Tensor:
    """하위 alpha 분위의 조건부 기댓값 (CVaR).

    수익 분포이므로 '하위 꼬리'가 최악의 경우다. alpha=1.0 이면 기댓값과 같다.
    구조 안전처럼 최악 시나리오가 결정적인 문제에서는 alpha를 0.2~0.5로 두어
    위험회피적으로 행동하게 한다.
    """
    if alpha >= 0.999:
        return expected_value(probs, support)

    cum = probs.cumsum(-1)
    # 누적확률이 alpha를 넘기 전까지의 질량만 사용한다
    within = (cum <= alpha).to(probs.dtype)
    # 경계 원자는 남은 질량만큼만 부분 반영
    prev_cum = cum - probs
    partial = torch.clamp(alpha - prev_cum, min=0.0)
    weights = torch.where(within.bool(), probs, torch.minimum(partial, probs))
    total = weights.sum(-1).clamp(min=1e-8)
    return (weights * support).sum(-1) / total
