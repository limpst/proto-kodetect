"""균열 분할망 학습 — 합성 데이터로 후보 생성을 배우게 한다.

무엇을 배우게 하는가
---------------------
정답은 `SynthSample.crack_mask` 다. **균열만** 양성이고 박리·백태·철근노출은
음성이다. 그래야 모델이 "어둡고 얼룩진 곳"이 아니라 "가늘고 긴 선"을 배운다.
다른 결함을 양성으로 섞으면 벤치마크의 `fp_on_other_defect` 가 그대로 늘어난다.

학습 조각 뽑기
---------------
타일을 무작위로 자르면 대부분 배경만 담긴다. 균열 픽셀이 0.1~1% 인 데이터에서
그렇게 하면 양성 조각이 거의 안 나온다. 그래서 **균열을 포함한 조각과 순수 배경
조각의 비율을 고정**해서 뽑는다. 배경 조각을 빼면 안 된다 — 표면 얼룩을 균열로
부르지 않는 법은 배경에서만 배운다.

증강
----
실제 촬영이 갖는 변이만 넣는다. 회전·반전은 균열의 방향성에 편향을 주지 않기
위해, 밝기·대비·블러·노이즈는 촬영 조건 차이를 흉내내기 위해. 색 왜곡 같은
비현실적 증강은 넣지 않는다 — 회색조 콘크리트에서 일어나지 않는 일이다.

실행:
    python -m datagen.train_seg --samples 900 --epochs 12
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from .synth import generate_sample  # noqa: E402

PATCH = 256
POS_RATIO = 0.7          # 조각 중 균열을 포함한 것의 비율
OUT = ROOT / "models" / "crack_seg.pt"


# ─── 조각 뽑기 ─────────────────────────────────────────────────
def _crop(img, mask, y, x):
    return img[y : y + PATCH, x : x + PATCH], mask[y : y + PATCH, x : x + PATCH]


def patches_from(sample, rng: np.random.Generator, n: int) -> list[tuple]:
    """한 표본에서 학습 조각 n 개."""
    img = sample.image
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = (sample.crack_mask > 0).astype(np.float32)
    h, w = img.shape[:2]
    if h < PATCH or w < PATCH:
        return []

    ys, xs = np.nonzero(m)
    out = []
    for _ in range(n):
        want_pos = len(ys) > 0 and rng.random() < POS_RATIO
        if want_pos:
            k = int(rng.integers(len(ys)))
            cy = int(np.clip(ys[k] - PATCH // 2 + rng.integers(-40, 41), 0, h - PATCH))
            cx = int(np.clip(xs[k] - PATCH // 2 + rng.integers(-40, 41), 0, w - PATCH))
        else:
            cy = int(rng.integers(0, h - PATCH + 1))
            cx = int(rng.integers(0, w - PATCH + 1))
        out.append(_crop(img, m, cy, cx))
    return out


def augment(img: np.ndarray, mask: np.ndarray, rng: np.random.Generator):
    """촬영 조건 차이를 흉내낸다. 기하 변환은 마스크에도 똑같이 건다."""
    k = int(rng.integers(4))
    if k:
        img, mask = np.rot90(img, k).copy(), np.rot90(mask, k).copy()
    if rng.random() < 0.5:
        img, mask = np.fliplr(img).copy(), np.fliplr(mask).copy()

    f = img.astype(np.float32)
    # 노출·감마 — 자동 노출이 프레임마다 다르다
    f = np.clip(f * rng.uniform(0.75, 1.3), 0, 255)
    g = rng.uniform(0.75, 1.35)
    f = np.power(f / 255.0, g) * 255.0
    # 초점/모션 블러 — 마스크는 흐리지 않는다. 정답 위치는 그대로다
    if rng.random() < 0.35:
        s = float(rng.uniform(0.6, 1.6))
        f = cv2.GaussianBlur(f, (0, 0), s)
    if rng.random() < 0.5:
        f += rng.normal(0, rng.uniform(1.5, 7.0), f.shape)
    return np.clip(f, 0, 255).astype(np.uint8), mask


def build(n_samples: int, per_sample: int, seed: int, size=(768, 768)):
    rng = np.random.default_rng(seed)
    X, Y = [], []
    for i in range(n_samples):
        s = generate_sample(seed * 100003 + i, size=size)
        for img, m in patches_from(s, rng, per_sample):
            img, m = augment(img, m, rng)
            X.append(img)
            Y.append(m)
        if (i + 1) % 100 == 0:
            print(f"  표본 {i+1}/{n_samples} · 조각 {len(X)}", flush=True)
    return np.stack(X), np.stack(Y)


# ─── 평가 ──────────────────────────────────────────────────────
def pixel_scores(net, torch, X, Y, thr=0.45, bs=32) -> dict:
    """픽셀 단위 정밀도·재현율.

    이것은 최종 지표가 아니다. 최종 판단은 `datagen.evaluate` 의 균열 단위
    매칭으로 한다 — 픽셀이 조금 겹치는 것과 균열 하나를 제대로 잡는 것은 다르다.
    여기서는 학습이 진행되는지만 본다.
    """
    net.eval()
    tp = fp = fn = 0
    with torch.no_grad():
        for i in range(0, len(X), bs):
            xb = torch.from_numpy(X[i : i + bs]).float().unsqueeze(1) / 255.0
            yb = torch.from_numpy(Y[i : i + bs]).float()
            p = (torch.sigmoid(net(xb)).squeeze(1) >= thr).float()
            tp += float((p * yb).sum())
            fp += float((p * (1 - yb)).sum())
            fn += float(((1 - p) * yb).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=900)
    ap.add_argument("--per-sample", type=int, default=6)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--base", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)

    import torch

    from app.services.segnet import UNetSmall, dice_bce_loss

    torch.set_num_threads(a.threads)
    torch.manual_seed(a.seed)

    print(f"학습 조각 생성 — 표본 {a.samples} × {a.per_sample}", flush=True)
    X, Y = build(a.samples, a.per_sample, a.seed)
    # 검증은 학습에 쓰지 않은 시드에서 따로 만든다. 같은 표본에서 자른 조각을
    # 나누면 배경 질감이 겹쳐 성능이 부풀려진다.
    Xv, Yv = build(max(40, a.samples // 12), 4, a.seed + 90001)
    print(f"학습 {len(X)} 조각 · 검증 {len(Xv)} 조각 "
          f"(양성 픽셀 {Y.mean()*100:.2f}%)", flush=True)

    net = UNetSmall(base=a.base)
    n_par = sum(p.numel() for p in net.parameters())
    opt = torch.optim.AdamW(net.parameters(), lr=a.lr, weight_decay=1e-4)
    steps = a.epochs * max(1, len(X) // a.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=steps)
    print(f"파라미터 {n_par/1e6:.2f}M · 스텝 {steps}", flush=True)

    rng = np.random.default_rng(a.seed)
    best = -1.0
    best_scores: dict = {}
    t0 = time.time()
    for ep in range(a.epochs):
        net.train()
        idx = rng.permutation(len(X))
        tot = 0.0
        nb = 0
        for i in range(0, len(idx) - a.batch + 1, a.batch):
            b = idx[i : i + a.batch]
            xb = torch.from_numpy(X[b]).float().unsqueeze(1) / 255.0
            yb = torch.from_numpy(Y[b]).float().unsqueeze(1)
            loss = dice_bce_loss(net(xb), yb)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            tot += float(loss)
            nb += 1
        sc = pixel_scores(net, torch, Xv, Yv)
        print(f"  epoch {ep+1:>2}/{a.epochs}  손실 {tot/max(nb,1):.4f}  "
              f"정밀도 {sc['precision']:.3f}  재현율 {sc['recall']:.3f}  "
              f"F1 {sc['f1']:.3f}  ({time.time()-t0:.0f}s)", flush=True)
        if sc["f1"] > best:
            best = sc["f1"]
            best_scores = sc
            a.out.parent.mkdir(parents=True, exist_ok=True)
            torch.save(
                {
                    "state_dict": net.state_dict(),
                    "base": a.base,
                    "patch": PATCH,
                    "epoch": ep + 1,
                    "pixel_scores": sc,
                    "trained_on": "synthetic",
                    "note": "균열만 양성. 박리·백태·철근노출은 음성으로 학습.",
                },
                a.out,
            )

    print(f"\n최고 픽셀 F1 {best:.4f} {best_scores} → {a.out}")
    print("검출기 단위 성능은 `python -m datagen.evaluate` 로 확인하십시오 — "
          "픽셀 지표와 균열 단위 지표는 다릅니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
