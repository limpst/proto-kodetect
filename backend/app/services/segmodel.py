"""학습 기반 균열 후보 생성 — 고전 분할이 놓치는 것을 메운다.

무엇을 바꾸고 무엇을 그대로 두는가
-----------------------------------
현재 검출기의 병목은 **후보 생성**이다. 벤치마크 재현율 0.605 에서 놓친 균열들은
분류기가 걸러낸 것이 아니라 `_segment()` 단계에서 아예 후보로 만들어지지 않았다.
배경 대비가 낮으면 능선 응답과 암부 조건을 동시에 넘지 못한다. 뒤에 아무리 좋은
분류기를 붙여도 없는 후보를 살릴 수는 없다.

그래서 이 모델은 **후보 생성만** 대체한다. 그 뒤는 전부 그대로다.

    [학습 모델] → 마스크 ─┐
                          ├→ 조각 연결 → FWHM 폭측정 → PSF보정 → MAD → 분류기 → 등급
    [고전 분할] → 마스크 ─┘

폭 측정 체인을 건드리지 않는 이유가 있다. 그 체인은 이미 편향 −0.011mm 로
보정을 마쳤고, **법정 판정의 근거가 되는 숫자**를 낸다. 학습 모델이 폭까지
회귀하게 만들면 그 숫자가 왜 그렇게 나왔는지 설명할 수 없게 된다. 균열이
'어디 있는가'는 배우게 하고, '얼마나 넓은가'는 물리적으로 재는 편이 옳다.

두 마스크의 합집합을 쓴다. 모델은 재현율을 올리고, 정밀도는 하류의 형상
분류기가 지킨다. 모델 단독으로 바꾸면 고전 분할이 이미 잘 잡던 뚜렷한 균열까지
모델 성능에 인질로 잡힌다.

가중치가 없으면 조용히 고전 분할만 쓴다. 학습 결과물이 배포에 없더라도 시스템은
동작해야 한다 — 폐쇄망 설치가 전제인 제품이다.
"""

from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

# 학습 입력 타일 크기. 균열은 가늘고 길어 문맥이 필요하지만, CPU 추론이
# 전제라 무한정 키울 수 없다. 256이면 0.35mm/px 에서 약 9cm 폭을 본다.
TILE = 256
OVERLAP = 32          # 타일 경계에서 균열이 끊기지 않도록 겹쳐 추론한다
DEFAULT_THRESHOLD = 0.45

WEIGHTS_PATH = Path(__file__).resolve().parents[3] / "models" / "crack_seg.pt"

_lock = threading.Lock()
_cached: "CrackSegmenter | None" = None
_load_failed = False


class CrackSegmenter:
    """U-Net 계열 소형 분할기의 추론 래퍼.

    스레드 안전을 위해 추론에 락을 건다. FastAPI 워커가 여러 요청을 동시에
    처리할 때 같은 모듈을 공유하는데, torch 모듈은 재진입 안전이 보장되지 않는다.
    """

    def __init__(self, weights: Path, threshold: float = DEFAULT_THRESHOLD):
        import torch  # 지연 임포트 — torch 없이도 시스템이 켜져야 한다

        from .segnet import UNetSmall

        self.torch = torch
        self.threshold = threshold
        ckpt = torch.load(weights, map_location="cpu", weights_only=True)
        self.net = UNetSmall(base=ckpt.get("base", 16))
        self.net.load_state_dict(ckpt["state_dict"])
        self.net.eval()
        # 추론 스레드를 제한한다. 학습 때 CPU를 다 먹으면 웹 응답이 멈춘다.
        torch.set_num_threads(min(4, torch.get_num_threads()))
        self.meta = {k: v for k, v in ckpt.items() if k not in ("state_dict",)}
        self._infer_lock = threading.Lock()

    # ── 추론 ───────────────────────────────────────────────────
    def probability(self, gray: np.ndarray) -> np.ndarray:
        """균열 확률맵 (0~1, 입력과 같은 크기)."""
        torch = self.torch
        h, w = gray.shape[:2]
        step = TILE - OVERLAP

        # 타일 격자. 가장자리는 마지막 타일을 안쪽으로 당겨 맞춘다 — 패딩으로
        # 채우면 그 인공 경계가 능선처럼 보여 가짜 균열이 잡힌다.
        xs = list(range(0, max(1, w - TILE + 1), step)) or [0]
        ys = list(range(0, max(1, h - TILE + 1), step)) or [0]
        if xs[-1] + TILE < w:
            xs.append(max(0, w - TILE))
        if ys[-1] + TILE < h:
            ys.append(max(0, h - TILE))

        acc = np.zeros((h, w), np.float32)
        wgt = np.zeros((h, w), np.float32)
        blend = _blend_window(TILE)

        batch: list[np.ndarray] = []
        coords: list[tuple[int, int, int, int]] = []
        for y in ys:
            for x in xs:
                y1, x1 = min(y + TILE, h), min(x + TILE, w)
                patch = gray[y:y1, x:x1]
                if patch.shape != (TILE, TILE):
                    pad = np.zeros((TILE, TILE), patch.dtype)
                    pad[: patch.shape[0], : patch.shape[1]] = patch
                    patch = pad
                batch.append(patch)
                coords.append((y, x, y1, x1))

        with self._infer_lock, torch.no_grad():
            for i in range(0, len(batch), 8):
                chunk = np.stack(batch[i : i + 8]).astype(np.float32) / 255.0
                t = torch.from_numpy(chunk).unsqueeze(1)
                prob = torch.sigmoid(self.net(t)).squeeze(1).numpy()
                for p, (y, x, y1, x1) in zip(prob, coords[i : i + 8]):
                    ph, pw = y1 - y, x1 - x
                    acc[y:y1, x:x1] += p[:ph, :pw] * blend[:ph, :pw]
                    wgt[y:y1, x:x1] += blend[:ph, :pw]

        return acc / np.maximum(wgt, 1e-6)

    def mask(self, gray: np.ndarray, sensitivity: float = 1.0) -> np.ndarray:
        """이진 균열 마스크 (0/255).

        sensitivity 는 고전 경로와 같은 뜻으로 쓴다 — 1보다 크면 임계를 낮춰
        더 많이 잡는다. 두 경로가 같은 손잡이에 반응해야 사용자가 헷갈리지 않는다.
        """
        thr = float(np.clip(self.threshold / max(sensitivity, 1e-3), 0.05, 0.95))
        m = (self.probability(gray) >= thr).astype(np.uint8) * 255
        # 점 노이즈 제거. 균열은 선이라 1~2px 점은 정의상 균열이 아니다.
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < 6:
                m[lab == i] = 0
        return m


def _blend_window(size: int) -> np.ndarray:
    """타일 경계의 이음매를 없애는 가중창.

    균등 가중으로 겹쳐 더하면 경계에서 확률이 계단처럼 튀고, 그 자리에 마스크가
    끊긴다. 가장자리로 갈수록 0에 수렴하는 창을 곱해 부드럽게 잇는다.
    """
    r = np.hanning(size + 2)[1:-1].astype(np.float32)
    win = np.outer(r, r)
    return np.maximum(win, 1e-3)


# ─── 로딩 ──────────────────────────────────────────────────────
def get_segmenter(weights: Path | None = None) -> CrackSegmenter | None:
    """사용 가능하면 분할기를, 아니면 None 을 준다 (한 번만 시도한다).

    실패를 기억하는 이유: torch 가 없거나 가중치가 깨진 환경에서 요청마다
    임포트를 재시도하면 응답마다 수백 ms 를 버린다.
    """
    global _cached, _load_failed
    if _cached is not None:
        return _cached
    if _load_failed:
        return None
    with _lock:
        if _cached is not None:
            return _cached
        if _load_failed:
            return None
        path = weights or WEIGHTS_PATH
        if not path.exists():
            _load_failed = True
            return None
        try:
            _cached = CrackSegmenter(path)
        except Exception:
            # 학습 모델이 없다고 검출이 멈추면 안 된다. 고전 경로로 계속한다.
            _load_failed = True
            return None
        return _cached


def reset_cache() -> None:
    """학습 직후 새 가중치를 반영할 때 쓴다."""
    global _cached, _load_failed
    with _lock:
        _cached = None
        _load_failed = False
