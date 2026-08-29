"""계층적 위험회피 강화학습 에이전트.

구조
----
* **Manager** — 몇 년에 한 번 예산 기조(관찰/예방/집중/긴급)를 정한다.
  느린 시간축에서 자원 배분을 결정하는 역할.
* **Worker**  — 매년 부재별 조치를 정한다. Manager가 정한 기조가 관측에 포함되어
  Worker의 선택을 조건화한다.

두 정책 모두 Branching Dueling C51 + NoisyNet + PER + n-step 으로 학습하며,
행동 선택 시 기댓값이 아니라 **CVaR**(하위 꼬리 조건부 기댓값)를 최대화한다.
구조 안전에서는 평균적으로 좋은 정책보다, 최악의 경우에 무너지지 않는 정책이
옳기 때문이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
import torch.nn.functional as F

from .networks import BranchingC51, cvar, expected_value
from .replay import PrioritizedReplay


@dataclass
class AgentConfig:
    obs_dim: int
    n_branches: int
    n_actions: int
    n_atoms: int = 51
    v_min: float = -30.0
    v_max: float = 30.0
    hidden: int = 256
    lr: float = 2.5e-4
    gamma: float = 0.97
    n_step: int = 3
    batch_size: int = 64
    buffer_size: int = 100_000
    target_sync: int = 500
    learn_start: int = 1_000
    risk_alpha: float = 0.35        # 1.0이면 위험중립(기댓값)
    grad_clip: float = 10.0
    device: str = "cpu"


def project_distribution(
    next_probs: torch.Tensor,
    rewards: torch.Tensor,
    dones: torch.Tensor,
    support: torch.Tensor,
    gamma_n: float,
) -> torch.Tensor:
    """C51 카테고리 투영 — 벨만 갱신 후 원자 격자에 다시 올린다.

    next_probs: (batch, atoms) — 목표 행동의 분포
    반환:       (batch, atoms)
    """
    batch, n_atoms = next_probs.shape
    v_min, v_max = float(support[0]), float(support[-1])
    delta_z = (v_max - v_min) / (n_atoms - 1)

    # Tz = r + gamma^n * z  (종료 시에는 r 만)
    tz = rewards.unsqueeze(1) + (1.0 - dones).unsqueeze(1) * gamma_n * support.unsqueeze(0)
    tz = tz.clamp(v_min, v_max)
    b = (tz - v_min) / delta_z
    lower = b.floor().long()
    upper = b.ceil().long()

    # 정확히 격자에 떨어지는 경우 질량이 사라지지 않도록 보정
    lower[(upper > 0) & (lower == upper)] -= 1
    upper[(lower < n_atoms - 1) & (lower == upper)] += 1

    proj = torch.zeros_like(next_probs)
    offset = (
        torch.arange(batch, device=next_probs.device).unsqueeze(1) * n_atoms
    ).expand(batch, n_atoms)
    proj.view(-1).index_add_(
        0, (lower + offset).view(-1), (next_probs * (upper.float() - b)).view(-1)
    )
    proj.view(-1).index_add_(
        0, (upper + offset).view(-1), (next_probs * (b - lower.float())).view(-1)
    )
    return proj


class BranchingRainbowAgent:
    """단일 수준(Worker 또는 Manager) 에이전트."""

    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self.device = torch.device(cfg.device)
        kw = dict(
            obs_dim=cfg.obs_dim,
            n_branches=cfg.n_branches,
            n_actions=cfg.n_actions,
            n_atoms=cfg.n_atoms,
            v_min=cfg.v_min,
            v_max=cfg.v_max,
            hidden=cfg.hidden,
        )
        self.online = BranchingC51(**kw).to(self.device)
        self.target = BranchingC51(**kw).to(self.device)
        self.target.load_state_dict(self.online.state_dict())
        self.target.eval()

        self.optimizer = torch.optim.Adam(self.online.parameters(), lr=cfg.lr, eps=1.5e-4)
        self.buffer = PrioritizedReplay(
            cfg.buffer_size,
            cfg.obs_dim,
            cfg.n_branches,
            n_step=cfg.n_step,
            gamma=cfg.gamma,
        )
        self.support = self.online.support
        self.steps = 0

    # ─── 행동 선택 ────────────────────────────────────────────
    @torch.no_grad()
    def act(self, obs: np.ndarray, *, greedy: bool = False) -> np.ndarray:
        self.online.train(not greedy)   # NoisyNet: 평가 시에는 잡음을 끈다
        t = torch.as_tensor(obs, dtype=torch.float32, device=self.device).unsqueeze(0)
        probs = self.online.probabilities(t)          # (1, B, A, atoms)
        score = cvar(probs, self.support, self.cfg.risk_alpha)   # (1, B, A)
        return score.argmax(dim=-1).squeeze(0).cpu().numpy()

    @torch.no_grad()
    def action_values(self, obs: np.ndarray) -> dict:
        """설명용 — 각 분기·행동의 기댓값과 CVaR을 함께 돌려준다."""
        self.online.eval()
        t = torch.as_tensor(obs, dtype=torch.float32, device=self.device).unsqueeze(0)
        probs = self.online.probabilities(t)
        return {
            "expected": expected_value(probs, self.support).squeeze(0).cpu().numpy(),
            "cvar": cvar(probs, self.support, self.cfg.risk_alpha)
            .squeeze(0)
            .cpu()
            .numpy(),
        }

    # ─── 학습 ─────────────────────────────────────────────────
    def remember(self, obs, action, reward, next_obs, done) -> None:
        self.buffer.add(obs, action, reward, next_obs, done)

    def learn(self) -> float | None:
        cfg = self.cfg
        if len(self.buffer) < max(cfg.learn_start, cfg.batch_size):
            return None

        obs, actions, rewards, next_obs, dones, weights, idxs = self.buffer.sample(
            cfg.batch_size, self.steps
        )
        obs_t = torch.as_tensor(obs, device=self.device)
        next_t = torch.as_tensor(next_obs, device=self.device)
        act_t = torch.as_tensor(actions, device=self.device)
        rew_t = torch.as_tensor(rewards, device=self.device)
        done_t = torch.as_tensor(dones, device=self.device)
        w_t = torch.as_tensor(weights, device=self.device)

        self.online.reset_noise()
        self.target.reset_noise()

        # 현재 분포: 실제 취한 행동의 log 확률
        log_probs = self.online(obs_t)                        # (B, br, A, atoms)
        idx = act_t.unsqueeze(-1).unsqueeze(-1).expand(
            -1, -1, 1, cfg.n_atoms
        )
        chosen_log = log_probs.gather(2, idx).squeeze(2)      # (B, br, atoms)

        with torch.no_grad():
            # Double DQN — 행동은 online 이 고르고, 분포는 target 이 준다
            next_online = self.online.probabilities(next_t)
            best = cvar(next_online, self.support, cfg.risk_alpha).argmax(-1)
            next_target = self.target.probabilities(next_t)
            bidx = best.unsqueeze(-1).unsqueeze(-1).expand(-1, -1, 1, cfg.n_atoms)
            next_probs = next_target.gather(2, bidx).squeeze(2)   # (B, br, atoms)

            gamma_n = cfg.gamma**cfg.n_step
            n_br = next_probs.shape[1]
            flat = next_probs.reshape(-1, cfg.n_atoms)
            proj = project_distribution(
                flat,
                rew_t.repeat_interleave(n_br),
                done_t.repeat_interleave(n_br),
                self.support,
                gamma_n,
            ).view(-1, n_br, cfg.n_atoms)

        # 분기별 교차엔트로피 — 분기 축은 평균, 배치 축은 PER 가중 평균
        per_sample = -(proj * chosen_log).sum(-1).mean(dim=1)
        loss = (w_t * per_sample).mean()

        self.optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.online.parameters(), cfg.grad_clip)
        self.optimizer.step()

        self.buffer.update_priorities(idxs, per_sample.detach().cpu().numpy())
        self.steps += 1
        if self.steps % cfg.target_sync == 0:
            self.target.load_state_dict(self.online.state_dict())
        return float(loss.item())

    # ─── 저장/복원 ────────────────────────────────────────────
    def save(self, path) -> None:
        torch.save(
            {"online": self.online.state_dict(), "cfg": self.cfg.__dict__}, str(path)
        )

    def load(self, path) -> None:
        state = torch.load(str(path), map_location=self.device)
        self.online.load_state_dict(state["online"])
        self.target.load_state_dict(state["online"])


class HierarchicalAgent:
    """Manager(느린 축) + Worker(빠른 축) 결합."""

    def __init__(
        self,
        obs_dim: int,
        n_members: int,
        n_worker_actions: int,
        n_manager_actions: int,
        *,
        manager_period: int = 3,
        risk_alpha: float = 0.35,
        device: str = "cpu",
    ) -> None:
        self.manager_period = manager_period
        self.worker = BranchingRainbowAgent(
            AgentConfig(
                obs_dim=obs_dim,
                n_branches=n_members,
                n_actions=n_worker_actions,
                risk_alpha=risk_alpha,
                device=device,
            )
        )
        self.manager = BranchingRainbowAgent(
            AgentConfig(
                obs_dim=obs_dim,
                n_branches=1,
                n_actions=n_manager_actions,
                risk_alpha=risk_alpha,
                hidden=128,
                n_step=manager_period,
                device=device,
            )
        )

    def act_manager(self, obs, *, greedy: bool = False) -> int:
        return int(self.manager.act(obs, greedy=greedy)[0])

    def act_worker(self, obs, *, greedy: bool = False) -> np.ndarray:
        return self.worker.act(obs, greedy=greedy)

    def save(self, dir_path) -> None:
        from pathlib import Path

        d = Path(dir_path)
        d.mkdir(parents=True, exist_ok=True)
        self.worker.save(d / "worker.pt")
        self.manager.save(d / "manager.pt")

    def load(self, dir_path) -> None:
        from pathlib import Path

        d = Path(dir_path)
        self.worker.load(d / "worker.pt")
        self.manager.load(d / "manager.pt")
