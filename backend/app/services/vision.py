"""균열 검출 및 폭 정량화 엔진.

파이프라인
----------
1. 그레이스케일 변환 + CLAHE 국소 대비 보정
2. 다중 스케일 헤시안 능선 필터(Frangi 계열)로 선형 구조만 강조
   콘크리트 표면의 얼룩/골재는 선형이 아니므로 여기서 대부분 탈락한다.
3. 히스테리시스 — 암부 영역 안에서 Otsu로 잡은 강한 능선 응답을 씨앗으로,
   완화된 능선 조건까지 성장시킨다. 임계를 전역 분위수로 두면 뚜렷한 균열
   하나가 예산을 독식해 나머지를 놓치므로 반드시 국소 적응 임계를 쓴다.
4. 연결요소 분석 — 길이/세장비/충실도/국소대비/사행도로 오검출 제거,
   인접 파편은 하나의 균열 인스턴스로 병합한다.
5. 중심선 법선 방향 밝기 프로파일의 FWHM으로 폭 산출 후,
   촬영계 PSF 기여분을 이차합으로 제거(deconvolve_width)한다.
6. GSD(mm/px)를 곱해 물리 단위(mm)로 환산

주의
----
본 엔진은 고전 영상처리 기반 베이스라인이다. 학습 모델(Mask R-CNN / Y-MaskNet)
교체를 전제로 `CrackDetector` 인터페이스를 고정해 두었으므로, 동일한
`detect()` 시그니처를 갖는 구현으로 치환하면 상위 계층은 수정이 필요 없다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# ─── 오검출 제거 임계값 ────────────────────────────────────────
MIN_AREA_PX = 60          # 이보다 작은 blob은 노이즈로 간주
MIN_LENGTH_PX = 40        # 균열로 인정할 최소 골격 길이
MIN_ELONGATION = 2.5      # 장축/단축 비 — 균열은 가늘고 길다
MAX_FILL_RATIO = 0.62     # 외접사각형 대비 채움비가 높으면 얼룩/그림자


@dataclass
class CrackInstance:
    """검출된 균열 1건."""

    bbox: tuple[int, int, int, int]      # x, y, w, h (px)
    length_px: float
    width_px_max: float
    width_px_p95: float
    width_px_mean: float
    area_px: int
    elongation: float
    confidence: float
    polyline: list[tuple[int, int]] = field(default_factory=list)

    # 물리 단위 — GSD 적용 후 채워진다
    length_mm: float | None = None
    width_mm_max: float | None = None
    width_mm_p95: float | None = None

    def apply_scale(self, mm_per_px: float | None) -> None:
        if not mm_per_px or mm_per_px <= 0:
            return
        self.length_mm = round(self.length_px * mm_per_px, 2)
        self.width_mm_max = round(self.width_px_max * mm_per_px, 3)
        self.width_mm_p95 = round(self.width_px_p95 * mm_per_px, 3)

    @property
    def representative_width_mm(self) -> float | None:
        """대표 균열폭 — 이상치에 둔감하도록 p95를 채택한다."""
        return self.width_mm_p95


@dataclass
class DetectionResult:
    cracks: list[CrackInstance]
    mask: np.ndarray
    image_size: tuple[int, int]          # (h, w)
    crack_area_ratio: float
    mm_per_px: float | None
    sharpness: float = 0.0
    quality_ok: bool = True
    quality_note: str = ""


def _equalize(gray: np.ndarray) -> np.ndarray:
    """국소 대비 강화 — 그늘진 영역의 균열도 살린다."""
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _ridge_response(
    gray: np.ndarray, scales: tuple[float, ...]
) -> tuple[np.ndarray, np.ndarray]:
    """다중 스케일 헤시안 능선 필터.

    각 스케일 s에서 가우시안 2차 미분으로 헤시안을 구성하고, 고유값 중
    큰 쪽(lambda_hi)을 취한다. 어두운 선(균열) 위에서는 lambda_hi > 0 이다.
    스케일 간 비교가 가능하도록 s^2 로 감마 정규화한 뒤 최댓값을 취한다.

    반환: (정규화된 능선 응답, 균열 법선 방향 각도[rad])
    법선 각도는 최대 응답을 낸 스케일의 주축 각도이며, 폭 측정 시
    이 방향으로 밝기 프로파일을 잘라 FWHM을 구한다.
    """
    src = gray.astype(np.float32) / 255.0
    best = np.zeros_like(src)
    normal = np.zeros_like(src)

    for s in scales:
        k = int(2 * round(3 * s) + 1)
        sm = cv2.GaussianBlur(src, (k, k), s)
        gxx = cv2.Sobel(sm, cv2.CV_32F, 2, 0, ksize=3)
        gyy = cv2.Sobel(sm, cv2.CV_32F, 0, 2, ksize=3)
        gxy = cv2.Sobel(sm, cv2.CV_32F, 1, 1, ksize=3)

        tmp = np.sqrt(np.maximum((gxx - gyy) ** 2 + 4.0 * gxy**2, 0.0))
        lam_hi = 0.5 * ((gxx + gyy) + tmp)
        resp = np.maximum(lam_hi, 0.0) * (s**2)

        win = resp > best
        best = np.where(win, resp, best)
        # 큰 고유값의 고유벡터 각도 = 곡률이 최대인 방향 = 균열을 가로지르는 방향
        normal = np.where(win, 0.5 * np.arctan2(2.0 * gxy, gxx - gyy), normal)

    if best.max() > 1e-9:
        best /= best.max()
    return best, normal


def _fwhm_widths(
    dark: np.ndarray,
    ridge_pts: np.ndarray,
    normal: np.ndarray,
    max_half_px: float = 40.0,
) -> np.ndarray:
    """능선점마다 법선 방향 밝기 프로파일의 반치전폭(FWHM)으로 폭을 잰다.

    이진화 임계값에 좌우되는 거리변환 방식과 달리, 균열 단면의 명암
    프로파일 자체에서 폭을 읽으므로 임계값 편향이 없다. 이것이 균열폭을
    ±0.1mm 수준으로 관리해야 하는 진단 실무에서 요구되는 정의다.
    """
    if len(ridge_pts) == 0:
        return np.zeros(0, np.float32)

    h, w = dark.shape
    ys = ridge_pts[:, 0].astype(np.float32)
    xs = ridge_pts[:, 1].astype(np.float32)
    theta = normal[ridge_pts[:, 0], ridge_pts[:, 1]]
    dy, dx = np.sin(theta), np.cos(theta)

    peak = dark[ridge_pts[:, 0], ridge_pts[:, 1]].astype(np.float32)
    half = 0.5 * peak

    step = 0.5
    n_steps = int(max_half_px / step)
    half_widths = np.zeros((len(ridge_pts), 2), np.float32)

    for sign_idx, sign in enumerate((1.0, -1.0)):
        done = np.zeros(len(ridge_pts), bool)
        edge = np.full(len(ridge_pts), max_half_px, np.float32)
        for n in range(1, n_steps + 1):
            t = n * step
            sy = np.clip(ys + sign * t * dy, 0, h - 1)
            sx = np.clip(xs + sign * t * dx, 0, w - 1)
            # 최근접 표본으로 충분하다 — 0.5px 간격이 이미 과표본이다
            val = dark[np.rint(sy).astype(np.int32), np.rint(sx).astype(np.int32)]
            crossed = (~done) & (val < half)
            edge[crossed] = t
            done |= crossed
            if done.all():
                break
        half_widths[:, sign_idx] = edge

    widths = half_widths.sum(axis=1)
    # 프로파일이 끝까지 반값 아래로 내려가지 않은 점은 신뢰할 수 없다
    valid = (half_widths < max_half_px).all(axis=1) & (peak > 3.0)
    return widths[valid].astype(np.float32)


def sharpness_score(gray: np.ndarray) -> float:
    """라플라시안 분산 기반 선명도 — 값이 낮으면 흐린 사진이다.

    흐린 영상에서는 균열 단면이 번져 폭이 실제보다 넓게 측정된다.
    실무에서 흐린 사진은 재촬영 대상이므로 여기서 게이트를 건다.
    """
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def deconvolve_width(fwhm_px: np.ndarray, psf_sigma_px: float) -> np.ndarray:
    """측정된 FWHM에서 촬영계 PSF 기여분을 이차합으로 제거한다.

    측정폭^2 = 실제폭^2 + (2.355 x sigma_psf)^2  (가우시안 근사)
    렌즈 흐림·모션블러·JPEG 평활화가 균열을 실제보다 넓게 보이게 하므로
    보정하지 않으면 폭이 계통적으로 과대평가된다.
    """
    psf_fwhm = 2.3548 * max(psf_sigma_px, 0.0)
    corrected = np.sqrt(np.maximum(fwhm_px**2 - psf_fwhm**2, 0.25))
    return corrected.astype(np.float32)


def _dark_field(gray: np.ndarray) -> np.ndarray:
    """국소 배경 대비 어두운 정도 (0~255). 균열의 실제 두께를 담는다."""
    ksize = max(15, (min(gray.shape) // 40) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
    return cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)


def _hysteresis(seed: np.ndarray, grow: np.ndarray) -> np.ndarray:
    """seed 를 포함하는 grow 연결요소만 남긴다 (형태학적 재구성)."""
    n, labels = cv2.connectedComponents(grow, 8)
    if n <= 1:
        return np.zeros_like(grow)
    keep = np.zeros(n, np.uint8)
    keep[np.unique(labels[seed > 0])] = 1
    keep[0] = 0
    return (keep[labels] * 255).astype(np.uint8)


def _segment(
    gray: np.ndarray, sensitivity: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """균열 중심선 마스크 · 암부 강도맵 · 법선 각도맵을 반환한다.

    마스크는 능선 응답으로 제약해 얇게 유지한다. 폭은 마스크가 아니라
    FWHM 프로파일에서 재므로, 마스크를 두껍게 키울 이유가 없다. 오히려
    얇게 두어야 인접한 별개의 균열이 하나로 뭉치지 않는다.
    """
    eq = _equalize(gray)
    ridge, normal = _ridge_response(eq, scales=(1.0, 2.0, 3.5, 5.0))
    dark = _dark_field(eq)

    # 씨앗: 능선 응답 상위 + 충분한 암부. 둘 다 만족해야 한다.
    #
    # 임계를 전역 분위수로 잡으면 "상위 N%"라는 고정 예산이 생겨, 아주 뚜렷한
    # 균열 하나가 예산을 독식하고 나머지 균열이 탈락한다. 암부 영역 안에서만
    # Otsu로 임계를 잡으면 균열 개수와 무관하게 동작한다.
    dark_hi = max(10.0, float(np.quantile(dark, 0.995)) * 0.45)
    dark_region = dark > dark_hi
    if dark_region.sum() >= 64:
        vals = (ridge[dark_region] * 255).astype(np.uint8)
        otsu, _ = cv2.threshold(vals, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        ridge_hi = float(otsu) / 255.0 / max(sensitivity, 1e-3)
    else:
        ridge_hi = float(np.quantile(ridge, 0.996))
    seed = ((ridge > ridge_hi) & dark_region).astype(np.uint8) * 255

    # 성장: 능선 조건을 완화하되 여전히 선형 구조로 제한한다.
    # 암부만으로 성장시키면 표면 얼룩을 타고 전체가 한 덩어리로 번진다.
    ridge_lo = float(np.quantile(ridge, np.clip(1.0 - 0.03 * sensitivity, 0.5, 0.9999)))
    dark_lo = max(5.0, dark_hi * 0.40)
    grow = ((ridge > ridge_lo) & (dark > dark_lo)).astype(np.uint8) * 255
    grow = cv2.morphologyEx(
        grow, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    )

    mask = _hysteresis(seed, grow)
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    )
    return mask, dark, normal


def _group_components(mask: np.ndarray, gap_px: int) -> tuple[int, np.ndarray]:
    """끊어진 파편을 하나의 균열 인스턴스로 묶는다.

    마스크를 gap_px 만큼 팽창시켜 라벨링하면, 근접한 파편이 같은 라벨을 갖는다.
    그 라벨을 원본 마스크에 되돌려 인스턴스 단위를 만든다.
    """
    if gap_px <= 0:
        return cv2.connectedComponents(mask, 8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap_px * 2 + 1,) * 2)
    n, labels = cv2.connectedComponents(cv2.dilate(mask, k), 8)
    return n, np.where(mask > 0, labels, 0)


def _ridge_points(dist: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """거리변환의 국소 최대 지점 = 균열 중심선(medial axis) 근사."""
    dilated = cv2.dilate(dist, np.ones((3, 3), np.uint8))
    ridge = (dist >= dilated - 1e-6) & (mask > 0) & (dist > 0.5)
    return ridge


def _skeleton_length(ridge: np.ndarray) -> float:
    """능선 픽셀 수를 길이로 환산. 대각 연결을 고려해 보정계수를 둔다."""
    n = int(np.count_nonzero(ridge))
    return n * 1.08 if n else 0.0


def _waviness(ridge_pts: np.ndarray) -> float:
    """중심선의 사행 정도 — 주축 대비 횡방향 잔차의 표준편차 / 길이.

    균열은 골재를 피해 사행하므로 값이 크다. 거푸집 이음선·시공줄눈·전선관
    자국은 직선이라 0에 가깝다. 이 한 가지 형상 특징이 콘크리트 벽면에서
    가장 흔한 오검출원을 걸러낸다.
    """
    if len(ridge_pts) < 8:
        return 1.0
    pts = ridge_pts.astype(np.float32)
    centered = pts - pts.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    along = centered @ vt[0]
    across = centered @ vt[1]
    span = float(along.max() - along.min())
    if span < 1e-6:
        return 1.0
    return float(across.std() / span)


def _polyline_from_component(
    ridge_pts: np.ndarray, max_points: int = 40
) -> list[tuple[int, int]]:
    """능선 좌표를 주축 기준으로 정렬해 표시용 폴리라인으로 축약한다."""
    if len(ridge_pts) < 2:
        return [(int(p[1]), int(p[0])) for p in ridge_pts]

    pts = ridge_pts.astype(np.float32)
    centered = pts - pts.mean(axis=0)
    # 주성분(장축) 방향으로 투영해 순서를 만든다.
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    order = np.argsort(centered @ vt[0])
    ordered = ridge_pts[order]

    step = max(1, len(ordered) // max_points)
    return [(int(p[1]), int(p[0])) for p in ordered[::step]]


class CrackDetector:
    """균열 검출기 — 학습 모델로 교체 가능한 인터페이스."""

    name = "opencv-ridge-baseline"

    def __init__(
        self,
        min_length_px: int = MIN_LENGTH_PX,
        min_elongation: float = MIN_ELONGATION,
        sensitivity: float = 1.0,
        merge_gap_px: int = 3,
        min_contrast: float = 6.0,
        psf_sigma_px: float = 2.6,
        min_sharpness: float = 45.0,
        min_waviness: float = 0.012,
        min_confidence: float = 0.58,
    ) -> None:
        self.min_length_px = min_length_px
        self.min_elongation = min_elongation
        self.sensitivity = sensitivity
        self.merge_gap_px = merge_gap_px
        self.min_contrast = min_contrast
        self.psf_sigma_px = psf_sigma_px
        self.min_sharpness = min_sharpness
        self.min_waviness = min_waviness
        self.min_confidence = min_confidence

    def detect(
        self, image: np.ndarray, mm_per_px: float | None = None
    ) -> DetectionResult:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        h, w = gray.shape[:2]

        sharp = sharpness_score(gray)
        mask, dark, normal = _segment(gray, self.sensitivity)
        n_labels, labels = _group_components(mask, self.merge_gap_px)
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)

        cracks: list[CrackInstance] = []
        kept_mask = np.zeros_like(mask)

        for i in range(1, n_labels):
            comp = labels == i
            area = int(comp.sum())
            if area < MIN_AREA_PX:
                continue

            ys, xs = np.nonzero(comp)
            x, y = int(xs.min()), int(ys.min())
            cw, ch = int(xs.max() - x + 1), int(ys.max() - y + 1)
            major, minor = max(cw, ch), max(1, min(cw, ch))
            elongation = major / minor
            fill_ratio = area / float(cw * ch)

            comp_dist = np.where(comp, dist, 0.0)
            ridge = _ridge_points(comp_dist, comp.astype(np.uint8))
            length_px = _skeleton_length(ridge)
            contrast = float(dark[comp].mean())

            if length_px < self.min_length_px:
                continue
            if contrast < self.min_contrast:
                continue
            # 짧고 뭉툭한 얼룩 제거. 길이가 충분히 길면 세장비 조건을 완화한다.
            if elongation < self.min_elongation and length_px < self.min_length_px * 3:
                continue
            if fill_ratio > MAX_FILL_RATIO and elongation < 6.0:
                continue

            ridge_pts = np.argwhere(ridge)
            # 화면을 가로지르는 완전 직선은 시공줄눈·거푸집 이음선으로 본다
            spans_frame = major > 0.55 * max(h, w)
            if spans_frame and _waviness(ridge_pts) < self.min_waviness:
                continue
            widths = _fwhm_widths(dark, ridge_pts, normal)
            if widths.size >= 3:
                widths = deconvolve_width(widths, self.psf_sigma_px)
            else:
                # 프로파일이 불안정하면 거리변환 근사로 되돌린다
                widths = 2.0 * comp_dist[ridge]
            if widths.size == 0:
                continue

            inst = CrackInstance(
                bbox=(x, y, cw, ch),
                length_px=round(float(length_px), 1),
                width_px_max=round(float(widths.max()), 2),
                width_px_p95=round(float(np.percentile(widths, 95)), 2),
                width_px_mean=round(float(widths.mean()), 2),
                area_px=area,
                elongation=round(float(elongation), 2),
                confidence=round(
                    self._confidence(elongation, fill_ratio, length_px, contrast), 3
                ),
                polyline=_polyline_from_component(ridge_pts),
            )
            if inst.confidence < self.min_confidence:
                continue
            inst.apply_scale(mm_per_px)
            cracks.append(inst)
            kept_mask[comp] = 255

        cracks.sort(key=lambda c: -(c.width_px_p95 * c.length_px))
        quality_ok = sharp >= self.min_sharpness
        return DetectionResult(
            cracks=cracks,
            mask=kept_mask,
            image_size=(h, w),
            crack_area_ratio=float(np.count_nonzero(kept_mask)) / float(h * w),
            mm_per_px=mm_per_px,
            sharpness=round(sharp, 1),
            quality_ok=quality_ok,
            quality_note=(
                ""
                if quality_ok
                else f"선명도 부족 (Laplacian var {sharp:.0f} < {self.min_sharpness:.0f}) "
                     "— 균열폭이 과대평가될 수 있어 재촬영을 권고합니다"
            ),
        )

    @staticmethod
    def _confidence(
        elongation: float, fill_ratio: float, length_px: float, contrast: float
    ) -> float:
        """형상·대비 근거의 휴리스틱 신뢰도."""
        e = min(elongation / 8.0, 1.0)
        f = 1.0 - min(fill_ratio / MAX_FILL_RATIO, 1.0)
        ell = min(length_px / (MIN_LENGTH_PX * 6.0), 1.0)
        c = min(contrast / 40.0, 1.0)
        score = 0.30 * e + 0.20 * f + 0.20 * ell + 0.30 * c
        return float(min(0.99, max(0.30, score)))


# ─── 오버레이 렌더링 ───────────────────────────────────────────
GRADE_BGR = {
    "a": (95, 185, 63),
    "b": (160, 211, 88),
    "c": (34, 153, 210),
    "d": (62, 136, 240),
    "e": (73, 81, 248),
}


def render_overlay(
    image: np.ndarray,
    cracks: list[CrackInstance],
    grades: list[str] | None = None,
    out_path: Path | None = None,
) -> np.ndarray:
    """검출 결과를 원본 위에 시각화한다."""
    canvas = image.copy() if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    grades = grades or ["c"] * len(cracks)

    for idx, (c, g) in enumerate(zip(cracks, grades), start=1):
        color = GRADE_BGR.get(g, (200, 200, 200))
        x, y, w, h = c.bbox
        cv2.rectangle(canvas, (x - 2, y - 2), (x + w + 2, y + h + 2), color, 2)

        if len(c.polyline) > 1:
            pts = np.array(c.polyline, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(canvas, [pts], False, color, 2, cv2.LINE_AA)

        width_txt = (
            f"{c.width_mm_p95:.2f}mm" if c.width_mm_p95 is not None
            else f"{c.width_px_p95:.1f}px"
        )
        label = f"#{idx} {g.upper()} {width_txt}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        ly = max(th + 6, y - 6)
        cv2.rectangle(canvas, (x - 2, ly - th - 6), (x + tw + 8, ly + 4), color, -1)
        cv2.putText(
            canvas, label, (x + 2, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
            (16, 20, 28), 1, cv2.LINE_AA,
        )

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), canvas)
    return canvas


# ─── GSD (Ground Sample Distance) ──────────────────────────────
def gsd_mm_per_px(
    distance_m: float,
    focal_length_mm: float,
    sensor_width_mm: float,
    image_width_px: int,
    gimbal_pitch_deg: float | None = None,
) -> float:
    """촬영 거리로부터 픽셀당 실제 길이(mm)를 산출한다.

    GSD = (거리 x 센서폭) / (초점거리 x 이미지폭)

    짐벌이 벽면에 수직이 아니면 사면 투영으로 실제 스케일이 늘어난다.
    pitch를 알면 1/cos(theta)로 보정한다(70도 이상은 신뢰할 수 없어 고정).
    """
    if min(distance_m, focal_length_mm, sensor_width_mm, image_width_px) <= 0:
        raise ValueError("GSD 산정 인자는 모두 양수여야 합니다")

    base = (distance_m * 1000.0 * sensor_width_mm) / (
        focal_length_mm * image_width_px
    )
    if gimbal_pitch_deg is not None:
        theta = math.radians(min(abs(gimbal_pitch_deg), 70.0))
        base /= max(math.cos(theta), 0.34)
    return round(base, 5)
