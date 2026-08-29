"""대규모 합성 데이터셋 생성 CLI.

사용 예
-------
    python -m datagen.generate --count 20000 --out data/synth_v1 --workers 8
    python -m datagen.generate --count 500 --out data/smoke --size 512

산출 구조
---------
    out/
      images/0000000.jpg ...
      masks/0000000_crack.png, 0000000_defect.png ...
      labels/0000000.json ...
      index.jsonl          전체 표본 1줄 1레코드
      splits.json          train/val/test 분할 (id 기준, 8:1:1)
      stats.json           생성 통계 — 결함 유형/균열폭 분포
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datagen.synth import generate_sample, write_sample  # noqa: E402


def _one(args: tuple[int, int, str, int]) -> dict:
    index, seed, out_dir, size = args
    sample = generate_sample(seed, size=(size, size))
    return write_sample(sample, Path(out_dir), index)


def _summarize(records: list[dict]) -> dict:
    widths = [
        d["width_mm_p95"]
        for r in records
        for d in r["defects"]
        if d["defect_type"] == "crack" and d.get("width_mm_p95")
    ]
    counts: dict[str, int] = {}
    for r in records:
        for d in r["defects"]:
            counts[d["defect_type"]] = counts.get(d["defect_type"], 0) + 1

    w = np.array(widths, np.float32) if widths else np.zeros(1, np.float32)
    # 판정 경계별 표본 수 — 등급 불균형을 즉시 확인하기 위함
    bands = {
        "a (<0.1mm)": int((w < 0.1).sum()),
        "b (0.1-0.2)": int(((w >= 0.1) & (w < 0.2)).sum()),
        "c (0.2-0.3)": int(((w >= 0.2) & (w < 0.3)).sum()),
        "d (0.3-1.0)": int(((w >= 0.3) & (w < 1.0)).sum()),
        "e (>=1.0mm)": int((w >= 1.0).sum()),
    }
    return {
        "samples": len(records),
        "clean_samples": sum(1 for r in records if r["meta"]["clean"]),
        "defect_counts": counts,
        "crack_instances": len(widths),
        "crack_width_mm": {
            "min": round(float(w.min()), 3),
            "p50": round(float(np.percentile(w, 50)), 3),
            "p95": round(float(np.percentile(w, 95)), 3),
            "max": round(float(w.max()), 3),
        },
        "grade_bands": bands,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="합성 드론 균열 데이터셋 생성")
    p.add_argument("--count", type=int, default=1000, help="생성할 표본 수")
    p.add_argument("--out", type=str, default="data/synth", help="출력 디렉터리")
    p.add_argument("--size", type=int, default=768, help="이미지 한 변 픽셀")
    p.add_argument("--seed", type=int, default=20260830, help="기준 시드")
    p.add_argument("--workers", type=int, default=0, help="0이면 단일 프로세스")
    args = p.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [
        (i, args.seed + i, str(out), args.size) for i in range(args.count)
    ]

    t0 = time.time()
    records: list[dict] = []
    if args.workers and args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(_one, j) for j in jobs]
            for n, f in enumerate(as_completed(futures), 1):
                records.append(f.result())
                if n % 200 == 0 or n == args.count:
                    rate = n / max(time.time() - t0, 1e-6)
                    print(f"  {n}/{args.count}  ({rate:.1f}/s)", flush=True)
    else:
        for n, j in enumerate(jobs, 1):
            records.append(_one(j))
            if n % 200 == 0 or n == args.count:
                rate = n / max(time.time() - t0, 1e-6)
                print(f"  {n}/{args.count}  ({rate:.1f}/s)", flush=True)

    records.sort(key=lambda r: r["id"])
    with (out / "index.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    ids = [r["id"] for r in records]
    n_train, n_val = int(len(ids) * 0.8), int(len(ids) * 0.1)
    (out / "splits.json").write_text(
        json.dumps(
            {
                "train": ids[:n_train],
                "val": ids[n_train:n_train + n_val],
                "test": ids[n_train + n_val:],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    stats = _summarize(records)
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    (out / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
