"""검출기 벤치마크 — 합성 데이터셋의 정답과 대조.

정답과 예측을 마스크 IoU로 1:1 매칭한 뒤 다음을 산출한다.

* 인스턴스 검출 P / R / F1  (IoU >= --iou)
* 균열폭 오차 MAE · 중앙값 · 편향(bias)
* 상태등급(a~e) 일치율 및 혼동행렬 — 실무에서 실제로 중요한 지표

사용 예
-------
    python -m datagen.evaluate --data data/smoke
    python -m datagen.evaluate --data data/synth_v1 --limit 500 --iou 0.3
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.grading import grade_by_crack_width  # noqa: E402
from app.services.vision import CrackDetector  # noqa: E402

GRADES = ["a", "b", "c", "d", "e"]


def _mask_from_bbox(shape: tuple[int, int], bbox: list[int]) -> np.ndarray:
    m = np.zeros(shape, np.uint8)
    x, y, w, h = bbox
    m[y : y + h, x : x + w] = 1
    return m


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = int(np.logical_and(a, b).sum())
    if inter == 0:
        return 0.0
    return inter / float(np.logical_or(a, b).sum())


def _buffer_overlap(a: list, b: list, tol: float) -> float:
    """a의 점들 중 b의 점에서 tol 이내에 있는 비율 (0~1).

    선형 구조 평가의 표준(완전성/정확성, buffer method)이다. 화면을 가로지르는
    균열은 외접 사각형이 서로 거의 같아 bbox IoU로는 구분되지 않으므로,
    중심선 간 거리로 매칭해야 한다.
    """
    if not a or not b:
        return 0.0
    pa = np.asarray(a, np.float32)
    pb = np.asarray(b, np.float32)
    d = np.sqrt(((pa[:, None, :] - pb[None, :, :]) ** 2).sum(-1))
    return float((d.min(axis=1) <= tol).mean())


def _match(
    gt_lines: list, pred_lines: list, tol: float, thr: float
) -> list[tuple[int, int]]:
    """중심선 버퍼 겹침 기준 탐욕적 1:1 매칭."""
    if not gt_lines or not pred_lines:
        return []
    scored = []
    for i, g in enumerate(gt_lines):
        for j, p in enumerate(pred_lines):
            completeness = _buffer_overlap(g, p, tol)
            correctness = _buffer_overlap(p, g, tol)
            # 두 방향 모두 만족해야 같은 균열로 본다 (조화평균)
            if completeness + correctness > 0:
                score = (
                    2 * completeness * correctness / (completeness + correctness)
                )
                scored.append((score, i, j))
    scored.sort(reverse=True)

    used_g: set[int] = set()
    used_p: set[int] = set()
    pairs = []
    for score, i, j in scored:
        if score < thr or i in used_g or j in used_p:
            continue
        used_g.add(i)
        used_p.add(j)
        pairs.append((i, j))
    return pairs


def evaluate(data_dir: Path, limit: int | None, iou_thr: float, **det_kwargs) -> dict:
    detector = CrackDetector(**det_kwargs)
    records = [
        json.loads(line)
        for line in (data_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if limit:
        records = records[:limit]

    tp = fp = fn = 0
    fp_on_other_defect = 0
    width_err: list[float] = []
    width_bias: list[float] = []
    confusion = Counter()
    grade_hit = 0
    # 폭 구간별 재현율 — 보수 대상(0.3mm 이상)을 놓치지 않는 것이 핵심이다
    bands = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 1.0), (1.0, 1e9)]
    band_gt = Counter()
    band_hit = Counter()

    def _band(width: float) -> str:
        for lo, hi in bands:
            if lo <= width < hi:
                return f"{lo:g}-{hi:g}mm" if hi < 1e9 else ">=1.0mm"
        return "?"

    for rec in records:
        image = cv2.imread(str(data_dir / rec["image"]))
        if image is None:
            continue
        result = detector.detect(image, rec["mm_per_px"])
        shape = image.shape[:2]

        gt = [d for d in rec["defects"] if d["defect_type"] == "crack"]
        other = [d for d in rec["defects"] if d["defect_type"] != "crack"]
        gt_boxes = [d["bbox"] for d in gt]
        pred_boxes = [list(c.bbox) for c in result.cracks]
        tol = max(6.0, 0.012 * max(shape))
        pairs = _match(
            [d.get("polyline") or [] for d in gt],
            [c.polyline for c in result.cracks],
            tol,
            iou_thr,
        )
        tp += len(pairs)
        fp += len(pred_boxes) - len(pairs)
        fn += len(gt_boxes) - len(pairs)

        # 다른 유형의 결함(박리·철근노출 등) 위에서 난 오검출은 별도로 센다.
        # 이는 유령 검출이 아니라 유형 분류의 문제이므로 성격이 다르다.
        matched_pred = {j for _, j in pairs}
        if other:
            other_masks = [_mask_from_bbox(shape, d["bbox"]) for d in other]
            for j, box in enumerate(pred_boxes):
                if j in matched_pred:
                    continue
                pm = _mask_from_bbox(shape, box)
                if any(_iou(pm, om) > 0.1 for om in other_masks):
                    fp_on_other_defect += 1

        matched_gt = {i for i, _ in pairs}
        for i, d in enumerate(gt):
            gw = d.get("width_mm_p95")
            if gw is None:
                continue
            b = _band(gw)
            band_gt[b] += 1
            if i in matched_gt:
                band_hit[b] += 1

        for gi, pj in pairs:
            gw = gt[gi].get("width_mm_p95")
            pw = result.cracks[pj].width_mm_p95
            if gw is None or pw is None:
                continue
            width_err.append(abs(gw - pw))
            width_bias.append(pw - gw)
            g_true = grade_by_crack_width(gw).value
            g_pred = grade_by_crack_width(pw).value
            confusion[(g_true, g_pred)] += 1
            grade_hit += int(g_true == g_pred)

    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    err = np.array(width_err) if width_err else np.zeros(1)
    bias = np.array(width_bias) if width_bias else np.zeros(1)

    return {
        "samples": len(records),
        "detection": {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "match_threshold": iou_thr,
            "fp_on_other_defect": fp_on_other_defect,
            "precision_excl_other": round(
                tp / max(tp + fp - fp_on_other_defect, 1), 4
            ),
        },
        "recall_by_width": {
            b: {
                "gt": band_gt[b],
                "hit": band_hit[b],
                "recall": round(band_hit[b] / band_gt[b], 3) if band_gt[b] else None,
            }
            for b in ["0-0.1mm", "0.1-0.2mm", "0.2-0.3mm", "0.3-1mm", ">=1.0mm"]
            if band_gt[b]
        },
        "width_mm": {
            "mae": round(float(err.mean()), 4),
            "median_ae": round(float(np.median(err)), 4),
            "p90_ae": round(float(np.percentile(err, 90)), 4),
            "bias": round(float(bias.mean()), 4),
            "n": len(width_err),
        },
        "grade": {
            "accuracy": round(grade_hit / max(len(width_err), 1), 4),
            "confusion": {f"{t}->{p}": n for (t, p), n in sorted(confusion.items())},
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="균열 검출기 벤치마크")
    p.add_argument("--data", type=str, default="data/smoke")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--iou", type=float, default=0.30,
                   help="중심선 버퍼 겹침(F) 매칭 임계")
    p.add_argument("--sensitivity", type=float, default=1.0)
    p.add_argument("--min-length", type=int, default=40)
    p.add_argument("--min-contrast", type=float, default=6.0)
    p.add_argument("--merge-gap", type=int, default=3)
    p.add_argument("--psf", type=float, default=2.2)
    p.add_argument("--waviness", type=float, default=0.012)
    p.add_argument("--min-conf", type=float, default=None,
                   help="비우면 검출기 기본값 (분류기 있으면 0.30)")
    a = p.parse_args(argv)

    report = evaluate(
        Path(a.data),
        a.limit or None,
        a.iou,
        sensitivity=a.sensitivity,
        min_length_px=a.min_length,
        min_contrast=a.min_contrast,
        merge_gap_px=a.merge_gap,
        psf_sigma_px=a.psf,
        min_waviness=a.waviness,
        min_confidence=a.min_conf,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
