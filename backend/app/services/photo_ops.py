"""사진 보정 도구 — 치수 측정 · 4점 원근 보정.

테스트 실행계획서 p.5 (18번 항목)가 규정한 두 가지 보정 수단이다.

    "'정보 부족' 사진은 [치수 측정]으로 길이 기준(측정자)을 잡아야 분석됩니다."
    "비뚤게 찍힌 사진은 [사진 보정](4점 원근) 후 [다시 사진 분석하기]."

왜 이 둘이 중요한가
-------------------
균열폭을 mm로 환산하려면 픽셀당 실제 길이(GSD)를 알아야 한다. 촬영거리를
기록하지 못했거나 EXIF가 없으면 GSD를 계산할 수 없고, 그 사진의 결함은
"폭 미상"이 아니라 **판정 불가**가 된다.

현장에서는 이 경우를 대비해 크랙스케일·줄자를 결함 옆에 대고 찍는다
(테스트계획서 ④ — "스케일 동반 컷은 필수, AI가 균열 폭을 추정·검증하는 기준").
치수 측정은 그 스케일 위의 두 점을 찍어 GSD를 역산하는 기능이다.

원근 보정은 다른 문제를 푼다. 벽면에 비스듬히 찍으면 같은 균열이라도 화면
위치에 따라 픽셀 폭이 달라져, 단일 GSD로는 전면을 옳게 환산할 수 없다.
네 점으로 평면을 펴면 전면에 균일한 스케일이 성립한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np


# ─── 치수 측정 ─────────────────────────────────────────────────
@dataclass
class ScaleResult:
    mm_per_px: float
    pixel_distance: float
    real_length_mm: float
    relative_precision: float   # 1px 오독이 만드는 상대오차
    note: str


# 측정선이 이보다 짧으면 1px 오독의 영향이 커져 신뢰할 수 없다.
MIN_MEASURE_PX = 40.0
# 상대정밀도가 이보다 나쁘면 경고한다 (예: 0.02 = 2%)
PRECISION_WARN = 0.02


def scale_from_two_points(
    p1: tuple[float, float],
    p2: tuple[float, float],
    real_length_mm: float,
) -> ScaleResult:
    """이미지 위 두 점과 그 사이의 실제 길이로 GSD를 역산한다.

    크랙스케일의 눈금 두 곳, 줄자의 두 눈금, 또는 규격을 아는 부재(벽돌 한 장,
    타일 한 변)의 양 끝을 찍으면 된다.

    **측정선을 길게 잡을수록 정확하다.** 픽셀 좌표는 1px 단위로만 찍히므로,
    50px 선에서 1px를 잘못 찍으면 2% 오차지만 500px 선에서는 0.2%다.
    그래서 상대정밀도를 함께 돌려주고, 짧으면 경고한다.
    """
    if real_length_mm <= 0:
        raise ValueError("실제 길이는 0보다 커야 합니다")

    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        raise ValueError("두 점이 같은 위치입니다. 서로 떨어진 두 점을 찍으십시오")

    mm_per_px = real_length_mm / dist
    precision = 1.0 / dist          # 1px 오독의 상대 기여

    if dist < MIN_MEASURE_PX:
        note = (
            f"측정선이 {dist:.0f}px로 짧습니다. 1px 오독이 "
            f"{precision * 100:.1f}% 오차를 만듭니다 — 더 긴 구간으로 다시 재십시오."
        )
    elif precision > PRECISION_WARN:
        note = (
            f"상대정밀도 {precision * 100:.1f}%. 균열폭 0.3mm 판정 경계에서 "
            f"±{0.3 * precision:.3f}mm 흔들립니다."
        )
    else:
        note = (
            f"측정선 {dist:.0f}px · 상대정밀도 {precision * 100:.2f}% — "
            "폭 판정에 쓰기 충분합니다."
        )

    return ScaleResult(
        mm_per_px=round(mm_per_px, 5),
        pixel_distance=round(dist, 1),
        real_length_mm=real_length_mm,
        relative_precision=round(precision, 5),
        note=note,
    )


# ─── 4점 원근 보정 ─────────────────────────────────────────────
@dataclass
class RectifyResult:
    image: np.ndarray
    width: int
    height: int
    mm_per_px: float | None
    tilt_deg: float
    note: str


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """네 점을 좌상-우상-우하-좌하 순으로 정렬한다.

    사용자가 어느 모서리부터 찍든 같은 결과가 나와야 한다. 순서를 강요하면
    현장에서 반드시 틀리게 찍는다.
    """
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [
            pts[np.argmin(s)],   # 좌상 — x+y 최소
            pts[np.argmin(d)],   # 우상 — x-y 최대 (diff = y-x 최소)
            pts[np.argmax(s)],   # 우하 — x+y 최대
            pts[np.argmax(d)],   # 좌하 — y-x 최대
        ],
        dtype=np.float32,
    )


def _tilt_from_quad(quad: np.ndarray) -> float:
    """사각형의 변 길이 불균형에서 기울기를 추정한다.

    정면이면 마주 보는 변의 길이가 같다. 비스듬할수록 한쪽이 짧아진다.
    그 비를 각도로 환산해 사용자에게 '얼마나 비뚤었는지' 알린다.
    """
    tl, tr, br, bl = quad
    top = float(np.linalg.norm(tr - tl))
    bottom = float(np.linalg.norm(br - bl))
    left = float(np.linalg.norm(bl - tl))
    right = float(np.linalg.norm(br - tr))

    ratios = []
    if max(top, bottom) > 1e-6:
        ratios.append(min(top, bottom) / max(top, bottom))
    if max(left, right) > 1e-6:
        ratios.append(min(left, right) / max(left, right))
    if not ratios:
        return 0.0
    r = min(ratios)
    return round(math.degrees(math.acos(max(min(r, 1.0), 0.0))), 1)


def rectify_perspective(
    image: np.ndarray,
    points: list[tuple[float, float]],
    *,
    real_width_mm: float | None = None,
    real_height_mm: float | None = None,
    max_side: int = 2400,
) -> RectifyResult:
    """네 점으로 지정한 평면을 정면으로 편다.

    실제 치수(가로·세로 mm)를 함께 주면 출력 영상의 GSD가 확정되므로,
    보정과 스케일 설정이 한 번에 끝난다. 치수를 모르면 형상만 편다.
    """
    if len(points) != 4:
        raise ValueError("정확히 4개의 점이 필요합니다")

    quad = _order_quad(np.array(points, dtype=np.float32))
    tilt = _tilt_from_quad(quad)

    tl, tr, br, bl = quad
    # 출력 크기는 원본에서 가장 긴 변을 기준으로 잡는다 — 축소로 인한
    # 해상도 손실이 미세 균열을 지우지 않도록.
    out_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    out_w = max(out_w, 16)
    out_h = max(out_h, 16)

    scale = min(1.0, max_side / max(out_w, out_h))
    out_w = int(out_w * scale)
    out_h = int(out_h * scale)

    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    M = cv2.getPerspectiveTransform(quad, dst)
    warped = cv2.warpPerspective(
        image, M, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )

    mm_per_px = None
    if real_width_mm and real_width_mm > 0:
        mm_per_px = real_width_mm / out_w
    elif real_height_mm and real_height_mm > 0:
        mm_per_px = real_height_mm / out_h

    if mm_per_px is not None:
        note = (
            f"기울기 {tilt:.1f}° 보정 · {out_w}×{out_h}px · "
            f"GSD {mm_per_px:.4f}mm/px 확정. 다시 분석하십시오."
        )
    elif tilt >= 8.0:
        note = (
            f"기울기 {tilt:.1f}°를 폈습니다. 실제 치수를 함께 입력하면 "
            "GSD까지 한 번에 확정됩니다."
        )
    else:
        note = (
            f"기울기 {tilt:.1f}° — 원래도 거의 정면입니다. "
            "보정 효과가 크지 않습니다."
        )

    return RectifyResult(
        image=warped,
        width=out_w,
        height=out_h,
        mm_per_px=round(mm_per_px, 5) if mm_per_px else None,
        tilt_deg=tilt,
        note=note,
    )


# ─── 분석 가능성 판정 ──────────────────────────────────────────
def analysis_readiness(
    gsd_mm_per_px: float | None, sharpness: float | None, min_sharpness: float = 45.0
) -> tuple[str, str]:
    """사진이 분석 가능한 상태인지 판정한다.

    반환: (analysis_state, 안내 문구)

    '정보 부족(needs_scale)'과 '실패(failed)'를 구분하는 것이 요점이다.
    전자는 사용자가 치수 측정으로 해결할 수 있고, 후자는 재촬영해야 한다.
    구분하지 않으면 사용자는 무엇을 해야 할지 모른다.
    """
    if not gsd_mm_per_px or gsd_mm_per_px <= 0:
        return (
            "needs_scale",
            "정보 부족 — 픽셀-실치수 환산 기준이 없습니다. "
            "[치수 측정]으로 스케일을 잡으면 분석할 수 있습니다.",
        )
    if sharpness is not None and sharpness < min_sharpness:
        return (
            "analyzed",
            f"선명도 부족 (Laplacian var {sharpness:.0f} < {min_sharpness:.0f}) — "
            "균열폭이 과대평가될 수 있어 재촬영을 권고합니다.",
        )
    return ("analyzed", "")
