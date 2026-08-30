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

from .segmodel import get_segmenter

# ─── 오검출 제거 임계값 ────────────────────────────────────────
MIN_AREA_PX = 60          # 이보다 작은 blob은 노이즈로 간주
MIN_LENGTH_PX = 40        # 균열로 인정할 최소 골격 길이
MIN_ELONGATION = 2.5      # 장축/단축 비 — 균열은 가늘고 길다
MAX_FILL_RATIO = 0.62     # 외접사각형 대비 채움비가 높으면 얼룩/그림자

# 신뢰도 문턱. 벤치마크(data/bench_test 60장)에서 F1이 최대인 지점으로 잡았다.
CLASSIFIER_CONFIDENCE = 0.30   # 학습 분류기의 확률
HEURISTIC_CONFIDENCE = 0.58    # 분류기가 없을 때의 형상 점수


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
    features: dict[str, float] = field(default_factory=dict)

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
    # 면적형 결함(박리·백태·철근노출) — 학습 모델만 채운다.
    # 고전 검출기는 균열만 찾으므로 항상 비어 있다.
    area_defects: list[dict] = field(default_factory=list)
    # 어느 검출기가 돌았는지. 결과 해석의 전제라 결과에 남긴다.
    detector: str = "opencv-ridge-baseline"


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


def _principal_axis(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """점군의 주축 단위벡터와 중심. pts 는 (N,2) [y,x]."""
    c = pts.mean(axis=0)
    centered = pts - c
    if len(pts) < 2:
        return np.array([1.0, 0.0], np.float32), c
    _, _, vt = np.linalg.svd(centered.astype(np.float32), full_matrices=False)
    return vt[0], c


def _endpoints_and_tangents(
    ridge_pts: np.ndarray, tail: int = 6
) -> tuple[np.ndarray, np.ndarray] | None:
    """중심선의 양 끝점과 그 지점의 접선 방향.

    주축으로 정렬한 뒤 양 끝 `tail` 개 점으로 국소 접선을 구한다. 전체 주축을
    쓰면 굽은 균열에서 끝단 방향이 틀어져 엉뚱한 파편과 이어진다.
    반환: (끝점 2개 [y,x], 바깥을 향하는 접선 2개)
    """
    if len(ridge_pts) < 4:
        return None
    axis, _ = _principal_axis(ridge_pts)
    proj = (ridge_pts - ridge_pts.mean(axis=0)) @ axis
    order = np.argsort(proj)
    ordered = ridge_pts[order]

    k = min(tail, max(2, len(ordered) // 3))
    head_pts, tail_pts = ordered[:k], ordered[-k:]

    p0, p1 = ordered[0].astype(np.float32), ordered[-1].astype(np.float32)
    t0 = p0 - head_pts.mean(axis=0)          # 시작점에서 바깥으로
    t1 = p1 - tail_pts.mean(axis=0)          # 끝점에서 바깥으로

    def unit(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v / n if n > 1e-6 else axis

    return np.stack([p0, p1]), np.stack([unit(t0), unit(t1)])


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _link_fragments(
    mask: np.ndarray,
    dist: np.ndarray,
    max_link_px: float,
    min_align: float = 0.80,
) -> tuple[int, np.ndarray]:
    """끊어진 파편을 **방향이 맞을 때만** 하나의 균열로 잇는다.

    팽창(dilation)으로 묶으면 가까이 있기만 하면 붙어버려, 나란한 별개의 균열이
    한 덩어리가 되고 일직선으로 이어질 파편은 간격이 조금만 넓어도 못 잇는다.

    여기서는 각 파편의 끝점과 그 지점의 접선을 구한 뒤, 두 끝점을 잇는 벡터가
    **양쪽 접선과 모두 정렬될 때만** 연결한다. 균열의 연속은 방향의 연속이다.
    """
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 2:
        return n, labels

    # 끝점 추출은 크롭에서만 한다. 전역 마스크를 컴포넌트마다 훑으면
    # 이미지 크기 x 컴포넌트 수만큼 스캔이 발생해 실측에서 수십 초가 걸린다.
    ends: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    boxes: dict[int, tuple[int, int, int, int]] = {}
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if area < MIN_AREA_PX:
            continue                       # 잡티는 이을 대상이 아니다
        sub_lab = labels[y : y + ch, x : x + cw]
        comp = sub_lab == i
        comp_dist = np.where(comp, dist[y : y + ch, x : x + cw], 0.0)
        ridge = _ridge_points(comp_dist, comp.astype(np.uint8))
        pts = np.argwhere(ridge)
        if len(pts) < 4:
            pts = np.argwhere(comp)
        e = _endpoints_and_tangents(pts)
        if e is None:
            continue
        pts_abs, tang = e
        pts_abs = pts_abs + np.array([y, x], np.float32)   # 전역 좌표로
        ends[i] = (pts_abs, tang)
        boxes[i] = (x, y, cw, ch)

    def _box_gap(a: int, b: int) -> float:
        ax, ay, aw, ah = boxes[a]
        bx, by, bw, bh = boxes[b]
        dx = max(0, max(ax - (bx + bw), bx - (ax + aw)))
        dy = max(0, max(ay - (by + bh), by - (ay + ah)))
        return float(np.hypot(dx, dy))

    uf = _UnionFind(n)
    keys = sorted(ends)
    for ai in range(len(keys)):
        a = keys[ai]
        pa, ta = ends[a]
        for bi in range(ai + 1, len(keys)):
            b = keys[bi]
            # 외접사각형 간 거리로 먼저 걸러 낸다 (O(1) 사전검사)
            if _box_gap(a, b) > max_link_px:
                continue
            pb, tb = ends[b]
            linked = False
            for u in range(2):
                for v in range(2):
                    d = pb[v] - pa[u]
                    dist_ab = float(np.linalg.norm(d))
                    if dist_ab > max_link_px or dist_ab < 1e-6:
                        continue
                    dir_ab = d / dist_ab
                    # a의 끝단은 b쪽을 향하고, b의 끝단은 a쪽을 향해야 한다
                    align = float(dir_ab @ ta[u]) * float(-(dir_ab @ tb[v]))
                    if align >= min_align:
                        linked = True
                        break
                if linked:
                    break
            if linked:
                uf.union(a, b)

    remap = np.zeros(n, np.int32)
    next_id = 1
    seen: dict[int, int] = {}
    for i in range(1, n):
        root = uf.find(i)
        if root not in seen:
            seen[root] = next_id
            next_id += 1
        remap[i] = seen[root]
    return next_id, remap[labels]


def robust_widths(widths: np.ndarray, k: float = 3.0) -> np.ndarray:
    """MAD 기반 이상치 제거.

    FWHM 프로파일은 분기 교차점과 끝단에서 튄다. 표준편차로 자르면 이상치가
    임계 자체를 끌어올려 무력해지므로, 중앙값 절대편차(MAD)를 쓴다.
    MAD는 표본의 절반이 오염돼도 무너지지 않는다.
    """
    if widths.size < 5:
        return widths
    med = float(np.median(widths))
    mad = float(np.median(np.abs(widths - med)))
    if mad < 1e-9:
        return widths
    scale = 1.4826 * mad          # 정규분포에서 표준편차와 같아지는 보정계수
    keep = np.abs(widths - med) <= k * scale
    return widths[keep] if keep.sum() >= 3 else widths


# ─── 형상 특징 · 오검출 분류기 ─────────────────────────────────
# 특징 순서를 여기서 고정한다. 학습기와 추론기가 같은 순서를 봐야 하므로
# 이 목록을 바꾸면 반드시 모델을 다시 학습해야 한다.
FEATURE_NAMES: tuple[str, ...] = (
    "log_length",        # 길이 — 긴 것일수록 균열
    "log_area",
    "elongation",        # 장축/단축
    "fill_ratio",        # 외접사각형 채움비 — 얼룩은 높다
    "solidity",          # 볼록껍질 채움비 — 사행하는 균열은 낮다
    "waviness",          # 사행도 — 시공줄눈은 0에 가깝다
    "contrast_mean",     # 국소 암부 세기
    "contrast_cv",       # 암부 변동 — 균열은 깊이가 들쭉날쭉
    "width_mean",        # 평균 폭(px)
    "width_cv",          # 폭 변동계수 — 균열은 변하고 인쇄선/줄눈은 일정
    "ridge_density",     # 중심선 화소 / 면적 — 가늘수록 크다
    "branch_ratio",      # 분기점 비율 — 균열은 갈라진다
    "orient_entropy",    # 국소 방향 엔트로피 — 직선 0, 균열 중간, 얼룩 최대
)


@dataclass
class CrackFeatures:
    values: np.ndarray                     # FEATURE_NAMES 순서

    def as_dict(self) -> dict[str, float]:
        return {k: float(v) for k, v in zip(FEATURE_NAMES, self.values)}


def _branch_ratio(ridge: np.ndarray) -> float:
    """중심선에서 이웃이 3개 이상인 화소의 비율 = 분기 정도."""
    r = ridge.astype(np.uint8)
    total = int(r.sum())
    if total == 0:
        return 0.0
    kernel = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.uint8)
    neigh = cv2.filter2D(r, -1, kernel, borderType=cv2.BORDER_CONSTANT)
    return float(((neigh >= 3) & (r > 0)).sum()) / total


def _orientation_entropy(ridge_pts: np.ndarray, bins: int = 8) -> float:
    """국소 접선 방향의 분포 엔트로피 (0~1로 정규화).

    완전 직선은 한 방향에 몰려 0에 가깝고, 사행하는 균열은 중간, 형태가 없는
    얼룩은 방향이 고르게 퍼져 1에 가깝다.
    """
    if len(ridge_pts) < 6:
        return 0.0
    axis, _ = _principal_axis(ridge_pts)
    order = np.argsort((ridge_pts - ridge_pts.mean(axis=0)) @ axis)
    p = ridge_pts[order].astype(np.float32)
    d = np.diff(p, axis=0)
    n = np.linalg.norm(d, axis=1)
    d = d[n > 1e-6]
    if len(d) < 3:
        return 0.0
    ang = np.arctan2(d[:, 0], d[:, 1]) % np.pi        # 방향은 180도 주기
    hist, _ = np.histogram(ang, bins=bins, range=(0.0, np.pi))
    pr = hist / max(hist.sum(), 1)
    pr = pr[pr > 0]
    ent = float(-(pr * np.log(pr)).sum())
    return ent / float(np.log(bins))


def extract_features(
    comp: np.ndarray,
    ridge: np.ndarray,
    ridge_pts: np.ndarray,
    dark: np.ndarray,
    widths_px: np.ndarray,
    length_px: float,
    bbox: tuple[int, int, int, int],
) -> CrackFeatures:
    x, y, w, h = bbox
    area = float(comp.sum())
    major, minor = float(max(w, h)), float(max(1, min(w, h)))
    fill = area / float(max(w * h, 1))

    cnts, _ = cv2.findContours(
        comp.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    hull_area = 0.0
    for c in cnts:
        if len(c) >= 3:
            hull_area += float(cv2.contourArea(cv2.convexHull(c)))
    solidity = area / hull_area if hull_area > 1e-6 else 1.0

    dv = dark[comp].astype(np.float32)
    c_mean = float(dv.mean()) if dv.size else 0.0
    c_cv = float(dv.std() / c_mean) if c_mean > 1e-6 else 0.0

    w_mean = float(widths_px.mean()) if widths_px.size else 0.0
    w_cv = float(widths_px.std() / w_mean) if w_mean > 1e-6 else 0.0

    values = np.array(
        [
            np.log1p(length_px),
            np.log1p(area),
            major / minor,
            fill,
            min(solidity, 2.0),
            _waviness(ridge_pts),
            c_mean / 64.0,
            c_cv,
            w_mean / 10.0,
            w_cv,
            float(ridge.sum()) / max(area, 1.0),
            _branch_ratio(ridge),
            _orientation_entropy(ridge_pts),
        ],
        np.float32,
    )
    return CrackFeatures(np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0))


class FalsePositiveFilter:
    """합성 데이터의 정답으로 학습한 오검출 판별기 (로지스틱 회귀).

    형상 규칙을 손으로 늘리면 하나를 막을 때마다 다른 하나가 새는데, 규칙 간
    가중치를 사람이 정할 근거가 없다. 정답이 있는 합성 표본으로 가중치를
    학습시키면 그 근거가 데이터에서 나온다.

    선형 모델을 쓴 이유는 계수를 눈으로 읽어 검증할 수 있기 때문이다.
    안전 판정에 들어가는 구성요소는 설명 가능해야 한다.
    """

    def __init__(
        self, mean: np.ndarray, std: np.ndarray, weights: np.ndarray, bias: float
    ) -> None:
        self.mean = mean
        self.std = std
        self.weights = weights
        self.bias = bias

    @classmethod
    def load(cls, path: Path) -> "FalsePositiveFilter | None":
        if not path.exists():
            return None
        try:
            import json

            d = json.loads(path.read_text(encoding="utf-8"))
            if tuple(d.get("features", ())) != FEATURE_NAMES:
                return None            # 특징 순서가 바뀌었으면 쓰지 않는다
            return cls(
                np.array(d["mean"], np.float32),
                np.array(d["std"], np.float32),
                np.array(d["weights"], np.float32),
                float(d["bias"]),
            )
        except Exception:
            return None

    def score(self, feats: CrackFeatures) -> float:
        """균열일 확률 (0~1)."""
        z = (feats.values - self.mean) / np.where(self.std > 1e-9, self.std, 1.0)
        logit = float(z @ self.weights + self.bias)
        return float(1.0 / (1.0 + np.exp(-np.clip(logit, -30.0, 30.0))))


DEFAULT_FP_MODEL = (
    Path(__file__).resolve().parents[3] / "models" / "fp_filter.json"
)


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
        # MAD 이상치 제거를 도입하면서 재보정한 값. 이상치를 남긴 채 맞췄던
        # 2.6은 과보정이 되어 폭을 계통적으로 과소평가했다(편향 -0.26mm).
        # 2.2에서 편향이 -0.003mm로 사실상 0이 된다.
        #
        # 1.8을 쓰면 등급 일치율이 0.793으로 더 높지만 +0.16mm 과대평가가 남는다.
        # 안전 쪽으로 치우친 값이라 유혹적이지만 채택하지 않았다 — 계측기는
        # 편향이 없어야 하고, 보수적 여유는 측정값이 아니라 판정 기준이 갖는다.
        psf_sigma_px: float = 2.2,
        min_sharpness: float = 45.0,
        min_waviness: float = 0.012,
        min_confidence: float | None = None,
        max_link_px: float = 26.0,
        min_align: float = 0.80,
        tile_threshold_px: int = 1600,
        tile_size_px: int = 1024,
        tile_overlap_px: int = 128,
        fp_model: Path | None = None,
        use_model: bool = True,
    ) -> None:
        self.min_length_px = min_length_px
        self.min_elongation = min_elongation
        self.sensitivity = sensitivity
        self.merge_gap_px = merge_gap_px
        self.min_contrast = min_contrast
        self.psf_sigma_px = psf_sigma_px
        self.min_sharpness = min_sharpness
        self.min_waviness = min_waviness
        self.max_link_px = max_link_px
        self.min_align = min_align
        self.tile_threshold_px = tile_threshold_px
        self.tile_size_px = tile_size_px
        self.tile_overlap_px = tile_overlap_px
        self.use_model = use_model
        self.fp_filter = FalsePositiveFilter.load(fp_model or DEFAULT_FP_MODEL)
        # 문턱은 신뢰도 점수의 출처에 따라 다르다. 학습 분류기의 확률과
        # 휴리스틱 점수는 스케일이 달라 같은 값을 쓰면 한쪽이 반드시 어긋난다.
        if min_confidence is not None:
            self.min_confidence = min_confidence
        else:
            self.min_confidence = (
                CLASSIFIER_CONFIDENCE if self.fp_filter is not None
                else HEURISTIC_CONFIDENCE
            )

    def detect(
        self, image: np.ndarray, mm_per_px: float | None = None
    ) -> DetectionResult:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        h, w = gray.shape[:2]

        # 큰 원본은 타일로 나눠 원해상도로 처리한다. 통째로 축소하면 미세 균열이
        # 리샘플링에서 사라지고, 통째로 처리하면 적응 임계가 전역 통계에 눌려
        # 국부적으로 옅은 균열을 놓친다.
        if max(h, w) > self.tile_threshold_px:
            return self._detect_tiled(gray, mm_per_px)

        sharp = sharpness_score(gray)
        mask, _, _ = _segment(gray, self.sensitivity)
        mask = self._augment_mask(gray, mask)
        result = self._detect_from_mask(gray, mask, mm_per_px)

        quality_ok = sharp >= self.min_sharpness
        result.sharpness = round(sharp, 1)
        result.quality_ok = quality_ok
        result.quality_note = (
            ""
            if quality_ok
            else f"선명도 부족 (Laplacian var {sharp:.0f} < {self.min_sharpness:.0f}) "
                 "— 균열폭이 과대평가될 수 있어 재촬영을 권고합니다"
        )
        return result

    def _augment_mask(self, gray: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """학습 분할기가 있으면 그 마스크를 합친다.

        재현율 병목이 후보 생성에 있기 때문에 여기서 손을 댄다. 배경 대비가
        낮아 능선·암부 조건을 동시에 넘지 못한 균열은 하류에서 되살릴 방법이
        없다 — 없는 후보는 분류기가 살릴 수 없다.

        합집합으로 두는 이유: 고전 분할이 이미 잘 잡던 뚜렷한 균열까지 모델
        성능에 인질로 잡히지 않게 한다. 늘어난 후보의 정밀도는 하류의 형상
        분류기가 지킨다 — 그것이 분류기를 둔 목적이다.

        모델이 없으면 아무 일도 하지 않는다. 학습 산출물이 배포에 없더라도
        검출은 동작해야 한다.
        """
        if not self.use_model:
            return mask
        seg = get_segmenter()
        if seg is None:
            return mask
        try:
            learned = seg.mask(gray, self.sensitivity)
        except Exception:
            # 추론이 실패해도 검출 전체를 멈추지 않는다.
            return mask
        return np.maximum(mask, learned)

    def _detect_from_mask(
        self, gray: np.ndarray, mask: np.ndarray, mm_per_px: float | None
    ) -> DetectionResult:
        """주어진 균열 마스크로부터 인스턴스를 만든다.

        단일 이미지 경로와 타일 병합 경로가 이 함수를 공유한다. 두 경로가 서로
        다른 인스턴스 생성 규칙을 갖게 되면 타일 경계에서만 결과가 달라진다.
        """
        h, w = gray.shape[:2]
        eq = _equalize(gray)
        _, normal = _ridge_response(eq, scales=(1.0, 2.0, 3.5, 5.0))
        dark = _dark_field(eq)

        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        # 근접이 아니라 방향 일치로 잇는다 — 나란한 별개 균열이 뭉치지 않는다
        n_labels, labels = _link_fragments(
            mask, dist, self.max_link_px, self.min_align
        )

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
                # 분기 교차점·끝단에서 튀는 값을 MAD로 걷어낸 뒤 PSF를 보정한다.
                # 순서가 중요하다 — 이상치를 남긴 채 보정하면 이상치도 함께 보정된다.
                widths = deconvolve_width(
                    robust_widths(widths), self.psf_sigma_px
                )
            else:
                # 프로파일이 불안정하면 거리변환 근사로 되돌린다
                widths = 2.0 * comp_dist[ridge]
            if widths.size == 0:
                continue

            feats = extract_features(
                comp, ridge, ridge_pts, dark, widths, length_px, (x, y, cw, ch)
            )
            if self.fp_filter is not None:
                confidence = self.fp_filter.score(feats)
            else:
                confidence = self._confidence(
                    elongation, fill_ratio, length_px, contrast
                )

            inst = CrackInstance(
                bbox=(x, y, cw, ch),
                length_px=round(float(length_px), 1),
                width_px_max=round(float(widths.max()), 2),
                width_px_p95=round(float(np.percentile(widths, 95)), 2),
                width_px_mean=round(float(widths.mean()), 2),
                area_px=area,
                elongation=round(float(elongation), 2),
                confidence=round(float(confidence), 3),
                polyline=_polyline_from_component(ridge_pts),
                features=feats.as_dict(),
            )
            if inst.confidence < self.min_confidence:
                continue
            inst.apply_scale(mm_per_px)
            cracks.append(inst)
            kept_mask[comp] = 255

        cracks.sort(key=lambda c: -(c.width_px_p95 * c.length_px))
        # 선명도 판정은 호출자가 채운다 — 타일 경로는 원본 전체로 재야 한다
        return DetectionResult(
            cracks=cracks,
            mask=kept_mask,
            image_size=(h, w),
            crack_area_ratio=float(np.count_nonzero(kept_mask)) / float(h * w),
            mm_per_px=mm_per_px,
        )

    # ─── 타일 기반 다중해상도 ──────────────────────────────────
    def _detect_tiled(
        self, gray: np.ndarray, mm_per_px: float | None
    ) -> DetectionResult:
        """겹치는 타일로 나눠 원해상도로 검출한 뒤 좌표를 되돌려 병합한다.

        타일 경계에 걸친 균열은 양쪽에서 잘려 두 건으로 잡힌다. 겹침 폭을 두고,
        병합 단계에서 중심선이 맞닿는 조각을 다시 잇는다.
        """
        h, w = gray.shape[:2]
        step = max(64, self.tile_size_px - self.tile_overlap_px)

        full_mask = np.zeros((h, w), np.uint8)
        sharp = sharpness_score(gray)

        # 타일 안에서는 재귀하지 않도록 임계를 크게 잡은 복제 검출기를 쓴다
        inner = CrackDetector(
            min_length_px=self.min_length_px,
            min_elongation=self.min_elongation,
            sensitivity=self.sensitivity,
            merge_gap_px=self.merge_gap_px,
            min_contrast=self.min_contrast,
            psf_sigma_px=self.psf_sigma_px,
            min_sharpness=0.0,
            min_waviness=self.min_waviness,
            min_confidence=self.min_confidence,
            max_link_px=self.max_link_px,
            min_align=self.min_align,
            tile_threshold_px=10**9,
            use_model=self.use_model,
        )
        inner.fp_filter = self.fp_filter

        for y0 in range(0, max(h - self.tile_overlap_px, 1), step):
            for x0 in range(0, max(w - self.tile_overlap_px, 1), step):
                y1 = min(y0 + self.tile_size_px, h)
                x1 = min(x0 + self.tile_size_px, w)
                if (y1 - y0) < 64 or (x1 - x0) < 64:
                    continue
                sub = inner.detect(gray[y0:y1, x0:x1], mm_per_px)
                full_mask[y0:y1, x0:x1] = np.maximum(
                    full_mask[y0:y1, x0:x1], sub.mask
                )

        # 병합된 마스크에서 인스턴스를 다시 만든다. 타일 경계에서 잘린 조각이
        # 여기서 하나로 이어진다 — 타일마다 나온 결과를 그냥 합치면 중복이 남는다.
        merged = CrackDetector(
            min_length_px=self.min_length_px,
            min_elongation=self.min_elongation,
            sensitivity=self.sensitivity,
            merge_gap_px=self.merge_gap_px,
            min_contrast=0.0,          # 이미 통과한 화소만 남아 있다
            psf_sigma_px=self.psf_sigma_px,
            min_sharpness=0.0,
            min_waviness=self.min_waviness,
            min_confidence=self.min_confidence,
            max_link_px=self.max_link_px,
            min_align=self.min_align,
            tile_threshold_px=10**9,
            use_model=self.use_model,
        )
        merged.fp_filter = self.fp_filter
        result = merged._detect_from_mask(gray, full_mask, mm_per_px)

        quality_ok = sharp >= self.min_sharpness
        result.sharpness = round(sharp, 1)
        result.quality_ok = quality_ok
        result.quality_note = (
            ""
            if quality_ok
            else f"선명도 부족 (Laplacian var {sharp:.0f} < {self.min_sharpness:.0f}) "
                 "— 균열폭이 과대평가될 수 있어 재촬영을 권고합니다"
        )
        return result

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
