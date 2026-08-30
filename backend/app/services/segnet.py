"""균열 분할망 정의 — 학습과 추론이 같은 구조를 공유한다.

구조를 별도 파일에 둔 이유는 하나다. 학습 스크립트와 추론 래퍼가 각자 모델을
정의하면 언젠가 어긋나고, 그때 나오는 것은 오류가 아니라 **조용히 틀린 마스크**다.

왜 작은 U-Net 인가
-------------------
- 폐쇄망 CPU 설치가 전제다. GPU 를 가정할 수 없다.
- 균열은 클래스가 하나뿐이고 형상이 단순하다. 깊은 백본이 주는 의미 표현이
  거의 쓸모없다. 필요한 것은 **가는 선을 원해상도로 되살리는 능력**이고,
  그건 스킵 연결이 담당한다.
- 파라미터가 적어야 합성 데이터로 학습했을 때 합성 특유의 무늬를 외우지 않는다.

base=16 기준 약 0.5M 파라미터. 256×256 타일 한 장에 CPU 4스레드로 수십 ms.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


def _block(cin: int, cout: int) -> nn.Sequential:
    """3×3 두 번 + BN + ReLU.

    BatchNorm 을 넣은 이유: 입력이 0~1 로 정규화된 회색조라 밝기 분포가
    촬영 조건마다 크게 흔들린다. 정규화 없이는 노출이 다른 사진에서 응답이
    통째로 밀린다.
    """
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
        nn.Conv2d(cout, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
    )


class UNetSmall(nn.Module):
    """4단 U-Net. 출력은 로짓 1채널 (시그모이드는 손실/추론에서 적용)."""

    def __init__(self, base: int = 16):
        super().__init__()
        b = base
        self.e1 = _block(1, b)
        self.e2 = _block(b, b * 2)
        self.e3 = _block(b * 2, b * 4)
        self.bott = _block(b * 4, b * 8)
        self.d3 = _block(b * 8 + b * 4, b * 4)
        self.d2 = _block(b * 4 + b * 2, b * 2)
        self.d1 = _block(b * 2 + b, b)
        self.out = nn.Conv2d(b, 1, 1)
        self.pool = nn.MaxPool2d(2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        e1 = self.e1(x)
        e2 = self.e2(self.pool(e1))
        e3 = self.e3(self.pool(e2))
        b = self.bott(self.pool(e3))

        d3 = self.d3(torch.cat([_up(b, e3), e3], 1))
        d2 = self.d2(torch.cat([_up(d3, e2), e2], 1))
        d1 = self.d1(torch.cat([_up(d2, e1), e1], 1))
        return self.out(d1)


def _up(x: torch.Tensor, ref: torch.Tensor) -> torch.Tensor:
    """참조 텐서 크기로 올린다.

    scale_factor=2 로 고정하지 않는 이유: 입력 변이 2의 배수가 아니면 풀링에서
    한 픽셀씩 잃어 스킵 연결과 크기가 어긋난다. 추론 타일은 256 고정이지만,
    학습 중 자른 조각까지 그렇다고 보장할 수 없다.
    """
    return F.interpolate(x, size=ref.shape[-2:], mode="bilinear", align_corners=False)


# ─── 손실 ──────────────────────────────────────────────────────
def dice_bce_loss(
    logits: torch.Tensor, target: torch.Tensor, pos_weight: float = 8.0
) -> torch.Tensor:
    """Dice + 가중 BCE.

    균열 픽셀은 전체의 0.1~1% 다. 순수 BCE 로 학습하면 전부 배경으로 예측하는
    해가 손실을 거의 최소화해서, 모델이 아무것도 검출하지 않는 상태로 수렴한다.
    - 양성 가중 BCE 로 놓친 균열의 대가를 키우고,
    - Dice 로 '겹친 비율'을 직접 최적화해 클래스 불균형에 둔감하게 만든다.

    둘 중 하나만으로는 부족하다. Dice 만 쓰면 초기에 기울기가 거의 0이라
    학습이 시작되지 않고, BCE 만 쓰면 위의 붕괴가 일어난다.
    """
    bce = F.binary_cross_entropy_with_logits(
        logits, target, pos_weight=torch.tensor(pos_weight, device=logits.device)
    )
    p = torch.sigmoid(logits)
    num = 2.0 * (p * target).sum(dim=(1, 2, 3)) + 1.0
    den = p.sum(dim=(1, 2, 3)) + target.sum(dim=(1, 2, 3)) + 1.0
    dice = 1.0 - (num / den).mean()
    return bce + dice
