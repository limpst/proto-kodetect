"""오검출 분류기 학습 — 합성 데이터의 정답으로 형상 특징 가중치를 학습한다.

왜 학습하는가
-------------
형상 규칙을 손으로 늘리면 하나를 막을 때마다 다른 하나가 샌다. "세장비 2.5 이상,
충실도 0.62 이하, 대비 6 이상" 같은 임계는 그 값을 왜 그렇게 정했는지 근거가 없다.
정답이 있는 합성 표본에서 가중치를 학습시키면 그 근거가 데이터에서 나온다.

왜 로지스틱 회귀인가
--------------------
계수를 사람이 읽어 검증할 수 있기 때문이다. "사행도 계수가 +1.8" 이면 사행할수록
균열로 본다는 뜻이고, 이는 도메인 지식과 대조 가능하다. 안전 판정에 들어가는
구성요소는 설명 가능해야 한다. 트리 앙상블이 몇 점 더 나와도 그 이유를 설명할 수
없으면 책임기술자가 검토할 수 없다.

사용
----
    python -m datagen.fit_filter --data data/bench_train --out models/fp_filter.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.vision import FEATURE_NAMES, CrackDetector  # noqa: E402
from datagen.evaluate import _match  # noqa: E402


# ─── 학습 표본 수집 ────────────────────────────────────────────
def collect(data_dir: Path, limit: int | None, sensitivity: float) -> tuple[np.ndarray, np.ndarray]:
    """검출 후보를 정답과 매칭해 (특징, 라벨) 을 만든다.

    분류기를 학습할 때는 후보를 전부 받아야 한다. 신뢰도 문턱으로 미리 걸러내면
    "걸러진 것"에 대한 라벨이 없어져 학습이 불가능하다.
    """
    detector = CrackDetector(sensitivity=sensitivity, min_confidence=0.0)
    detector.fp_filter = None                 # 자기 자신을 쓰지 않는다

    records = [
        json.loads(line)
        for line in (data_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if limit:
        records = records[:limit]

    X: list[np.ndarray] = []
    y: list[int] = []
    n_pos = n_neg = 0

    for k, rec in enumerate(records, 1):
        image = cv2.imread(str(data_dir / rec["image"]))
        if image is None:
            continue
        result = detector.detect(image, rec["mm_per_px"])
        if not result.cracks:
            continue

        gt = [d for d in rec["defects"] if d["defect_type"] == "crack"]
        tol = max(6.0, 0.012 * max(image.shape[:2]))
        pairs = _match(
            [d.get("polyline") or [] for d in gt],
            [c.polyline for c in result.cracks],
            tol,
            0.30,
        )
        matched = {j for _, j in pairs}

        for j, c in enumerate(result.cracks):
            vec = np.array(
                [c.features.get(name, 0.0) for name in FEATURE_NAMES], np.float32
            )
            X.append(vec)
            label = 1 if j in matched else 0
            y.append(label)
            n_pos += label
            n_neg += 1 - label

        if k % 25 == 0:
            print(f"  {k}/{len(records)}  후보 {len(y)}건 (정 {n_pos} / 오 {n_neg})",
                  flush=True)

    return np.array(X, np.float32), np.array(y, np.int32)


# ─── 로지스틱 회귀 (numpy) ─────────────────────────────────────
def fit_logistic(
    X: np.ndarray,
    y: np.ndarray,
    *,
    l2: float = 1e-2,
    lr: float = 0.2,
    epochs: int = 4000,
    class_balance: bool = True,
) -> tuple[np.ndarray, float, list[float]]:
    """L2 정규화 로지스틱 회귀. 클래스 불균형은 표본 가중으로 보정한다."""
    n, d = X.shape
    w = np.zeros(d, np.float64)
    b = 0.0

    if class_balance:
        n_pos = max(int(y.sum()), 1)
        n_neg = max(n - n_pos, 1)
        sw = np.where(y == 1, n / (2.0 * n_pos), n / (2.0 * n_neg))
    else:
        sw = np.ones(n)
    sw = sw / sw.sum() * n

    losses: list[float] = []
    for e in range(epochs):
        z = X @ w + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        err = (p - y) * sw
        gw = X.T @ err / n + l2 * w
        gb = float(err.sum() / n)
        w -= lr * gw
        b -= lr * gb
        if e % 400 == 0:
            eps = 1e-9
            loss = float(
                -(sw * (y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))).mean()
                + 0.5 * l2 * float(w @ w)
            )
            losses.append(round(loss, 5))
    return w, b, losses


def roc_auc(y: np.ndarray, score: np.ndarray) -> float:
    """순위 기반 AUC (Mann-Whitney U). 동점은 평균 순위로 처리."""
    pos, neg = score[y == 1], score[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(score)
    ranks = np.empty(len(score), np.float64)
    ranks[order] = np.arange(1, len(score) + 1)
    # 동점 평균 순위
    s = score[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        if j > i:
            ranks[order[i : j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    r_pos = ranks[y == 1].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def pr_at_threshold(y: np.ndarray, score: np.ndarray, thr: float) -> tuple[float, float]:
    pred = score >= thr
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    p = tp / max(tp + fp, 1)
    r = tp / max(tp + fn, 1)
    return p, r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="오검출 분류기 학습")
    ap.add_argument("--data", type=str, default="data/bench_train")
    ap.add_argument("--out", type=str, default="models/fp_filter.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sensitivity", type=float, default=1.0)
    ap.add_argument("--val-frac", type=float, default=0.25)
    ap.add_argument("--l2", type=float, default=1e-2)
    a = ap.parse_args(argv)

    print(f"학습 표본 수집: {a.data}")
    X, y = collect(Path(a.data), a.limit or None, a.sensitivity)
    if len(X) < 50:
        print(f"표본이 부족합니다 ({len(X)}건). 데이터셋을 늘리십시오.")
        return 1
    print(f"수집 완료: {len(X)}건 (정 {int(y.sum())} / 오 {int((1 - y).sum())})")

    # 검증 분할 — 학습에 쓰지 않은 표본으로만 성능을 본다
    rng = np.random.default_rng(7)
    idx = rng.permutation(len(X))
    n_val = int(len(X) * a.val_frac)
    val_idx, tr_idx = idx[:n_val], idx[n_val:]

    mean = X[tr_idx].mean(axis=0)
    std = X[tr_idx].std(axis=0)
    std = np.where(std > 1e-9, std, 1.0)
    Z = (X - mean) / std

    w, b, losses = fit_logistic(Z[tr_idx], y[tr_idx], l2=a.l2)

    s_tr = 1 / (1 + np.exp(-np.clip(Z[tr_idx] @ w + b, -30, 30)))
    s_va = 1 / (1 + np.exp(-np.clip(Z[val_idx] @ w + b, -30, 30)))
    auc_tr, auc_va = roc_auc(y[tr_idx], s_tr), roc_auc(y[val_idx], s_va)

    print(f"\n손실 추이: {losses}")
    print(f"AUC  학습 {auc_tr:.4f}  검증 {auc_va:.4f}")
    print("\n문턱별 검증 정밀도/재현율")
    for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        p, r = pr_at_threshold(y[val_idx], s_va, thr)
        f1 = 2 * p * r / max(p + r, 1e-9)
        print(f"  {thr:.1f}  P={p:.3f}  R={r:.3f}  F1={f1:.3f}")

    print("\n특징 계수 (표준화 기준 · 절댓값 순)")
    for name, coef in sorted(
        zip(FEATURE_NAMES, w), key=lambda kv: -abs(kv[1])
    ):
        arrow = "균열←" if coef > 0 else "오검출←"
        print(f"  {name:16s} {coef:+7.3f}  {arrow}")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "features": list(FEATURE_NAMES),
                "mean": [float(v) for v in mean],
                "std": [float(v) for v in std],
                "weights": [float(v) for v in w],
                "bias": float(b),
                "trained_on": str(a.data),
                "samples": int(len(X)),
                "positives": int(y.sum()),
                "auc_train": round(float(auc_tr), 4),
                "auc_val": round(float(auc_va), 4),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
