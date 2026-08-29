"""유지관리 정책 학습 및 기준정책 대비 평가.

사용 예
-------
    python -m rl.train --episodes 400 --out models/rl_v1
    python -m rl.train --episodes 50 --members 4 --horizon 20   # 빠른 확인

산출
----
    models/<out>/worker.pt, manager.pt   학습된 정책
    models/<out>/report.json             학습 곡선 + 기준정책 비교 결과
report.json 은 웹 대시보드가 그대로 읽어 정책 비교 차트를 그린다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rl.env import (  # noqa: E402
    N_MANAGER_ACTIONS,
    N_WORKER_ACTIONS,
    EnvConfig,
    MaintenanceEnv,
    policy_periodic,
    policy_reactive,
)


def run_episode_baseline(env: MaintenanceEnv, policy, seed: int) -> dict:
    """기준정책 1회 에피소드 — 학습된 정책과 같은 지표로 집계한다."""
    env.reset(seed=seed)
    env.set_manager_action(1)
    total_r = total_cost = total_risk = 0.0
    worst = 0
    failure_years = 0
    while True:
        actions = policy(env)
        _, r, done, info = env.step(actions)
        total_r += r
        total_cost += info.cost
        total_risk += info.risk
        worst = max(worst, info.worst_grade)
        failure_years += int(info.failures > 0)
        if done:
            break
    return {
        "return": round(total_r, 2),
        "cost": round(total_cost, 1),
        "risk": round(total_risk, 1),
        "worst_grade": worst,
        "failure_years": failure_years,
    }


def evaluate_agent(agent, cfg: EnvConfig, episodes: int = 30) -> dict:
    env = MaintenanceEnv(cfg)
    rows = []
    for e in range(episodes):
        obs = env.reset(seed=10_000 + e)
        total_r = total_cost = total_risk = 0.0
        worst = 0
        failure_years = 0
        year = 0
        while True:
            if year % agent.manager_period == 0:
                env.set_manager_action(agent.act_manager(obs, greedy=True))
                obs = env.observe()
            actions = agent.act_worker(obs, greedy=True)
            obs, r, done, info = env.step(actions)
            total_r += r
            total_cost += info.cost
            total_risk += info.risk
            worst = max(worst, info.worst_grade)
            failure_years += int(info.failures > 0)
            year += 1
            if done:
                break
        rows.append(
            {
                "return": total_r,
                "cost": total_cost,
                "risk": total_risk,
                "worst_grade": worst,
                "failure_years": failure_years,
            }
        )
    agg = lambda k: float(np.mean([r[k] for r in rows]))  # noqa: E731
    return {
        "return": round(agg("return"), 2),
        "cost": round(agg("cost"), 1),
        "risk": round(agg("risk"), 1),
        "worst_grade": round(agg("worst_grade"), 2),
        "failure_years": round(agg("failure_years"), 2),
        "episodes": episodes,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="유지관리 정책 강화학습")
    p.add_argument("--episodes", type=int, default=300)
    p.add_argument("--members", type=int, default=6)
    p.add_argument("--horizon", type=int, default=30)
    p.add_argument("--budget", type=float, default=26.0)
    p.add_argument("--severity", type=float, default=1.0)
    p.add_argument("--risk-alpha", type=float, default=0.35,
                   help="CVaR 분위. 1.0이면 위험중립")
    p.add_argument("--manager-period", type=int, default=3)
    p.add_argument("--out", type=str, default="models/rl_v1")
    p.add_argument("--eval-episodes", type=int, default=30)
    a = p.parse_args(argv)

    from rl.agent import HierarchicalAgent  # torch 의존 — 필요한 시점에만 import

    cfg = EnvConfig(
        n_members=a.members,
        horizon=a.horizon,
        annual_budget=a.budget,
        environment_severity=a.severity,
        seed=7,
    )
    env = MaintenanceEnv(cfg)
    agent = HierarchicalAgent(
        obs_dim=env.obs_dim,
        n_members=a.members,
        n_worker_actions=N_WORKER_ACTIONS,
        n_manager_actions=N_MANAGER_ACTIONS,
        manager_period=a.manager_period,
        risk_alpha=a.risk_alpha,
    )

    curve = []
    t0 = time.time()
    for ep in range(a.episodes):
        obs = env.reset(seed=ep)
        ep_return = 0.0
        year = 0
        manager_obs = obs
        manager_reward = 0.0

        while True:
            if year % agent.manager_period == 0:
                if year > 0:
                    agent.manager.remember(
                        manager_obs, np.array([manager_action]),
                        manager_reward, obs, False,
                    )
                    agent.manager.learn()
                manager_obs = obs
                manager_reward = 0.0
                manager_action = agent.act_manager(obs)
                env.set_manager_action(manager_action)
                obs = env.observe()

            actions = agent.act_worker(obs)
            next_obs, reward, done, info = env.step(actions)
            agent.worker.remember(obs, actions, reward, next_obs, done)
            agent.worker.learn()

            obs = next_obs
            ep_return += reward
            manager_reward += reward
            year += 1
            if done:
                agent.manager.remember(
                    manager_obs, np.array([manager_action]), manager_reward, obs, True
                )
                agent.manager.learn()
                break

        curve.append(round(ep_return, 2))
        if (ep + 1) % max(1, a.episodes // 20) == 0:
            recent = np.mean(curve[-20:])
            print(
                f"  ep {ep + 1}/{a.episodes}  return(최근20) {recent:8.2f}  "
                f"({time.time() - t0:.0f}s)",
                flush=True,
            )

    out = Path(a.out)
    agent.save(out)

    # ─── 기준정책 대비 평가 ───────────────────────────────────
    base_env = MaintenanceEnv(cfg)
    reactive = [
        run_episode_baseline(base_env, policy_reactive, 10_000 + e)
        for e in range(a.eval_episodes)
    ]
    periodic = [
        run_episode_baseline(base_env, policy_periodic, 10_000 + e)
        for e in range(a.eval_episodes)
    ]
    agg = lambda rows, k: round(float(np.mean([r[k] for r in rows])), 2)  # noqa: E731

    report = {
        "config": {
            "members": a.members, "horizon": a.horizon, "budget": a.budget,
            "severity": a.severity, "risk_alpha": a.risk_alpha,
            "manager_period": a.manager_period, "episodes": a.episodes,
        },
        "training_curve": curve,
        "elapsed_sec": round(time.time() - t0, 1),
        "policies": {
            "사후보수 (현행 관행)": {
                "return": agg(reactive, "return"), "cost": agg(reactive, "cost"),
                "risk": agg(reactive, "risk"),
                "worst_grade": agg(reactive, "worst_grade"),
                "failure_years": agg(reactive, "failure_years"),
            },
            "정기보수 (주기 기반)": {
                "return": agg(periodic, "return"), "cost": agg(periodic, "cost"),
                "risk": agg(periodic, "risk"),
                "worst_grade": agg(periodic, "worst_grade"),
                "failure_years": agg(periodic, "failure_years"),
            },
            "강화학습 (HRL + CVaR)": evaluate_agent(agent, cfg, a.eval_episodes),
        },
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["policies"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
