"""드론 촬영 콘크리트 결함 영상 합성기.

학습·검증용 대규모 표본을 절차적으로 생성한다. 실촬영 데이터가 수백 장
수준일 때 수만 장 규모의 사전학습 세트를 만드는 것이 목적이며, 모든 표본은
픽셀 단위 마스크와 **정답 균열폭(mm)** 을 함께 갖는다.

생성 요소
---------
* 콘크리트 표면 — 다중 옥타브 값잡음 + 골재 반점 + 얼룩/오염
* 균열 — 분기하는 랜덤워크 경로, 경로를 따라 변하는 폭(테이퍼)
* 부가 결함 — 박리·백태·철근노출·누수·재료분리·손상
* 드론 효과 — 원근 왜곡, 모션블러, 비네팅, 노출 변동, 센서 노이즈

정답 균열폭은 **최종 렌더링된 마스크**에서 거리변환 능선으로 측정한다.
그리기 단계의 명목 선 두께를 쓰면 안티에일리어싱·분기 중첩·원근 왜곡 때문에
실제 화소상의 폭과 어긋난다. 검출기와 완전히 동일한 추정량 정의를 쓰므로
폭 오차(MAE)가 알고리즘 성능만을 반영한다.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

DEFECT_TYPES = (
    "crack",
    "spalling",
    "efflorescence",
    "leakage",
    "rebar_exposure",
    "segregation",
    "damage",
)


@dataclass
class SynthDefect:
    """합성된 결함 1건의 정답 라벨."""

    defect_type: str
    bbox: tuple[int, int, int, int]
    width_mm_p95: float | None = None
    width_mm_max: float | None = None
    length_mm: float | None = None
    area_ratio: float = 0.0
    polyline: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class SynthSample:
    """합성 표본 1건 — 이미지 · 마스크 · 라벨."""

    image: np.ndarray
    crack_mask: np.ndarray
    defect_mask: np.ndarray
    defects: list[SynthDefect]
    mm_per_px: float
    meta: dict


# ─── 배경: 콘크리트 표면 ───────────────────────────────────────
def _value_noise(rng: np.random.Generator, h: int, w: int, octaves: int = 5) -> np.ndarray:
    """다중 옥타브 값잡음 — 콘크리트 표면의 얼룩덜룩한 명암."""
    out = np.zeros((h, w), np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        step = max(2, int(min(h, w) / (2 ** (o + 1))))
        small = rng.random((max(2, h // step), max(2, w // step))).astype(np.float32)
        out += amp * cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
        total += amp
        amp *= 0.55
    return out / max(total, 1e-6)


def concrete_surface(rng: np.random.Generator, h: int, w: int) -> np.ndarray:
    """콘크리트 벽면 배경 생성 (BGR uint8)."""
    base_gray = rng.uniform(118, 178)
    field_ = _value_noise(rng, h, w, octaves=5)
    surface = base_gray + (field_ - field_.mean()) * rng.uniform(45, 85)

    # 골재 반점 — 소금-후추에 가까운 미세 입자
    speckle = rng.normal(0.0, rng.uniform(4.0, 9.0), (h, w)).astype(np.float32)
    surface += cv2.GaussianBlur(speckle, (0, 0), 0.7)

    # 습기 얼룩 / 오염 — 큰 스케일의 어두운 영역
    for _ in range(rng.integers(0, 4)):
        blob = np.zeros((h, w), np.float32)
        cx, cy = rng.integers(0, w), rng.integers(0, h)
        rx, ry = rng.integers(w // 10, w // 3), rng.integers(h // 10, h // 3)
        cv2.ellipse(blob, (int(cx), int(cy)), (int(rx), int(ry)),
                    float(rng.uniform(0, 180)), 0, 360, 1.0, -1)
        blob = cv2.GaussianBlur(blob, (0, 0), max(rx, ry) * 0.25)
        surface -= blob * rng.uniform(8, 26)

    # 거푸집 이음선 — 규칙적인 수평/수직 선
    if rng.random() < 0.45:
        gap = rng.integers(h // 5, h // 2)
        for y in range(int(rng.integers(0, gap)), h, int(gap)):
            surface[max(0, y - 1):y + 1, :] -= rng.uniform(6, 16)

    surface = np.clip(surface, 0, 255).astype(np.uint8)
    bgr = cv2.cvtColor(surface, cv2.COLOR_GRAY2BGR).astype(np.float32)
    # 콘크리트의 미세한 색편차
    bgr *= np.array([
        rng.uniform(0.97, 1.02), rng.uniform(0.98, 1.02), rng.uniform(0.98, 1.03)
    ], np.float32)
    return np.clip(bgr, 0, 255).astype(np.uint8)


# ─── 균열 ──────────────────────────────────────────────────────
def _crack_path(
    rng: np.random.Generator, h: int, w: int, n_steps: int, start: tuple[float, float],
    heading: float,
) -> list[tuple[float, float]]:
    """방향 지속성을 가진 랜덤워크 — 실제 균열의 사행(蛇行) 재현."""
    x, y = start
    pts = [(x, y)]
    for _ in range(n_steps):
        heading += rng.normal(0.0, 0.22)
        step = rng.uniform(3.0, 9.0)
        x += math.cos(heading) * step
        y += math.sin(heading) * step
        if not (0 <= x < w and 0 <= y < h):
            break
        pts.append((x, y))
    return pts


def draw_crack(
    rng: np.random.Generator,
    img: np.ndarray,
    mask: np.ndarray,
    mm_per_px: float,
    *,
    target_width_mm: float,
    branch_depth: int = 2,
) -> SynthDefect | None:
    """균열 1개(분기 포함)를 그리고 정답 라벨을 반환한다."""
    h, w = mask.shape[:2]
    peak_px = max(1.0, target_width_mm / max(mm_per_px, 1e-6))

    edge = rng.integers(0, 4)
    start = {
        0: (rng.uniform(0, w), 0.0),
        1: (rng.uniform(0, w), float(h - 1)),
        2: (0.0, rng.uniform(0, h)),
        3: (float(w - 1), rng.uniform(0, h)),
    }[int(edge)]
    heading = math.atan2(h / 2 - start[1], w / 2 - start[0]) + rng.normal(0, 0.5)

    widths: list[float] = []
    polyline: list[tuple[int, int]] = []
    darkness = rng.uniform(45, 95)

    def _stroke(pts: list[tuple[float, float]], scale: float) -> None:
        """경로를 따라 폭이 변하는 선을 그린다 (중앙부 최대, 끝단 수렴)."""
        n = len(pts)
        if n < 2:
            return
        for i in range(n - 1):
            t = i / max(n - 2, 1)
            # 중앙이 넓고 양끝으로 갈수록 좁아지는 테이퍼
            taper = math.sin(math.pi * min(max(t, 0.02), 0.98)) ** 0.55
            wpx = max(1.0, peak_px * scale * taper * rng.uniform(0.85, 1.15))
            widths.append(wpx)
            p0 = (int(round(pts[i][0])), int(round(pts[i][1])))
            p1 = (int(round(pts[i + 1][0])), int(round(pts[i + 1][1])))
            cv2.line(mask, p0, p1, 255, max(1, int(round(wpx))), cv2.LINE_AA)
            if i % 4 == 0:
                polyline.append(p0)

    main = _crack_path(rng, h, w, int(rng.integers(40, 130)), start, heading)
    if len(main) < 8:
        return None
    _stroke(main, 1.0)

    # 분기 — 주균열 중간 지점에서 가늘게 갈라진다
    for _ in range(int(rng.integers(0, branch_depth + 1))):
        if len(main) < 20:
            break
        idx = int(rng.integers(len(main) // 5, len(main) * 4 // 5))
        bh = math.atan2(
            main[min(idx + 1, len(main) - 1)][1] - main[idx][1],
            main[min(idx + 1, len(main) - 1)][0] - main[idx][0],
        ) + rng.choice([-1, 1]) * rng.uniform(0.4, 1.1)
        branch = _crack_path(rng, h, w, int(rng.integers(12, 45)), main[idx], bh)
        _stroke(branch, rng.uniform(0.35, 0.7))

    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None

    # 균열 내부는 어둡고 가장자리는 부드럽게 번진다
    soft = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), 0.8)
    img -= (soft[..., None] * darkness).astype(np.float32)

    # 폭·길이는 최종 마스크에서 measure_mask() 로 다시 잰다. 여기서는 형상만 넘긴다.
    del widths
    return SynthDefect(
        defect_type="crack",
        bbox=(int(xs.min()), int(ys.min()),
              int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)),
        area_ratio=float(len(xs)) / float(h * w),
        polyline=polyline[:40],
    )


def measure_mask(mask: np.ndarray, mm_per_px: float) -> dict | None:
    """마스크에서 균열폭·길이를 측정한다.

    검출기(`app.services.vision`)와 동일한 정의:
      폭 = 2 x (거리변환 능선점의 값) = 최대내접원 지름
      길이 = 능선 화소 수 x 1.08 (대각 연결 보정)
    """
    binary = (mask > 0).astype(np.uint8)
    if binary.sum() == 0:
        return None
    dist = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
    dilated = cv2.dilate(dist, np.ones((3, 3), np.uint8))
    ridge = (dist >= dilated - 1e-6) & (binary > 0) & (dist > 0.5)
    if not ridge.any():
        return None

    widths = 2.0 * dist[ridge] * mm_per_px
    ys, xs = np.nonzero(binary)

    # 중심선 폴리라인 — 기하변환 이후 좌표계에서 다시 뽑는다.
    ridge_pts = np.argwhere(ridge)
    if len(ridge_pts) >= 2:
        centered = ridge_pts.astype(np.float32) - ridge_pts.mean(axis=0)
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        ordered = ridge_pts[np.argsort(centered @ vt[0])]
        step = max(1, len(ordered) // 40)
        polyline = [(int(p[1]), int(p[0])) for p in ordered[::step]]
    else:
        polyline = [(int(p[1]), int(p[0])) for p in ridge_pts]

    return {
        "polyline": polyline,
        "width_mm_p95": round(float(np.percentile(widths, 95)), 3),
        "width_mm_max": round(float(widths.max()), 3),
        "length_mm": round(float(np.count_nonzero(ridge)) * 1.08 * mm_per_px, 1),
        "bbox": (int(xs.min()), int(ys.min()),
                 int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)),
        "area_ratio": float(binary.sum()) / float(binary.size),
    }


# ─── 균열 외 결함 ──────────────────────────────────────────────
def draw_other_defect(
    rng: np.random.Generator, img: np.ndarray, mask: np.ndarray, kind: str
) -> SynthDefect:
    h, w = mask.shape[:2]
    cx, cy = int(rng.integers(w // 6, w * 5 // 6)), int(rng.integers(h // 6, h * 5 // 6))
    rx, ry = int(rng.integers(w // 22, w // 7)), int(rng.integers(h // 22, h // 7))
    blob = np.zeros((h, w), np.float32)
    cv2.ellipse(blob, (cx, cy), (rx, ry), float(rng.uniform(0, 180)), 0, 360, 1.0, -1)
    # 가장자리를 불규칙하게 — 실제 결함은 매끈한 타원이 아니다
    blob *= (_value_noise(rng, h, w, 3) > 0.42).astype(np.float32)
    blob = cv2.GaussianBlur(blob, (0, 0), 2.0)
    solid = blob > 0.35

    if kind == "spalling":
        # 박리 — 표면이 떨어져 나가 밝고 거친 면이 드러남 + 그림자 테두리
        img[solid] += rng.uniform(18, 40)
        edge = cv2.morphologyEx(
            solid.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((5, 5), np.uint8)
        ).astype(bool)
        img[edge] -= rng.uniform(30, 60)
    elif kind == "efflorescence":
        # 백태 — 흰 석회질 침전, 흘러내린 형상
        streak = cv2.GaussianBlur(blob, (0, 0), 1.0)
        streak = np.maximum.accumulate(streak, axis=0) * 0.75
        img += (streak[..., None] * rng.uniform(35, 70)).astype(np.float32)
    elif kind == "leakage":
        # 누수 — 아래로 번지는 어두운 젖은 자국
        wet = np.maximum.accumulate(blob, axis=0)
        wet = cv2.GaussianBlur(wet, (0, 0), 4.0)
        img -= (wet[..., None] * rng.uniform(22, 48)).astype(np.float32)
    elif kind == "rebar_exposure":
        # 철근노출 — 평행한 철근 + 녹물
        img[solid] -= rng.uniform(25, 45)
        pitch = int(rng.integers(9, 18))
        for off in range(-ry, ry, pitch):
            cv2.line(img, (cx - rx, cy + off), (cx + rx, cy + off),
                     (28.0, 42.0, 96.0), int(rng.integers(2, 5)), cv2.LINE_AA)
        rust = cv2.GaussianBlur(blob, (0, 0), 6.0)[..., None]
        img += rust * np.array([-18.0, 6.0, 34.0], np.float32)
    elif kind == "segregation":
        # 재료분리 — 굵은 골재만 노출된 거친 면
        coarse = (rng.random((h, w)) > 0.55).astype(np.float32)
        coarse = cv2.GaussianBlur(coarse, (0, 0), 1.2) * blob
        img += (coarse[..., None] * rng.uniform(30, 55)).astype(np.float32)
        img -= (blob[..., None] * rng.uniform(6, 14)).astype(np.float32)
    else:  # damage
        img[solid] -= rng.uniform(30, 55)

    mask[solid] = 255
    ys, xs = np.nonzero(solid)
    if len(xs) == 0:
        return SynthDefect(kind, (cx, cy, 1, 1), area_ratio=0.0)
    return SynthDefect(
        defect_type=kind,
        bbox=(int(xs.min()), int(ys.min()),
              int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)),
        area_ratio=float(len(xs)) / float(h * w),
    )


# ─── 드론 촬영 효과 ────────────────────────────────────────────
def apply_drone_effects(
    rng: np.random.Generator, img: np.ndarray, masks: list[np.ndarray]
) -> tuple[np.ndarray, list[np.ndarray]]:
    """원근·모션블러·비네팅·노출·노이즈를 적용한다(마스크에도 동일 기하변환)."""
    h, w = img.shape[:2]

    # 1) 원근 왜곡 — 벽면에 완전 수직으로 찍히는 경우는 드물다
    if rng.random() < 0.75:
        j = min(h, w) * rng.uniform(0.01, 0.06)
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        dst = src + rng.normal(0, j, src.shape).astype(np.float32)
        M = cv2.getPerspectiveTransform(src, dst)
        img = cv2.warpPerspective(img, M, (w, h), borderMode=cv2.BORDER_REFLECT)
        masks = [
            cv2.warpPerspective(m, M, (w, h), flags=cv2.INTER_NEAREST) for m in masks
        ]

    # 2) 모션블러 — 기체 이동/진동.
    # 실무에서는 흐린 프레임을 재촬영으로 걸러내므로 경미한 수준만 재현한다.
    if rng.random() < 0.40:
        k = int(rng.integers(3, 6))
        kern = np.zeros((k, k), np.float32)
        kern[k // 2, :] = 1.0 / k
        kern = cv2.warpAffine(
            kern, cv2.getRotationMatrix2D((k / 2 - 0.5, k / 2 - 0.5),
                                          float(rng.uniform(0, 180)), 1.0), (k, k)
        )
        s = kern.sum()
        if s > 1e-6:
            img = cv2.filter2D(img, -1, kern / s)

    img = img.astype(np.float32)

    # 3) 비네팅 — 렌즈 주변부 광량 저하
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    img *= (1.0 - rng.uniform(0.06, 0.26) * np.clip(r, 0, 1.4) ** 2)[..., None]

    # 4) 노출/화이트밸런스 변동
    img = img * rng.uniform(0.82, 1.18) + rng.uniform(-16, 16)

    # 5) 센서 노이즈 + JPEG 압축 아티팩트
    img += rng.normal(0, rng.uniform(1.5, 6.0), img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if rng.random() < 0.6:
        q = int(rng.integers(58, 94))
        ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if ok:
            img = cv2.imdecode(enc, cv2.IMREAD_COLOR)

    return img, masks


# ─── 표본 1건 생성 ─────────────────────────────────────────────
def generate_sample(
    seed: int,
    *,
    size: tuple[int, int] = (768, 768),
    mm_per_px_range: tuple[float, float] = (0.03, 0.55),
    max_cracks: int = 4,
    max_other: int = 3,
    clean_prob: float = 0.12,
) -> SynthSample:
    """재현 가능한 합성 표본 1건을 만든다 (seed 고정 시 항상 동일)."""
    rng = np.random.default_rng(seed)
    h, w = size
    # 근접 촬영(망원)일수록 GSD가 작다 — 로그균등이 실제 운용 분포에 가깝다
    lo, hi = mm_per_px_range
    mm_per_px = float(np.exp(rng.uniform(math.log(lo), math.log(hi))))

    img = concrete_surface(rng, h, w).astype(np.float32)
    crack_mask = np.zeros((h, w), np.uint8)
    defect_mask = np.zeros((h, w), np.uint8)
    defects: list[SynthDefect] = []
    singles: list[np.ndarray] = []

    is_clean = rng.random() < clean_prob
    if not is_clean:
        for _ in range(int(rng.integers(1, max_cracks + 1))):
            single = np.zeros((h, w), np.uint8)
            # 0.05~2.0mm — 판정 경계(0.1/0.2/0.3/1.0)를 모두 포함하도록 로그균등
            target = float(np.exp(rng.uniform(math.log(0.04), math.log(1.2))))
            d = draw_crack(rng, img, single, mm_per_px, target_width_mm=target)
            if d is not None:
                defects.append(d)
                singles.append(single)
                crack_mask = np.maximum(crack_mask, single)

        for _ in range(int(rng.integers(0, max_other + 1))):
            kind = str(rng.choice(DEFECT_TYPES[1:]))
            defects.append(draw_other_defect(rng, img, defect_mask, kind))

    img = np.clip(img, 0, 255).astype(np.uint8)
    warped = apply_drone_effects(rng, img, [crack_mask, defect_mask] + singles)
    img, all_masks = warped
    crack_mask, defect_mask = all_masks[0], all_masks[1]

    # 정답 폭/길이는 기하변환까지 끝난 최종 마스크에서 측정한다.
    crack_defects = [d for d in defects if d.defect_type == "crack"]
    surviving: list[SynthDefect] = []
    for d, m in zip(crack_defects, all_masks[2:]):
        stats = measure_mask(m, mm_per_px)
        if stats is None:
            continue
        d.width_mm_p95 = stats["width_mm_p95"]
        d.width_mm_max = stats["width_mm_max"]
        d.length_mm = stats["length_mm"]
        d.bbox = stats["bbox"]
        d.area_ratio = stats["area_ratio"]
        d.polyline = stats["polyline"]
        surviving.append(d)
    defects = surviving + [d for d in defects if d.defect_type != "crack"]

    altitude = float(rng.uniform(3.0, 45.0))
    return SynthSample(
        image=img,
        crack_mask=crack_mask,
        defect_mask=defect_mask,
        defects=defects,
        mm_per_px=round(mm_per_px, 5),
        meta={
            "seed": int(seed),
            "clean": bool(is_clean),
            "distance_m": round(altitude, 2),
            "gimbal_pitch_deg": round(float(rng.uniform(-12, 12)), 1),
            "size": [h, w],
            "n_cracks": sum(1 for d in defects if d.defect_type == "crack"),
            "n_defects": len(defects),
        },
    )


def write_sample(sample: SynthSample, out_dir: Path, index: int) -> dict:
    """표본을 이미지/마스크/JSON으로 저장하고 인덱스 레코드를 반환한다."""
    stem = f"{index:07d}"
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    (out_dir / "masks").mkdir(parents=True, exist_ok=True)
    (out_dir / "labels").mkdir(parents=True, exist_ok=True)

    cv2.imwrite(str(out_dir / "images" / f"{stem}.jpg"), sample.image,
                [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    cv2.imwrite(str(out_dir / "masks" / f"{stem}_crack.png"), sample.crack_mask)
    cv2.imwrite(str(out_dir / "masks" / f"{stem}_defect.png"), sample.defect_mask)

    record = {
        "id": stem,
        "image": f"images/{stem}.jpg",
        "crack_mask": f"masks/{stem}_crack.png",
        "defect_mask": f"masks/{stem}_defect.png",
        "mm_per_px": sample.mm_per_px,
        "meta": sample.meta,
        "defects": [asdict(d) for d in sample.defects],
    }
    (out_dir / "labels" / f"{stem}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return record
