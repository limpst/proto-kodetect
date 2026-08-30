"""회차 간 균열 자동 매칭 — "작년의 그 균열이 자랐는가".

왜 필요한가
-----------
시계열 분석(`services/timeseries.py`)은 같은 균열의 폭 이력을 전제로 한다.
그런데 지금까지 그 "같은 균열"을 사람이 손으로 이어야 했다. 회차마다 결함이
수십~수백 건이면 이 작업이 내업의 큰 몫이 되고, 잘못 이으면 진행 속도가
통째로 틀어진다.

무엇을 기준으로 같다고 하는가
-----------------------------
사진 좌표는 회차마다 다르다. 같은 벽을 찍어도 촬영 위치·각도·거리가 달라
픽셀 좌표를 직접 비교할 수 없다. 그래서 세 겹으로 좁힌다.

1. **위치 단위 일치** — 같은 사진 그룹 이름 + 같은 부재. 그룹 이름은 현장에서
   부재·위치로 정한 식별자이므로(예: `3F-S2-C3`) 회차가 달라도 같은 곳을 가리킨다.
2. **기하 유사** — 중심선을 mm 단위로 환산해 정규화한 뒤, 중심 위치·방향·
   길이를 비교한다. 픽셀이 아니라 실치수로 비교해야 촬영 거리 차이가 상쇄된다.
3. **폭 연속성** — 균열은 줄어들지 않는다. 폭이 크게 감소했다면 다른 균열일
   가능성이 높으므로 비용을 올린다(보수한 경우는 사람이 확인해야 한다).

한계를 분명히
-------------
이 매칭은 **보조**다. 사진 그룹 이름을 현장에서 일관되게 붙였다는 전제에
기대고, 정합(stitching)으로 공통 평면이 잡히기 전까지는 회전·이동을 완전히
보정하지 못한다. 그래서 결과에 신뢰도를 붙이고, 낮은 것은 사람이 확인하도록
`needs_review` 로 표시한다. 조용히 이어 붙이면 틀린 진행 속도가 판정에 들어간다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# 같은 균열로 보기 위한 상한. 실치수(mm) 기준이다.
MAX_CENTER_DIST_MM = 900.0      # 중심 간 거리
MAX_ANGLE_DEG = 25.0            # 주축 방향 차이. 균열은 회차 사이에 방향을 바꾸지 않는다
MAX_LENGTH_RATIO = 3.0          # 길이 비 (긴 쪽 / 짧은 쪽)
MIN_SCORE = 0.45                # 이 아래는 연결하지 않는다
REVIEW_SCORE = 0.82             # 이 아래는 사람이 확인해야 한다
#
# 임계를 조인 근거: 서로 다른 균열 2건과 4건을 같은 그룹에 넣고 돌렸을 때
# 각도차 30도인 쌍이 0.70 으로 통과했다. 잘못 이으면 진행 속도가 통째로
# 틀어지고 그 속도가 잔여수명 예측과 보수 우선순위에 그대로 들어간다.
# 놓친 연결은 사람이 이으면 되지만, 잘못된 연결은 발견되지 않는다.


@dataclass
class CrackShape:
    """매칭에 쓰는 균열의 기하 요약 — 실치수(mm) 좌표계."""

    defect_id: int
    member_code: str
    group_name: str
    center: tuple[float, float]
    angle_deg: float
    length_mm: float
    width_mm: float | None
    n_points: int

    @property
    def valid(self) -> bool:
        return self.n_points >= 2 and self.length_mm > 0


def parse_polyline(text: str) -> np.ndarray:
    """`"x,y;x,y;..."` → (N,2) 배열."""
    if not text:
        return np.zeros((0, 2), np.float32)
    pts = []
    for seg in text.split(";"):
        if not seg:
            continue
        try:
            x, y = seg.split(",")
            pts.append((float(x), float(y)))
        except ValueError:
            continue
    return np.array(pts, np.float32) if pts else np.zeros((0, 2), np.float32)


def shape_of(
    defect_id: int,
    polyline: str,
    *,
    member_code: str,
    group_name: str,
    mm_per_px: float | None,
    width_mm: float | None,
    length_mm: float | None,
) -> CrackShape:
    """결함 하나를 실치수 기하 요약으로 바꾼다.

    스케일이 없으면 픽셀을 그대로 쓴다. 그 경우 회차 간 촬영 거리가 다르면
    비교가 무너지므로, 호출부에서 신뢰도를 낮춰야 한다.
    """
    pts = parse_polyline(polyline)
    s = mm_per_px if (mm_per_px and mm_per_px > 0) else 1.0
    if len(pts) >= 2:
        mm = pts * s
        center = (float(mm[:, 0].mean()), float(mm[:, 1].mean()))
        centered = mm - np.array(center, np.float32)
        # 주축 각도 — 균열의 진행 방향
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        angle = math.degrees(math.atan2(float(vt[0][1]), float(vt[0][0]))) % 180.0
        span = float(np.linalg.norm(mm[-1] - mm[0]))
        length = length_mm if length_mm else span
    else:
        center, angle, length = (0.0, 0.0), 0.0, (length_mm or 0.0)

    return CrackShape(
        defect_id=defect_id,
        member_code=member_code,
        group_name=group_name,
        center=center,
        angle_deg=angle,
        length_mm=float(length or 0.0),
        width_mm=width_mm,
        n_points=len(pts),
    )


def _angle_diff(a: float, b: float) -> float:
    """0~180 주기의 각도 차이 (0~90)."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def similarity(prev: CrackShape, curr: CrackShape) -> tuple[float, dict]:
    """두 균열이 같은 것일 점수(0~1)와 근거."""
    if prev.member_code != curr.member_code:
        return 0.0, {"reason": "부재 불일치"}
    if prev.group_name != curr.group_name:
        return 0.0, {"reason": "위치(사진 그룹) 불일치"}

    dist = math.dist(prev.center, curr.center)
    ang = _angle_diff(prev.angle_deg, curr.angle_deg)
    lo, hi = sorted((max(prev.length_mm, 1e-3), max(curr.length_mm, 1e-3)))
    ratio = hi / lo

    if dist > MAX_CENTER_DIST_MM or ang > MAX_ANGLE_DEG or ratio > MAX_LENGTH_RATIO:
        return 0.0, {
            "reason": "기하 불일치",
            "center_mm": round(dist, 1),
            "angle_deg": round(ang, 1),
            "length_ratio": round(ratio, 2),
        }

    s_dist = 1.0 - dist / MAX_CENTER_DIST_MM
    s_ang = 1.0 - ang / MAX_ANGLE_DEG
    s_len = 1.0 - (ratio - 1.0) / (MAX_LENGTH_RATIO - 1.0)

    # 폭 연속성 — 균열은 스스로 줄지 않는다. 크게 줄었으면 다른 균열을 의심한다.
    s_width = 1.0
    shrink = None
    if prev.width_mm and curr.width_mm:
        shrink = (prev.width_mm - curr.width_mm) / max(prev.width_mm, 1e-6)
        if shrink > 0.35:
            s_width = max(0.0, 1.0 - (shrink - 0.35) / 0.65)

    score = 0.40 * s_dist + 0.25 * s_ang + 0.15 * s_len + 0.20 * s_width
    return round(float(score), 4), {
        "center_mm": round(dist, 1),
        "angle_deg": round(ang, 1),
        "length_ratio": round(ratio, 2),
        "width_shrink": None if shrink is None else round(shrink, 3),
    }


def match(
    prev: list[CrackShape], curr: list[CrackShape], *, min_score: float = MIN_SCORE
) -> tuple[list[dict], list[int], list[int]]:
    """두 회차의 균열을 1:1로 잇는다.

    점수 내림차순 탐욕 배정을 쓴다. 헝가리안이 이론적으로는 최적이지만,
    여기서는 후보가 이미 위치·부재로 좁혀져 충돌이 드물고, scipy 의존을
    늘리지 않는 편이 폐쇄망 배포에 유리하다.

    반환: (연결 목록, 사라진 prev id, 새로 생긴 curr id)
    """
    scored = []
    for i, a in enumerate(prev):
        if not a.valid:
            continue
        for j, b in enumerate(curr):
            if not b.valid:
                continue
            s, why = similarity(a, b)
            if s >= min_score:
                scored.append((s, i, j, why))
    scored.sort(key=lambda t: -t[0])

    used_p: set[int] = set()
    used_c: set[int] = set()
    links: list[dict] = []
    for s, i, j, why in scored:
        if i in used_p or j in used_c:
            continue
        used_p.add(i)
        used_c.add(j)
        links.append(
            {
                "prev_defect_id": prev[i].defect_id,
                "curr_defect_id": curr[j].defect_id,
                "score": s,
                "needs_review": s < REVIEW_SCORE,
                "evidence": why,
            }
        )

    gone = [p.defect_id for k, p in enumerate(prev) if k not in used_p and p.valid]
    new = [c.defect_id for k, c in enumerate(curr) if k not in used_c and c.valid]
    return links, gone, new
