"""사진 보정 — 치수 측정(스케일 확정)과 4점 원근 보정.

왜 필요한가
-----------
검출기는 균열을 픽셀로 잰다. 이걸 mm로 바꾸려면 **픽셀당 실제 길이(스케일)** 가
있어야 한다. 촬영거리를 모르면 스케일이 없고, 스케일이 없으면 등급 판정 자체가
성립하지 않는다 — 폭 0.3mm 초과 여부를 말할 수 없기 때문이다.

현장에서 스케일을 확보하는 방법은 둘이다.

1. **치수 측정** — 사진 안에 길이를 아는 물체(크랙스케일·줄자·표준 벽돌)를 찍고,
   그 두 끝점을 찍어 실제 길이를 알려준다. 테스트 실행계획서가 "스케일 동반 컷"을
   필수로 규정하는 이유가 이것이다.
2. **4점 원근 보정** — 벽면을 비스듬히 찍으면 같은 균열도 위치마다 폭이 달라진다.
   직사각형인 것을 아는 영역(창틀·타일·패널)의 네 모서리를 찍어 정면 시점으로
   펴면 왜곡이 사라지고, 그 직사각형의 실제 치수를 알면 스케일도 함께 확정된다.

정확도에 관한 정직한 한계
-------------------------
치수 측정은 **기준물이 균열과 같은 평면에 있을 때만** 맞는다. 기준자를 카메라
쪽으로 당겨 들고 찍으면 실제보다 크게 나와 균열폭이 과소평가된다.
원근 보정은 **평면 가정**에 기댄다. 굴곡진 면이나 돌출부가 있으면 보정 후에도
국부 오차가 남는다. 둘 다 화면에서 사용자에게 경고한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class ScaleResult:
    mm_per_px: float
    pixel_distance: float
    real_mm: float
    note: str


def scale_from_reference(
    p1: tuple[float, float], p2: tuple[float, float], real_mm: float
) -> ScaleResult:
    """사진 위 두 점과 그 사이의 실제 길이로 픽셀 스케일을 확정한다."""
    if real_mm <= 0:
        raise ValueError("실제 길이는 0보다 커야 합니다")
    dist = math.dist(p1, p2)
    if dist < 5.0:
        raise ValueError(
            "두 점이 너무 가깝습니다. 5px 이상 떨어뜨려 찍으십시오 — "
            "짧은 기준은 클릭 오차가 그대로 스케일 오차가 됩니다."
        )

    mm_per_px = real_mm / dist
    # 클릭 오차를 ±1px로 보면 상대오차는 1/dist 다. 이걸 사용자에게 그대로 알린다.
    rel_err = 1.0 / dist
    note = (
        f"기준 길이 {real_mm:.1f}mm 를 {dist:.0f}px 로 측정했습니다. "
        f"클릭 오차 ±1px 기준 스케일 불확실도 약 ±{rel_err * 100:.1f}%. "
        "기준물이 균열과 같은 평면에 있어야 정확합니다."
    )
    return ScaleResult(
        mm_per_px=round(mm_per_px, 6),
        pixel_distance=round(dist, 1),
        real_mm=real_mm,
        note=note,
    )


@dataclass
class RectifyResult:
    image: np.ndarray
    mm_per_px: float | None
    out_size: tuple[int, int]        # (w, h)
    note: str


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """네 점을 좌상·우상·우하·좌하 순서로 정렬한다.

    사용자가 어느 모서리부터 찍든 같은 결과가 나와야 한다. 합(x+y)이 최소인
    점이 좌상, 최대가 우하; 차(y-x)가 최소가 우상, 최대가 좌하다.
    """
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def rectify_quad(
    image: np.ndarray,
    quad: list[tuple[float, float]],
    *,
    real_width_mm: float | None = None,
    real_height_mm: float | None = None,
    max_side: int = 2400,
) -> RectifyResult:
    """네 모서리를 정면 시점으로 편다.

    출력 크기는 원본 네 변의 길이에서 가로·세로 대표값을 잡아 정한다. 임의로
    정하면 보정 과정에서 해상도가 떨어지거나 불필요하게 커진다.
    실제 치수를 알려주면 스케일(mm/px)도 함께 확정한다.
    """
    if len(quad) != 4:
        raise ValueError("모서리는 정확히 4개여야 합니다")

    src = _order_quad(np.array(quad, dtype=np.float32))
    (tl, tr, br, bl) = src

    width = max(math.dist(tl, tr), math.dist(bl, br))
    height = max(math.dist(tl, bl), math.dist(tr, br))
    if min(width, height) < 20:
        raise ValueError("선택한 영역이 너무 작습니다")

    # 과도한 확대를 막는다 — 원본에 없는 정보가 생기지는 않는다
    scale = min(1.0, max_side / max(width, height))
    out_w, out_h = int(round(width * scale)), int(round(height * scale))

    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(
        image, M, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )

    mm_per_px = None
    parts = [f"원근 보정 완료 ({out_w}×{out_h}px)."]
    if real_width_mm and real_height_mm:
        # 두 방향이 다르게 나오면 평면 가정이 깨졌다는 뜻이므로 그대로 알린다
        sx = real_width_mm / out_w
        sy = real_height_mm / out_h
        mm_per_px = round((sx + sy) / 2.0, 6)
        skew = abs(sx - sy) / max(sx, sy)
        parts.append(f"스케일 {mm_per_px:.4f} mm/px 확정.")
        if skew > 0.08:
            parts.append(
                f"가로·세로 스케일이 {skew * 100:.0f}% 어긋납니다 — "
                "네 점이 실제 직사각형이 아니거나 면이 평면이 아닐 수 있습니다."
            )
    elif real_width_mm:
        mm_per_px = round(real_width_mm / out_w, 6)
        parts.append(f"가로 기준 스케일 {mm_per_px:.4f} mm/px.")
    elif real_height_mm:
        mm_per_px = round(real_height_mm / out_h, 6)
        parts.append(f"세로 기준 스케일 {mm_per_px:.4f} mm/px.")
    else:
        parts.append("실제 치수를 입력하지 않아 스케일은 확정되지 않았습니다.")

    parts.append("평면 가정이 전제입니다 — 굴곡면에서는 국부 오차가 남습니다.")
    return RectifyResult(
        image=warped, mm_per_px=mm_per_px, out_size=(out_w, out_h), note=" ".join(parts)
    )


def map_points(
    quad: list[tuple[float, float]], out_size: tuple[int, int], pts: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """원본 좌표를 보정 후 좌표로 옮긴다.

    보정 전에 찍어 둔 손상 위치를 보정본에서도 유지하기 위해 필요하다.
    """
    if not pts:
        return []
    src = _order_quad(np.array(quad, dtype=np.float32))
    out_w, out_h = out_size
    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    M = cv2.getPerspectiveTransform(src, dst)
    arr = np.array(pts, dtype=np.float32).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(arr, M).reshape(-1, 2)
    return [(float(x), float(y)) for x, y in out]
