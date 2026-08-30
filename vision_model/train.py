"""세그멘테이션 학습 — 합성 데이터셋 → U-Net → ONNX.

    python -m vision_model.train --data data/seg_v1 --epochs 30 --out models/seg_v1

데이터가 없으면 먼저 만든다.

    python -m datagen.generate --count 4000 --out data/seg_v1 --size 512 --workers 8

정직한 한계
-----------
학습 데이터가 전부 합성이다. 실촬영 분포와는 다르므로 여기서 나온 IoU 를
현장 성능으로 읽으면 안 된다. 이 학습의 목적은 두 가지다.

1. 고전 영상처리로는 불가능한 **다중 클래스 분류**(균열/박리/백태/철근노출)를
   같은 인터페이스 안에서 가능하게 만드는 것
2. 실촬영 라벨이 확보됐을 때 곧바로 이어 학습할 **파이프라인을 완성**하는 것

실사 데이터가 들어오면 여기에 섞어 미세조정(fine-tune)하면 된다.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision_model.model import (  # noqa: E402
    CLASS_NAMES,
    NUM_CLASSES,
    SegLoss,
    UNet,
    confusion_counts,
)


class SynthSegDataset(Dataset):
    """datagen 산출물을 읽어 (이미지, 라벨) 쌍으로 준다."""

    def __init__(self, root: Path, ids: list[str], size: int = 384, train: bool = True):
        self.root = root
        self.ids = ids
        self.size = size
        self.train = train

    def __len__(self) -> int:
        return len(self.ids)

    def __getitem__(self, i: int):
        stem = self.ids[i]
        img = cv2.imread(str(self.root / "images" / f"{stem}.jpg"))
        lab = cv2.imread(str(self.root / "masks" / f"{stem}_label.png"), cv2.IMREAD_GRAYSCALE)
        if img is None or lab is None:
            raise FileNotFoundError(stem)

        if img.shape[0] != self.size:
            img = cv2.resize(img, (self.size, self.size), interpolation=cv2.INTER_AREA)
            # 라벨은 최근접으로만 줄인다. 보간하면 없는 클래스 값이 생긴다.
            lab = cv2.resize(lab, (self.size, self.size), interpolation=cv2.INTER_NEAREST)

        if self.train:
            # 기하 증강만 쓴다. 밝기·노이즈는 생성기가 이미 넣었다.
            if random.random() < 0.5:
                img, lab = img[:, ::-1], lab[:, ::-1]
            if random.random() < 0.5:
                img, lab = img[::-1], lab[::-1]
            k = random.randint(0, 3)
            if k:
                img, lab = np.rot90(img, k), np.rot90(lab, k)

        x = torch.from_numpy(np.ascontiguousarray(img[:, :, ::-1])).permute(2, 0, 1).float() / 255.0
        y = torch.from_numpy(np.ascontiguousarray(lab)).long()
        return x, y


def load_splits(root: Path) -> tuple[list[str], list[str]]:
    sp = json.loads((root / "splits.json").read_text(encoding="utf-8"))
    return sp["train"], sp["val"]


def class_weights(root: Path, ids: list[str], sample: int = 300) -> torch.Tensor:
    """화소 빈도의 역수(제곱근 완화)로 클래스 가중을 만든다.

    역수를 그대로 쓰면 극희소 클래스의 가중이 수천 배가 되어 학습이 발산한다.
    제곱근으로 눌러 균형과 안정 사이를 잡는다.
    """
    counts = np.zeros(NUM_CLASSES, np.float64)
    for stem in random.sample(ids, min(sample, len(ids))):
        lab = cv2.imread(str(root / "masks" / f"{stem}_label.png"), cv2.IMREAD_GRAYSCALE)
        if lab is None:
            continue
        counts += np.bincount(lab.reshape(-1), minlength=NUM_CLASSES)
    freq = counts / max(counts.sum(), 1)
    w = 1.0 / np.sqrt(np.maximum(freq, 1e-6))
    w = w / w.mean()
    # 아예 등장하지 않은 클래스는 1로 둔다 — 가중이 무의미하다
    w[counts == 0] = 1.0
    return torch.tensor(w, dtype=torch.float32)


@torch.no_grad()
def evaluate(model, loader, device) -> dict:
    model.eval()
    inter = torch.zeros(NUM_CLASSES, device=device)
    psum = torch.zeros(NUM_CLASSES, device=device)
    tsum = torch.zeros(NUM_CLASSES, device=device)
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        i, p, t = confusion_counts(model(x), y)
        inter += i
        psum += p
        tsum += t
    union = psum + tsum - inter
    iou = (inter / union.clamp(min=1)).cpu().numpy()
    present = (tsum > 0).cpu().numpy()
    return {
        "iou": {CLASS_NAMES[c]: round(float(iou[c]), 4) for c in range(NUM_CLASSES) if present[c]},
        "miou": round(float(iou[present].mean()), 4) if present.any() else 0.0,
        "crack_iou": round(float(iou[1]), 4),
    }


def export_onnx(model, out: Path, size: int) -> Path:
    """ONNX 로 내보낸다 — 서비스에 torch 를 올리지 않기 위함."""
    model.eval().cpu()
    path = out / "segmenter.onnx"
    dummy = torch.zeros(1, 3, size, size)
    torch.onnx.export(
        model, dummy, str(path),
        input_names=["image"], output_names=["logits"],
        dynamic_axes={"image": {0: "n", 2: "h", 3: "w"},
                      "logits": {0: "n", 2: "h", 3: "w"}},
        opset_version=17,
    )
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="결함 세그멘테이션 학습")
    ap.add_argument("--data", type=str, default="data/seg_v1")
    ap.add_argument("--out", type=str, default="models/seg_v1")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--size", type=int, default=384)
    ap.add_argument("--base", type=int, default=24)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="학습 표본 상한 (빠른 확인용)")
    a = ap.parse_args(argv)

    root = Path(a.data)
    if not (root / "splits.json").exists():
        print(f"데이터가 없습니다: {root}")
        print("  python -m datagen.generate --count 4000 --out "
              f"{a.data} --size 512 --workers 8")
        return 1

    torch.manual_seed(0)
    random.seed(0)
    np.random.seed(0)

    tr_ids, va_ids = load_splits(root)
    if a.limit:
        tr_ids, va_ids = tr_ids[: a.limit], va_ids[: max(8, a.limit // 8)]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.set_num_threads(max(1, (torch.get_num_threads() or 4) // 2))

    tr = DataLoader(
        SynthSegDataset(root, tr_ids, a.size, True),
        batch_size=a.batch, shuffle=True, num_workers=a.workers, drop_last=True,
    )
    va = DataLoader(
        SynthSegDataset(root, va_ids, a.size, False),
        batch_size=a.batch, shuffle=False, num_workers=a.workers,
    )

    model = UNet(base=a.base).to(device)
    n_par = sum(p.numel() for p in model.parameters())
    w = class_weights(root, tr_ids).to(device)
    crit = SegLoss(class_weights=w)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=a.lr, total_steps=max(1, a.epochs * len(tr))
    )

    print(f"학습 {len(tr_ids)}장 · 검증 {len(va_ids)}장 · {device} · 파라미터 {n_par/1e6:.2f}M")
    print("클래스 가중:", {CLASS_NAMES[i]: round(float(v), 2) for i, v in enumerate(w)})

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    history, best = [], -1.0
    t0 = time.time()

    for ep in range(1, a.epochs + 1):
        model.train()
        total = 0.0
        for x, y in tr:
            x, y = x.to(device), y.to(device)
            opt.zero_grad(set_to_none=True)
            loss = crit(model(x), y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            sched.step()
            total += float(loss.item())

        m = evaluate(model, va, device)
        m.update(epoch=ep, loss=round(total / max(len(tr), 1), 4),
                 elapsed=round(time.time() - t0, 1))
        history.append(m)
        print(f"  ep {ep:>3}/{a.epochs}  loss {m['loss']:.4f}  "
              f"mIoU {m['miou']:.4f}  crack {m['crack_iou']:.4f}  ({m['elapsed']:.0f}s)",
              flush=True)

        # 균열 IoU 를 기준으로 고른다. 폭 판정의 근거이므로 다른 클래스보다 중요하다.
        if m["crack_iou"] > best:
            best = m["crack_iou"]
            torch.save({"state": model.state_dict(), "base": a.base,
                        "size": a.size, "classes": CLASS_NAMES}, out / "best.pt")

    ckpt = torch.load(out / "best.pt", map_location="cpu")
    model = UNet(base=ckpt["base"])
    model.load_state_dict(ckpt["state"])
    onnx_path = export_onnx(model, out, a.size)

    report = {
        "data": str(root), "train": len(tr_ids), "val": len(va_ids),
        "epochs": a.epochs, "size": a.size, "base": a.base,
        "params_m": round(n_par / 1e6, 3),
        "best_crack_iou": best, "final": history[-1] if history else None,
        "history": history,
        "onnx": str(onnx_path.relative_to(Path.cwd())) if onnx_path.is_relative_to(Path.cwd()) else str(onnx_path),
        "caveat": "합성 데이터만으로 학습했습니다. 실촬영 성능은 별도 측정이 필요합니다.",
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n최고 균열 IoU {best:.4f} · ONNX → {onnx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
