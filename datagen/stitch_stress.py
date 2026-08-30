"""정합 스트레스 시험 — 실제 드론 촬영이 갖는 조건을 주입한다.

왜 필요한가
------------
지금까지의 정합 검증은 큰 합성 이미지를 격자로 잘라낸 타일로 했다. 그 타일들은
**같은 평면을 같은 각도·같은 노출로 본 조각**이라 호모그래피가 항등변환에 가깝다.
배치 잔차 0.0px 은 정합기가 정확해서가 아니라 문제가 쉬웠기 때문일 수 있다.

실제 드론 촬영은 다르다. 이 모듈은 그 차이를 하나씩 주입해서 **어디서 깨지는지**를
찾는다. 실촬영본을 구하기 전까지, 이것이 낼 수 있는 가장 정직한 답이다.

주입하는 조건
-------------
| 조건 | 실제 원인 |
|---|---|
| 시점(yaw/pitch) 변화 | 드론이 벽면과 완전 정면일 수 없다. 짐벌이 흔들린다 |
| 자동 노출 변동 | 프레임마다 밝기·감마가 달라진다 |
| 모션 블러 | 비행 중 셔터. 방향은 진행 방향을 따른다 |
| 렌즈 배럴 왜곡 | 광각 렌즈. 정합기는 이를 모델링하지 않는다 |
| 겹침률 | 비행 간격에 따라 20~50% 로 흔들린다 |
| 센서 노이즈 | 고 ISO 저조도 |

각 조건을 **단독으로** 키워가며 정합이 무너지는 지점을 찾고, 마지막에 전부 합친
'현실 조합'을 돌린다. 단독으로 봐야 무엇이 원인인지 알 수 있다.

측정하는 것
-----------
- 배치 성공률(placed/total)과 커버리지
- **배치 잔차** — 타일을 잘라낸 원래 위치를 알고 있으므로 정답이 있다.
  정합기가 추정한 변환으로 타일 네 귀퉁이를 파노라마로 보내고, 정답 위치와의
  거리를 잰다. 이것이 정합 정확도의 유일한 직접 지표다.
- 배율 편차, 매칭 신뢰도, 소요

실행:  python -m datagen.stitch_stress
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.stitching import Stitcher, map_point  # noqa: E402

from .synth import generate_sample  # noqa: E402


# ─── 촬영 조건 ─────────────────────────────────────────────────
@dataclass
class Condition:
    """한 번의 촬영이 갖는 조건. 0 이면 이상적(=격자 잘라내기)."""

    name: str
    overlap: float = 0.40          # 이웃 타일과의 겹침 비율
    view_deg: float = 0.0          # 시점 기울기 최대치 (yaw/pitch 합성)
    exposure: float = 0.0          # 노출 변동 폭 (0.2 = ±20%)
    blur_px: float = 0.0           # 모션 블러 길이
    barrel: float = 0.0            # 배럴 왜곡 계수 k1
    noise: float = 0.0             # 가우시안 노이즈 표준편차


@dataclass
class StressResult:
    cond: str
    total: int
    placed: int
    coverage: float
    residual_mean: float | None      # 전역 호모그래피 정렬 후 — 정합기의 실제 오차
    residual_max: float | None
    residual_sim: float | None       # 상사변환 정렬 후 — 기준계 기울기 포함
    scale_drift: float
    pairs: int
    confidence: float
    elapsed: float
    warnings: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.placed < self.total:
            return "실패"
        if self.residual_mean is None:
            return "미측정"
        if self.residual_mean <= 2.0:
            return "양호"
        if self.residual_mean <= 8.0:
            return "허용"
        return "부정확"


# ─── 왜곡 주입 ─────────────────────────────────────────────────
def _view_homography(w: int, h: int, deg: float, rng: np.random.Generator) -> np.ndarray:
    """시점 기울기를 호모그래피로 만든다.

    드론이 벽면을 비스듬히 보면 사각형이 사다리꼴이 된다. 네 귀퉁이를 각도에
    비례해 안팎으로 밀어 그 효과를 낸다. 회전각을 직접 쓰지 않고 귀퉁이 변위로
    표현하는 편이 크기 감각이 분명하다 — deg=10 이면 변의 약 8%가 밀린다.
    """
    if deg <= 0:
        return np.eye(3, dtype=np.float64)
    amp = np.tan(np.radians(deg)) * 0.5
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    off = rng.uniform(-amp, amp, size=(4, 2)) * np.float32([w, h])
    return cv2.getPerspectiveTransform(src, (src + off).astype(np.float32)).astype(np.float64)


def _barrel(img: np.ndarray, k1: float) -> np.ndarray:
    """배럴 왜곡. 광각 렌즈의 직선이 바깥으로 휘는 현상."""
    if abs(k1) < 1e-9:
        return img
    h, w = img.shape[:2]
    cam = np.array([[w, 0, w / 2], [0, w, h / 2], [0, 0, 1]], np.float64)
    dist = np.array([k1, 0, 0, 0], np.float64)
    return cv2.undistort(img, cam, dist)


def _motion_blur(img: np.ndarray, length: float, angle_deg: float) -> np.ndarray:
    if length < 1:
        return img
    n = int(round(length)) | 1
    k = np.zeros((n, n), np.float32)
    k[n // 2, :] = 1.0
    m = cv2.getRotationMatrix2D((n / 2 - 0.5, n / 2 - 0.5), angle_deg, 1.0)
    k = cv2.warpAffine(k, m, (n, n))
    s = k.sum()
    return cv2.filter2D(img, -1, k / s) if s > 0 else img


def make_tiles(
    seed: int, cond: Condition, tile: int = 600, size: tuple[int, int] = (1100, 1700)
) -> tuple[list[np.ndarray], list[np.ndarray], float]:
    """조건을 주입한 타일들과 **정답 변환**을 함께 낸다.

    정답 변환 G_i 는 '타일 좌표 → 원본 큰 이미지 좌표'다. 격자 잘라내기에서는
    단순 평행이동이지만, 시점 왜곡을 넣으면 그 호모그래피의 역이 곱해진다.
    이걸 알고 있어야 정합 결과의 잔차를 잴 수 있다.
    """
    s = generate_sample(seed, size=size)
    big = s.image
    H, W = big.shape[:2]
    step = max(40, int(tile * (1.0 - cond.overlap)))
    rng = np.random.default_rng(seed ^ 0xA5A5)

    tiles: list[np.ndarray] = []
    truth: list[np.ndarray] = []
    for y in range(0, H - tile + 1, step):
        for x in range(0, W - tile + 1, step):
            patch = big[y : y + tile, x : x + tile].copy()

            # 시점 — 타일을 사다리꼴로 편다. 정합기는 이 역을 찾아내야 한다.
            Hv = _view_homography(tile, tile, cond.view_deg, rng)
            if cond.view_deg > 0:
                patch = cv2.warpPerspective(
                    patch, Hv, (tile, tile), flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT,
                )

            if cond.barrel:
                patch = _barrel(patch, cond.barrel)
            if cond.blur_px:
                patch = _motion_blur(patch, cond.blur_px, rng.uniform(0, 180))
            if cond.exposure:
                gain = 1.0 + rng.uniform(-cond.exposure, cond.exposure)
                gamma = 1.0 + rng.uniform(-cond.exposure * 0.6, cond.exposure * 0.6)
                p = np.clip(patch.astype(np.float32) * gain, 0, 255) / 255.0
                patch = np.clip(np.power(p, gamma) * 255.0, 0, 255).astype(np.uint8)
            if cond.noise:
                patch = np.clip(
                    patch.astype(np.float32) + rng.normal(0, cond.noise, patch.shape),
                    0, 255,
                ).astype(np.uint8)

            tiles.append(patch)
            # 타일 좌표 → 원본 좌표 : 평행이동 ∘ 시점왜곡의 역
            T = np.array([[1, 0, x], [0, 1, y], [0, 0, 1]], np.float64)
            truth.append(T @ np.linalg.inv(Hv))
    return tiles, truth, s.mm_per_px


# ─── 잔차 측정 ─────────────────────────────────────────────────
def placement_residual(
    result, tiles: list[np.ndarray], truth: list[np.ndarray]
) -> tuple[float, float, float] | None:
    """추정 배치와 정답 배치의 거리.

    정합기는 첫 장의 좌표계를 기준으로 삼는다. 그런데 첫 장 자체가 시점
    왜곡으로 기울어져 있으면, 정합기의 기준계와 원본 평면은 **호모그래피**만큼
    다르다. 그래서 상사변환으로 맞추면 정합기가 정확해도 잔차가 크게 나온다 —
    좌표계 선택의 차이를 정합기의 오차로 잘못 읽는 것이다.

    그 전역 호모그래피 한 장을 먼저 맞춘 뒤 남은 오차를 잰다. 이것이 정합기가
    실제로 틀린 양이다. 비교를 위해 상사변환 기준 잔차도 함께 낸다 —
    둘의 차이가 곧 '기준계가 얼마나 기울었는가'다.
    """
    if len(result.placed) < 2:
        return None
    est: list[tuple[float, float]] = []
    gt: list[tuple[float, float]] = []
    for i in result.placed:
        h, w = tiles[i].shape[:2]
        for cx, cy in ((0, 0), (w, 0), (w, h), (0, h), (w / 2, h / 2)):
            est.append(map_point(result.transforms[i], cx, cy))
            p = truth[i] @ np.array([cx, cy, 1.0])
            gt.append((p[0] / p[2], p[1] / p[2]))
    E = np.asarray(est, np.float64)
    G = np.asarray(gt, np.float64)

    Ef = E.reshape(-1, 1, 2).astype(np.float32)
    Gf = G.reshape(-1, 1, 2).astype(np.float32)

    H, _ = cv2.findHomography(Ef, Gf, method=0)  # 전역 기준계 정렬
    if H is None:
        return None
    P = cv2.perspectiveTransform(Ef, H).reshape(-1, 2)
    d = np.linalg.norm(P - G, axis=1)

    # 상사변환 기준 — 기준계 기울기를 보정하지 않은 값
    M, _ = cv2.estimateAffinePartial2D(Ef, Gf, method=cv2.LMEDS)
    if M is None:
        sim = float("nan")
    else:
        Q = (M[:, :2] @ E.T).T + M[:, 2]
        sim = float(np.linalg.norm(Q - G, axis=1).mean())
    return float(d.mean()), float(d.max()), sim


def run(cond: Condition, seed: int = 4242) -> StressResult:
    tiles, truth, gsd = make_tiles(seed, cond)
    t0 = time.time()
    r = Stitcher().stitch(tiles, mm_per_px=[gsd] * len(tiles), ordered=True)
    el = time.time() - t0

    res = placement_residual(r, tiles, truth) if r.panorama is not None else None
    conf = (
        float(np.mean([p.confidence for p in r.pairs])) if r.pairs else 0.0
    )
    return StressResult(
        cond=cond.name,
        total=len(tiles),
        placed=len(r.placed),
        coverage=r.coverage,
        residual_mean=round(res[0], 2) if res else None,
        residual_max=round(res[1], 2) if res else None,
        residual_sim=round(res[2], 2) if res else None,
        scale_drift=r.scale_drift,
        pairs=len(r.pairs),
        confidence=round(conf, 3),
        elapsed=round(el, 1),
        warnings=r.warnings,
    )


# ─── 시험 항목 ─────────────────────────────────────────────────
def suite() -> list[Condition]:
    """단독 조건을 세기별로 올린 뒤, 마지막에 현실 조합."""
    out = [Condition("이상적(격자 잘라내기)")]
    for d in (5, 10, 15, 20):
        out.append(Condition(f"시점 {d}°", view_deg=d))
    for e in (0.15, 0.30):
        out.append(Condition(f"노출 ±{int(e*100)}%", exposure=e))
    for b in (3.0, 6.0):
        out.append(Condition(f"모션블러 {b:.0f}px", blur_px=b))
    for k in (-0.15, -0.30):
        out.append(Condition(f"배럴왜곡 k1={k}", barrel=k))
    for ov in (0.30, 0.20, 0.15):
        out.append(Condition(f"겹침 {int(ov*100)}%", overlap=ov))
    out.append(Condition("노이즈 σ=8", noise=8.0))
    out.append(
        Condition("현실 조합(약)", overlap=0.35, view_deg=8, exposure=0.15,
                  blur_px=2.0, barrel=-0.10, noise=4.0)
    )
    out.append(
        Condition("현실 조합(강)", overlap=0.30, view_deg=15, exposure=0.30,
                  blur_px=5.0, barrel=-0.25, noise=8.0)
    )
    return out


def main() -> None:
    print(f"{'조건':<22}{'배치':>8}{'커버':>7}{'잔차평균':>9}{'잔차최대':>9}"
          f"{'상사기준':>9}{'배율':>7}{'쌍':>5}{'신뢰':>7}{'초':>6}  판정")
    print("─" * 105)
    rows: list[StressResult] = []
    for c in suite():
        r = run(c)
        rows.append(r)
        rm = f"{r.residual_mean:.2f}" if r.residual_mean is not None else "—"
        rx = f"{r.residual_max:.2f}" if r.residual_max is not None else "—"
        rs = f"{r.residual_sim:.1f}" if r.residual_sim is not None else "—"
        print(f"{r.cond:<22}{r.placed:>4}/{r.total:<3}{r.coverage*100:>6.0f}%"
              f"{rm:>9}{rx:>9}{rs:>9}{r.scale_drift:>7.3f}{r.pairs:>5}"
              f"{r.confidence:>7.3f}{r.elapsed:>6.1f}  {r.verdict}")
        if r.warnings:
            print(f"{'':<22}⚠ {r.warnings[0][:70]}")

    print("─" * 105)
    ok = [r for r in rows if r.verdict in ("양호", "허용")]
    print(f"통과 {len(ok)}/{len(rows)}")


if __name__ == "__main__":
    main()
