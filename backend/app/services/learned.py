"""학습 모델 기반 결함 검출기 — ONNX 추론.

고전 검출기(`vision.CrackDetector`)와 **같은 인터페이스**를 갖는다. 상위 계층은
어느 쪽이 도는지 알 필요가 없다.

왜 ONNX 인가
------------
서비스에 torch(약 200MB)를 올리지 않기 위해서다. onnxruntime 은 약 15MB 이고
CPU 추론 성능도 충분하다. 폐쇄망 온프레미스 설치에서 설치 용량은 그 자체로
제약이다.

무엇이 달라지는가
-----------------
고전 검출기는 균열 하나만 찾는다. 이 검출기는 **4종(균열·박리/박락·백태·
철근노출)** 을 구분한다 — PRO 명세의 탐지 항목이다. 나머지 3종(누수·재료분리·
손상)도 학습돼 있으나, 실촬영 검증 전까지는 신뢰도가 낮아 기본에서 제외한다.

폭 측정은 바뀌지 않는다
-----------------------
모델은 **어디가 균열인지**만 알려준다. 폭은 여전히 중심선 법선 방향 FWHM 으로
잰다. 세그멘테이션 마스크의 두께를 폭으로 쓰면 모델의 임계값 성향이 그대로
치수 오차가 되기 때문이다. 측정은 물리에 맡기고 모델은 위치만 맡는다.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from .vision import (
    MIN_AREA_PX,
    CrackInstance,
    DetectionResult,
    _dark_field,
    _equalize,
    _fwhm_widths,
    _link_fragments,
    _polyline_from_component,
    _ridge_points,
    _ridge_response,
    _skeleton_length,
    deconvolve_width,
    sharpness_score,
)

log = logging.getLogger("kodetect.learned")

CLASS_NAMES = [
    "background", "crack", "spalling", "efflorescence",
    "leakage", "rebar_exposure", "segregation", "damage",
]
CRACK_CLASS = 1
# 실촬영 검증 전까지 신뢰할 수 있는 범위. PRO 명세의 탐지 4종과 같다.
AREA_CLASSES = ("spalling", "efflorescence", "rebar_exposure")


class LearnedDetector:
    """ONNX 세그멘테이션 + 물리 기반 폭 측정."""

    name = "unet-onnx"

    def __init__(
        self,
        model_path: str | Path,
        *,
        tile: int = 512,
        overlap: int = 64,
        min_length_px: int = 40,
        max_link_px: float = 24.0,
        psf_sigma_px: float = 2.2,
        min_sharpness: float = 45.0,
        min_area_ratio: float = 0.0004,
    ) -> None:
        import onnxruntime as ort  # 지연 import — 없으면 고전 검출기로 대체된다

        self.path = Path(model_path)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2      # 서비스와 CPU를 나눠 쓴다
        self.sess = ort.InferenceSession(
            str(self.path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.sess.get_inputs()[0].name
        self.tile = tile
        self.overlap = overlap
        self.min_length_px = min_length_px
        self.max_link_px = max_link_px
        self.psf_sigma_px = psf_sigma_px
        self.min_sharpness = min_sharpness
        self.min_area_ratio = min_area_ratio

    # ─── 추론 ─────────────────────────────────────────────
    def _infer_tiles(self, bgr: np.ndarray) -> np.ndarray:
        """타일 분할 추론 — 4000px 원본을 한 번에 넣으면 메모리가 터진다.

        타일 경계에서 결함이 끊기지 않도록 겹쳐 자르고, 확률을 누적 평균한다.
        """
        h, w = bgr.shape[:2]
        n_cls = self.sess.get_outputs()[0].shape[1]
        n_cls = n_cls if isinstance(n_cls, int) else len(CLASS_NAMES)

        acc = np.zeros((n_cls, h, w), np.float32)
        cnt = np.zeros((1, h, w), np.float32)
        step = max(64, self.tile - self.overlap)

        for y in range(0, max(h - self.overlap, 1), step):
            for x in range(0, max(w - self.overlap, 1), step):
                y1, x1 = min(y + self.tile, h), min(x + self.tile, w)
                y0, x0 = max(0, y1 - self.tile), max(0, x1 - self.tile)
                patch = bgr[y0:y1, x0:x1]
                ph, pw = patch.shape[:2]
                # 모델은 32의 배수를 기대한다(4단 다운샘플)
                py, px = (-ph) % 32, (-pw) % 32
                if py or px:
                    patch = cv2.copyMakeBorder(patch, 0, py, 0, px, cv2.BORDER_REFLECT)

                inp = (
                    np.ascontiguousarray(patch[:, :, ::-1])
                    .transpose(2, 0, 1)[None]
                    .astype(np.float32) / 255.0
                )
                logits = self.sess.run(None, {self.input_name: inp})[0][0]
                logits = logits[:, :ph, :pw]
                # softmax
                e = np.exp(logits - logits.max(axis=0, keepdims=True))
                probs = e / e.sum(axis=0, keepdims=True)

                acc[:, y0:y1, x0:x1] += probs
                cnt[:, y0:y1, x0:x1] += 1.0

        return acc / np.maximum(cnt, 1e-6)

    # ─── 검출 ─────────────────────────────────────────────
    def detect(
        self, image: np.ndarray, mm_per_px: float | None = None
    ) -> DetectionResult:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        h, w = gray.shape[:2]
        sharp = sharpness_score(gray)

        probs = self._infer_tiles(image if image.ndim == 3 else cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
        pred = probs.argmax(0).astype(np.uint8)

        crack_mask = (pred == CRACK_CLASS).astype(np.uint8) * 255
        cracks = self._crack_instances(gray, crack_mask, probs, mm_per_px)
        areas = self._area_instances(pred, probs, h * w)

        quality_ok = sharp >= self.min_sharpness
        return DetectionResult(
            cracks=cracks,
            mask=crack_mask,
            image_size=(h, w),
            crack_area_ratio=float(np.count_nonzero(crack_mask)) / float(h * w),
            mm_per_px=mm_per_px,
            sharpness=round(sharp, 1),
            quality_ok=quality_ok,
            quality_note=(
                ""
                if quality_ok
                else f"선명도 부족 (Laplacian var {sharp:.0f} < {self.min_sharpness:.0f}) "
                     "— 균열폭이 과대평가될 수 있어 재촬영을 권고합니다"
            ),
            area_defects=areas,
            detector=self.name,
        )

    def _crack_instances(
        self, gray: np.ndarray, mask: np.ndarray, probs: np.ndarray,
        mm_per_px: float | None,
    ) -> list[CrackInstance]:
        """모델이 준 균열 마스크에서 인스턴스와 폭을 뽑는다."""
        if not mask.any():
            return []

        eq = _equalize(gray)
        _, normal = _ridge_response(eq, scales=(1.0, 2.0, 3.5, 5.0))
        dark = _dark_field(eq)

        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        # 방향이 맞을 때만 파편을 잇는다 — 나란한 별개 균열이 붙지 않게
        n_labels, labels = _link_fragments(mask, dist, self.max_link_px)

        out: list[CrackInstance] = []
        for i in range(1, n_labels):
            comp = labels == i
            area = int(comp.sum())
            if area < MIN_AREA_PX:
                continue

            ys, xs = np.nonzero(comp)
            x, y = int(xs.min()), int(ys.min())
            cw, ch = int(xs.max() - x + 1), int(ys.max() - y + 1)

            comp_dist = np.where(comp, dist, 0.0)
            ridge = _ridge_points(comp_dist, comp.astype(np.uint8))
            length_px = _skeleton_length(ridge)
            if length_px < self.min_length_px:
                continue

            ridge_pts = np.argwhere(ridge)
            widths = _fwhm_widths(dark, ridge_pts, normal)
            if widths.size >= 3:
                widths = deconvolve_width(widths, self.psf_sigma_px)
            else:
                widths = 2.0 * comp_dist[ridge]
            if widths.size == 0:
                continue

            # 신뢰도는 모델의 균열 확률 평균이다. 형상 휴리스틱보다 정직하다.
            conf = float(probs[CRACK_CLASS][comp].mean())

            inst = CrackInstance(
                bbox=(x, y, cw, ch),
                length_px=round(float(length_px), 1),
                width_px_max=round(float(widths.max()), 2),
                width_px_p95=round(float(np.percentile(widths, 95)), 2),
                width_px_mean=round(float(widths.mean()), 2),
                area_px=area,
                elongation=round(max(cw, ch) / max(1, min(cw, ch)), 2),
                confidence=round(min(0.99, max(0.30, conf)), 3),
                polyline=_polyline_from_component(ridge_pts),
            )
            inst.apply_scale(mm_per_px)
            out.append(inst)

        out.sort(key=lambda c: -(c.width_px_p95 * c.length_px))
        return out

    def _area_instances(
        self, pred: np.ndarray, probs: np.ndarray, total_px: int
    ) -> list[dict]:
        """면적형 결함(박리·백태·철근노출)을 유형별로 집계한다."""
        out: list[dict] = []
        for name in AREA_CLASSES:
            cls = CLASS_NAMES.index(name)
            m = (pred == cls).astype(np.uint8)
            if not m.any():
                continue
            n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
            for i in range(1, n):
                x, y, cw, ch, area = stats[i]
                ratio = area / float(total_px)
                if ratio < self.min_area_ratio:
                    continue
                comp = labels == i
                out.append(
                    {
                        "defect_type": name,
                        "bbox": [int(x), int(y), int(cw), int(ch)],
                        "area_px": int(area),
                        "area_ratio": round(float(ratio), 6),
                        "confidence": round(float(probs[cls][comp].mean()), 3),
                    }
                )
        out.sort(key=lambda d: -d["area_ratio"])
        return out


# ─── 선택 로직 ────────────────────────────────────────────────
_CACHE: dict[str, object] = {}


def load_detector(model_path: str | Path | None, **kw):
    """학습 모델이 있으면 그것을, 없으면 고전 검출기를 준다.

    실패했을 때 예외를 올리지 않고 고전 검출기로 내려가는 이유는, 모델 파일이
    없다고 서비스 전체가 멎으면 안 되기 때문이다. 어느 쪽이 돌았는지는
    DetectionResult.detector 에 남는다.
    """
    from .vision import CrackDetector

    if not model_path:
        return CrackDetector(**kw)
    key = str(model_path)
    if key in _CACHE:
        return _CACHE[key]
    try:
        det = LearnedDetector(model_path)
        _CACHE[key] = det
        log.info("학습 모델 로드: %s", model_path)
        return det
    except Exception as exc:
        log.warning("학습 모델 로드 실패 (%s) — 고전 검출기로 대체합니다: %s", model_path, exc)
        return CrackDetector(**kw)
