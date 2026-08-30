"""균열·결함 세그멘테이션 모델 — 경량 U-Net.

왜 U-Net 인가
-------------
균열은 폭이 1~수십 픽셀인 얇은 선형 구조다. 다운샘플만 반복하는 분류형
백본은 이 정보를 잃는다. U-Net 의 스킵 연결은 인코더의 고해상도 특징을
디코더로 그대로 넘겨, 얇은 구조의 위치를 원해상도로 복원한다.

왜 큰 모델을 쓰지 않는가
------------------------
1. 배포 환경이 폐쇄망 온프레미스다. 무거운 백본은 설치 부담이 된다.
2. 학습 데이터가 합성이다. 용량이 큰 모델은 합성 패턴을 외워 실사에서
   더 크게 무너진다. 도메인 격차가 있는 상황에서는 작은 모델이 안전하다.
3. ONNX 로 내보내 onnxruntime(약 15MB)으로 추론한다. torch(약 200MB)를
   서비스에 올리지 않기 위해서다.

클래스 불균형
-------------
균열 화소는 전체의 1% 미만이다. 가중치 없이 학습하면 전부 배경으로 예측하는
해가 손실을 가장 잘 낮춘다. 그래서 Dice 손실을 섞고 클래스 가중을 준다.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

# 0=배경, 1..7 = datagen.synth.DEFECT_TYPES 순서
NUM_CLASSES = 8
CLASS_NAMES = [
    "background", "crack", "spalling", "efflorescence",
    "leakage", "rebar_exposure", "segregation", "damage",
]


def _block(cin: int, cout: int) -> nn.Sequential:
    """Conv-BN-ReLU 두 겹. BN 을 쓰는 이유는 합성 표본의 노출 변동이 커서
    입력 분포가 배치마다 크게 흔들리기 때문이다."""
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
        nn.Conv2d(cout, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
    )


class UNet(nn.Module):
    """4단 U-Net. base=24 기준 약 1.9M 파라미터."""

    def __init__(self, num_classes: int = NUM_CLASSES, base: int = 24) -> None:
        super().__init__()
        b = base
        self.enc1 = _block(3, b)
        self.enc2 = _block(b, b * 2)
        self.enc3 = _block(b * 2, b * 4)
        self.enc4 = _block(b * 4, b * 8)
        self.pool = nn.MaxPool2d(2)

        self.bottom = _block(b * 8, b * 16)

        self.up4 = nn.ConvTranspose2d(b * 16, b * 8, 2, stride=2)
        self.dec4 = _block(b * 16, b * 8)
        self.up3 = nn.ConvTranspose2d(b * 8, b * 4, 2, stride=2)
        self.dec3 = _block(b * 8, b * 4)
        self.up2 = nn.ConvTranspose2d(b * 4, b * 2, 2, stride=2)
        self.dec2 = _block(b * 4, b * 2)
        self.up1 = nn.ConvTranspose2d(b * 2, b, 2, stride=2)
        self.dec1 = _block(b * 2, b)

        self.head = nn.Conv2d(b, num_classes, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        e1 = self.enc1(x)
        e2 = self.enc2(self.pool(e1))
        e3 = self.enc3(self.pool(e2))
        e4 = self.enc4(self.pool(e3))
        bt = self.bottom(self.pool(e4))

        d4 = self.dec4(torch.cat([self.up4(bt), e4], 1))
        d3 = self.dec3(torch.cat([self.up3(d4), e3], 1))
        d2 = self.dec2(torch.cat([self.up2(d3), e2], 1))
        d1 = self.dec1(torch.cat([self.up1(d2), e1], 1))
        return self.head(d1)


def dice_loss(logits: torch.Tensor, target: torch.Tensor, eps: float = 1.0) -> torch.Tensor:
    """다중 클래스 Dice.

    교차엔트로피만 쓰면 화소 수가 압도적인 배경이 손실을 지배한다. Dice 는
    클래스별 겹침 비율이라 희소 클래스도 같은 무게를 갖는다.
    """
    probs = F.softmax(logits, dim=1)
    onehot = F.one_hot(target, probs.shape[1]).permute(0, 3, 1, 2).float()
    dims = (0, 2, 3)
    inter = (probs * onehot).sum(dims)
    denom = probs.sum(dims) + onehot.sum(dims)
    # 표본에 없는 클래스는 평균에서 뺀다. 없는 것을 못 맞췄다고 벌하면 안 된다.
    present = onehot.sum(dims) > 0
    d = (2 * inter + eps) / (denom + eps)
    return 1.0 - d[present].mean() if present.any() else logits.sum() * 0.0


class SegLoss(nn.Module):
    """가중 교차엔트로피 + Dice."""

    def __init__(self, class_weights: torch.Tensor | None = None, dice_w: float = 0.5) -> None:
        super().__init__()
        self.ce = nn.CrossEntropyLoss(weight=class_weights)
        self.dice_w = dice_w

    def forward(self, logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        return self.ce(logits, target) + self.dice_w * dice_loss(logits, target)


@torch.no_grad()
def confusion_counts(
    logits: torch.Tensor, target: torch.Tensor, num_classes: int = NUM_CLASSES
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """클래스별 (교집합, 예측합, 정답합) — IoU 집계를 배치 간 누적하기 위함."""
    pred = logits.argmax(1)
    inter = torch.zeros(num_classes, device=logits.device)
    p_sum = torch.zeros(num_classes, device=logits.device)
    t_sum = torch.zeros(num_classes, device=logits.device)
    for c in range(num_classes):
        p = pred == c
        t = target == c
        inter[c] = (p & t).sum()
        p_sum[c] = p.sum()
        t_sum[c] = t.sum()
    return inter, p_sum, t_sum
