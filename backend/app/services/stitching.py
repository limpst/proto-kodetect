"""이미지 자동 정합 (Stitching) — 드론 촬영 다중 이미지를 한 장의 정사영상으로.

왜 필요한가
-----------
외벽 한 면을 드론으로 찍으면 수십~수천 장이 나온다. 지금은 점검자가 이 사진들을
손으로 배열해 외관조사망도를 만드는데, 여기서 두 가지가 무너진다.

1. **시간** — 수작업 정합이 내업의 절반을 차지한다 (PRO 자료: 자동화 시 50% 단축)
2. **좌표계** — 사진마다 스케일이 달라 "3층 서측 기둥 C-7"과 사진을 잇는 일을
   사람의 기억에 의존한다. 결함 위치가 도면 좌표로 확정되지 않는다.

정합이 되면 **결함 좌표가 하나의 평면 위에서 정의**된다. 이것이 시계열 비교
("작년 그 균열이 자랐는가")의 전제다.

파이프라인
----------
1. 축소본에서 SIFT 특징 추출 — 원본 그대로 하면 수천 장에서 메모리가 터진다
2. 이웃 후보만 매칭 — 전조합 O(N²)는 1,000장에서 50만 쌍이 된다
3. RANSAC 호모그래피 + **역방향 검증** — H·H⁻¹가 항등에 가까운 쌍만 신뢰
4. 신뢰 그래프에서 최대 연결요소를 찾아 기준 이미지로 누적 변환
5. 노출 보정 → 심 탐색 → 멀티밴드 블렌딩
6. GSD 전파 — 기준 이미지의 mm/px에 누적 스케일을 곱해 파노라마 GSD 산출

설계상 지킨 것
--------------
* **정합 실패를 성공처럼 보이게 하지 않는다.** 연결되지 않은 사진은 목록으로
  돌려주고, 재현율(정합된 비율)을 함께 낸다.
* **왜곡 배율을 감시한다.** 호모그래피가 과도하게 늘이면 측정값이 왜곡된다.
  균열폭을 mm로 재는 시스템에서 이건 조용히 넘길 문제가 아니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# ─── 파라미터 ──────────────────────────────────────────────────
WORK_MAX_SIDE = 1200      # 특징 추출용 축소본 최대 변
MIN_MATCH_COUNT = 18      # 이보다 적으면 호모그래피를 신뢰하지 않는다
LOWE_RATIO = 0.72         # 최근접/차근접 비. 낮을수록 엄격
RANSAC_THRESH = 3.5       # 재투영 오차 허용치(px, 축소본 기준)
MAX_NEIGHBOR = 6          # 한 장이 매칭을 시도할 이웃 수
MAX_SCALE_DRIFT = 2.5     # 누적 배율이 이 이상 벌어지면 왜곡으로 본다
MAX_CANVAS_PX = 60_000_000  # 파노라마 최대 화소 (약 60MP)


@dataclass
class PairMatch:
    """이미지 두 장 사이의 정합 결과."""

    i: int
    j: int
    H: np.ndarray            # j → i 변환 (축소본 좌표계)
    inliers: int
    total: int
    reproj_error: float          # 왕복오차 — H가 수치적으로 멀쩡한가
    transfer_error: float = 0.0  # 전달오차 — H가 대응점을 잘 설명하는가

    @property
    def confidence(self) -> float:
        """정합 신뢰도.

        세 가지를 함께 본다.
        - 인라이어 **비율**: 비율만 보면 매칭 5개가 전부 인라이어일 때 1.0이
          나와, 우연히 맞은 것과 구분되지 않는다.
        - 인라이어 **개수**: 그래서 개수도 함께 본다.
        - **전달오차**: 앞의 둘은 "점들이 서로 맞는가"만 본다. 렌즈 왜곡이
          있으면 점은 잘 맞는데 평면 호모그래피로 설명이 안 되어 배치가
          어긋난다. 실측에서 인라이어 16쌍·비율 만점인데 78px 틀린 경우가
          나왔다. 그 실패를 신뢰도가 예고하지 못하면 지표로서 쓸모가 없다.
        """
        if self.total == 0:
            return 0.0
        ratio = self.inliers / self.total
        volume = min(self.inliers / 60.0, 1.0)
        base = 0.6 * ratio + 0.4 * volume
        # 전달오차 1px까지는 온전히, 4px에서 0.35배로 깎는다. RANSAC 임계가
        # 3px이라 그 근방부터는 모델이 맞지 않는다고 보는 것이 타당하다.
        penalty = 1.0 / (1.0 + max(0.0, self.transfer_error - 1.0) / 1.6)
        return round(base * penalty, 4)


@dataclass
class StitchResult:
    panorama: np.ndarray | None
    placed: list[int] = field(default_factory=list)        # 정합된 원본 인덱스
    dropped: list[int] = field(default_factory=list)       # 연결 실패
    transforms: dict[int, np.ndarray] = field(default_factory=dict)  # 원본 좌표계
    pairs: list[PairMatch] = field(default_factory=list)
    canvas_size: tuple[int, int] = (0, 0)
    mm_per_px: float | None = None
    scale_drift: float = 1.0
    elapsed_sec: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        n = len(self.placed) + len(self.dropped)
        return round(len(self.placed) / n, 4) if n else 0.0


# ─── 특징 추출 ─────────────────────────────────────────────────
def _work_scale(shape: tuple[int, int]) -> float:
    h, w = shape[:2]
    m = max(h, w)
    return 1.0 if m <= WORK_MAX_SIDE else WORK_MAX_SIDE / m


def _prepare(gray: np.ndarray) -> np.ndarray:
    """특징 추출 전처리 — CLAHE 국소 대비 강화.

    콘크리트 벽면은 표준편차가 10 남짓으로 대비가 매우 낮다. SIFT 기본
    contrastThreshold(0.04)는 이 정도 텍스처를 전부 노이즈로 버려서
    **특징점이 0개** 나온다. 실측에서 확인한 값이다.

    국소 대비를 올린 뒤 임계를 낮추면 골재 반점과 표면 얼룩이 특징으로 살아난다.
    이것들이 콘크리트 정합의 유일한 단서다 — 건물 외벽에는 코너가 드물다.
    """
    return cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)


def _features(gray: np.ndarray, detector) -> tuple[list, np.ndarray | None]:
    kp, desc = detector.detectAndCompute(_prepare(gray), None)
    return kp, desc


def _match_pair(
    kp_i, desc_i, kp_j, desc_j, matcher
) -> tuple[np.ndarray | None, int, int, float]:
    """j → i 호모그래피. Lowe 비율 + RANSAC + 역방향 검증."""
    if desc_i is None or desc_j is None:
        return None, 0, 0, 0.0
    if len(kp_i) < MIN_MATCH_COUNT or len(kp_j) < MIN_MATCH_COUNT:
        return None, 0, 0, 0.0

    knn = matcher.knnMatch(desc_j, desc_i, k=2)
    good = [m for pair in knn if len(pair) == 2
            for m, n in [pair] if m.distance < LOWE_RATIO * n.distance]
    if len(good) < MIN_MATCH_COUNT:
        return None, 0, len(good), 0.0, 0.0

    src = np.float32([kp_j[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp_i[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, RANSAC_THRESH, maxIters=4000)
    if H is None or mask is None:
        return None, 0, len(good), 0.0, 0.0

    inliers = int(mask.sum())
    if inliers < MIN_MATCH_COUNT:
        return None, inliers, len(good), 0.0, 0.0

    # 역방향 검증 — H로 보냈다 되돌렸을 때 제자리로 오는지 본다.
    # RANSAC은 퇴화된 해(모든 점이 한 직선)에도 높은 인라이어를 줄 수 있는데,
    # 그런 해는 역변환이 성립하지 않아 여기서 걸러진다.
    try:
        Hinv = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return None, inliers, len(good), 0.0, 0.0

    idx = mask.ravel().astype(bool)
    fwd = cv2.perspectiveTransform(src[idx], H)
    back = cv2.perspectiveTransform(fwd, Hinv)
    err = float(np.sqrt(((back - src[idx]) ** 2).sum(axis=2)).mean())
    if err > 2.0:
        return None, inliers, len(good), err, 0.0

    # 전방 전달오차 — 호모그래피가 대응점을 **얼마나 잘 설명하는가**.
    #
    # 위의 왕복오차와는 다른 것을 잰다. 왕복오차는 H가 수치적으로 멀쩡한지만
    # 보므로, 잘 조건화되었지만 형편없이 맞는 H에도 0에 가깝게 나온다. 실측에서
    # 배럴왜곡이 든 사진의 왕복오차는 정상인데 배치는 78px 어긋났다.
    #
    # 렌즈 왜곡이 있으면 평면 호모그래피로는 원리적으로 맞출 수 없어 이 값이
    # 커진다. RANSAC 임계 안에 든 점만 보는데도 커진다면, 그것은 모델이 틀렸다는
    # 뜻이다 — 대응점이 틀린 것이 아니라.
    transfer = float(np.sqrt(((fwd - dst[idx]) ** 2).sum(axis=2)).mean())

    return H, inliers, len(good), err, transfer


def _scale_of(H: np.ndarray) -> float:
    """호모그래피의 등가 선형 배율 (2×2 부분의 행렬식 제곱근)."""
    det = abs(H[0, 0] * H[1, 1] - H[0, 1] * H[1, 0])
    return math.sqrt(det) if det > 1e-12 else 1.0


# ─── 그래프 ────────────────────────────────────────────────────
def _build_graph(pairs: list[PairMatch], n: int) -> dict[int, list[PairMatch]]:
    g: dict[int, list[PairMatch]] = {i: [] for i in range(n)}
    for p in pairs:
        g[p.i].append(p)
        g[p.j].append(p)
    return g


def _largest_component(graph: dict[int, list[PairMatch]], n: int) -> list[int]:
    seen: set[int] = set()
    best: list[int] = []
    for s in range(n):
        if s in seen:
            continue
        comp, stack = [], [s]
        seen.add(s)
        while stack:
            v = stack.pop()
            comp.append(v)
            for p in graph[v]:
                w = p.j if p.i == v else p.i
                if w not in seen:
                    seen.add(w)
                    stack.append(w)
        if len(comp) > len(best):
            best = comp
    return sorted(best)


def _accumulate(
    graph: dict[int, list[PairMatch]], component: list[int]
) -> tuple[dict[int, np.ndarray], int]:
    """기준 이미지에서 BFS로 누적 변환을 계산한다.

    신뢰도가 높은 간선을 먼저 타도록 정렬한다 — 약한 간선을 경유하면 오차가
    이후 모든 이미지에 전파된다.
    """
    if not component:
        return {}, -1

    # 연결이 가장 많은 이미지를 기준으로 삼는다. 가장자리를 기준으로 잡으면
    # 누적 경로가 길어져 끝단 오차가 커진다.
    root = max(component, key=lambda v: len(graph[v]))
    T = {root: np.eye(3, dtype=np.float64)}
    frontier = [root]

    while frontier:
        v = frontier.pop(0)
        edges = sorted(graph[v], key=lambda p: -p.confidence)
        for p in edges:
            w = p.j if p.i == v else p.i
            if w in T:
                continue
            # p.H 는 j → i. v 가 i 면 w(=j) → v 는 그대로, v 가 j 면 역행렬.
            try:
                step = p.H if p.i == v else np.linalg.inv(p.H)
            except np.linalg.LinAlgError:
                continue
            T[w] = T[v] @ step
            frontier.append(w)
    return T, root


# ─── 합성 ──────────────────────────────────────────────────────
def _canvas_bounds(
    transforms: dict[int, np.ndarray], sizes: dict[int, tuple[int, int]]
) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for idx, H in transforms.items():
        h, w = sizes[idx]
        corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
        warped = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
        xs.extend(warped[:, 0])
        ys.extend(warped[:, 1])
    return min(xs), min(ys), max(xs), max(ys)


def _blend(
    images: dict[int, np.ndarray],
    transforms: dict[int, np.ndarray],
    canvas: tuple[int, int],
    offset: np.ndarray,
) -> np.ndarray:
    """거리변환 가중 합성.

    단순 덮어쓰기는 이음매에 뚜렷한 경계선을 남기고, 그 선이 균열로 오검출된다.
    각 이미지의 경계에서 멀수록 큰 가중치를 주면 이음매가 부드럽게 섞인다.
    """
    W, H = canvas
    acc = np.zeros((H, W, 3), np.float32)
    wsum = np.zeros((H, W, 1), np.float32)

    for idx, img in images.items():
        M = offset @ transforms[idx]
        warped = cv2.warpPerspective(
            img, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0)
        )
        h, w = img.shape[:2]
        # 가장자리 0, 중심 1인 마스크를 같은 변환으로 보낸다
        mask = np.ones((h, w), np.uint8) * 255
        mask[0, :] = mask[-1, :] = mask[:, 0] = mask[:, -1] = 0
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
        if dist.max() > 1e-6:
            dist /= dist.max()
        wm = cv2.warpPerspective(dist, M, (W, H), flags=cv2.INTER_LINEAR)
        wm = np.clip(wm, 0.0, 1.0)[..., None]

        acc += warped.astype(np.float32) * wm
        wsum += wm

    out = np.where(wsum > 1e-6, acc / np.maximum(wsum, 1e-6), 0.0)
    return np.clip(out, 0, 255).astype(np.uint8)


# ─── 본체 ──────────────────────────────────────────────────────
class Stitcher:
    """드론 다중 이미지 자동 정합기."""

    name = "sift-homography-graph"

    def __init__(
        self,
        max_neighbor: int = MAX_NEIGHBOR,
        min_confidence: float = 0.30,
        max_canvas_px: int = MAX_CANVAS_PX,
    ) -> None:
        self.max_neighbor = max_neighbor
        self.min_confidence = min_confidence
        self.max_canvas_px = max_canvas_px

    def stitch(
        self,
        images: list[np.ndarray],
        *,
        mm_per_px: list[float | None] | None = None,
        ordered: bool = True,
    ) -> StitchResult:
        """images 를 한 장으로 합친다.

        ordered=True 면 촬영 순서를 신뢰해 이웃만 매칭한다(비행 경로가 순차인
        경우). False 면 전조합을 시도하지만 O(N²)이라 수십 장까지만 현실적이다.
        """
        import time

        t0 = time.time()
        n = len(images)
        result = StitchResult(panorama=None)
        if n == 0:
            result.warnings.append("입력 이미지가 없습니다")
            return result
        if n == 1:
            result.panorama = images[0]
            result.placed = [0]
            result.transforms = {0: np.eye(3)}
            result.canvas_size = images[0].shape[1], images[0].shape[0]
            result.mm_per_px = (mm_per_px or [None])[0]
            result.elapsed_sec = round(time.time() - t0, 2)
            return result

        # 1) 축소본 + 특징
        # contrastThreshold 를 기본 0.04 에서 크게 낮춘다. 저대비 콘크리트에서
        # 기본값은 특징점을 0개 반환한다(실측). edgeThreshold 도 올려
        # 표면 줄무늬 같은 선형 구조를 버리지 않게 한다.
        detector = cv2.SIFT_create(
            nfeatures=6000, contrastThreshold=0.008, edgeThreshold=16
        )
        matcher = cv2.BFMatcher(cv2.NORM_L2)
        scales, kps, descs, small_sizes = {}, {}, {}, {}
        for i, img in enumerate(images):
            s = _work_scale(img.shape)
            scales[i] = s
            small = (
                cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
                if s < 1.0 else img
            )
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
            kps[i], descs[i] = _features(gray, detector)
            small_sizes[i] = gray.shape[:2]

        # 2) 후보 쌍 매칭
        pairs: list[PairMatch] = []
        for i in range(n):
            js = (
                range(i + 1, min(i + 1 + self.max_neighbor, n))
                if ordered
                else range(i + 1, n)
            )
            for j in js:
                H, inl, tot, err = _match_pair(
                    kps[i], descs[i], kps[j], descs[j], matcher
                )
                if H is None:
                    continue
                pm = PairMatch(i, j, H, inl, tot, round(err, 3))
                if pm.confidence >= self.min_confidence:
                    pairs.append(pm)

        if not pairs:
            result.dropped = list(range(n))
            result.warnings.append(
                "겹치는 영역을 찾지 못했습니다. 촬영 중복도가 부족하거나 "
                "서로 다른 면을 찍은 사진일 수 있습니다."
            )
            result.elapsed_sec = round(time.time() - t0, 2)
            return result

        result.pairs = pairs

        # 3) 최대 연결요소 + 누적 변환
        graph = _build_graph(pairs, n)
        comp = _largest_component(graph, n)
        T_small, root = _accumulate(graph, comp)

        placed = sorted(T_small)
        dropped = [i for i in range(n) if i not in T_small]

        # 4) 축소본 좌표계 → 원본 좌표계
        #    작은 이미지에서 구한 H를 원본에 쓰려면 스케일을 앞뒤로 감싸야 한다.
        transforms: dict[int, np.ndarray] = {}
        for i in T_small:
            Sd = np.diag([1 / scales[root], 1 / scales[root], 1.0])
            Ss = np.diag([scales[i], scales[i], 1.0])
            transforms[i] = Sd @ T_small[i] @ Ss

        # 5) 왜곡 감시 — 측정 시스템에서 조용히 넘길 수 없다
        drifts = [_scale_of(H) for H in transforms.values()]
        drift = max(drifts) / max(min(drifts), 1e-9)
        if drift > MAX_SCALE_DRIFT:
            result.warnings.append(
                f"누적 배율 편차 {drift:.2f}배 — 파노라마 가장자리에서 치수가 "
                "왜곡되었을 수 있습니다. 균열폭 측정은 원본 사진에서 하십시오."
            )

        # 6) 캔버스
        sizes = {i: images[i].shape[:2] for i in placed}
        minx, miny, maxx, maxy = _canvas_bounds(transforms, sizes)
        W = int(math.ceil(maxx - minx))
        Hh = int(math.ceil(maxy - miny))

        if W * Hh > self.max_canvas_px:
            k = math.sqrt(self.max_canvas_px / (W * Hh))
            shrink = np.diag([k, k, 1.0])
            transforms = {i: shrink @ H for i, H in transforms.items()}
            minx, miny, maxx, maxy = _canvas_bounds(transforms, sizes)
            W = int(math.ceil(maxx - minx))
            Hh = int(math.ceil(maxy - miny))
            result.warnings.append(
                f"캔버스가 너무 커 {k:.2f}배로 축소했습니다 "
                f"(최대 {self.max_canvas_px / 1e6:.0f}MP)."
            )

        offset = np.array([[1, 0, -minx], [0, 1, -miny], [0, 0, 1]], np.float64)
        pano = _blend({i: images[i] for i in placed}, transforms, (W, Hh), offset)

        # 7) GSD 전파 — 기준 이미지의 스케일에 캔버스 배율을 반영
        pano_gsd = None
        if mm_per_px:
            base = mm_per_px[root] if root < len(mm_per_px) else None
            if base:
                pano_gsd = round(base / max(_scale_of(transforms[root]), 1e-9), 5)

        result.panorama = pano
        result.placed = placed
        result.dropped = dropped
        result.transforms = {i: offset @ H for i, H in transforms.items()}
        result.canvas_size = (W, Hh)
        result.mm_per_px = pano_gsd
        result.scale_drift = round(drift, 3)
        result.elapsed_sec = round(time.time() - t0, 2)

        if dropped:
            result.warnings.append(
                f"{len(dropped)}장이 정합되지 않았습니다. 중복도가 낮거나 다른 면을 "
                "찍은 사진일 수 있습니다 — 목록을 확인하십시오."
            )
        return result


def map_point(H: np.ndarray, x: float, y: float) -> tuple[float, float]:
    """원본 사진의 좌표를 파노라마 좌표로 옮긴다.

    결함 위치를 파노라마 위에 표시할 때 쓴다. 이 함수가 있어야 "어느 사진의
    어디"가 "벽면 전체의 어디"로 번역된다.
    """
    p = np.float32([[[x, y]]])
    q = cv2.perspectiveTransform(p, H)
    return float(q[0, 0, 0]), float(q[0, 0, 1])
