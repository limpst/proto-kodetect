"""시연용 초기 데이터 생성.

빈 데이터베이스에 건축물 3동, 각 4~6회차 점검, 균열 추적 이력, 계측 채널을
만든다. 균열 이력은 실제 진행 양상(초기 급진 후 완만)을 따르도록 생성하므로,
시계열 분석 화면이 곧바로 의미 있는 결과를 보여준다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import func, select

from .db import SessionLocal
from .domain import DefectType, Environment
from .grading import assess_defect, assess_inspection
from .models import Building, CrackTrack, Defect, Inspection, SensorChannel
from .services.sensors import CHANNEL_SPECS, default_channels

BUILDINGS = [
    dict(
        name="서울 공공임대 A동",
        address="서울특별시 강서구 마곡중앙로 100",
        facility_class="2종",
        structure_type="철근콘크리트 벽식",
        completed_year=1998,
        floors_above=15,
        floors_below=2,
        gross_area_m2=18400.0,
        environment=Environment.HUMID.value,
        latitude=37.5601,
        longitude=126.8265,
    ),
    dict(
        name="아산 도로 옹벽 3구간",
        address="충청남도 아산시 배방읍 희망로 55",
        facility_class="2종",
        structure_type="철근콘크리트 옹벽",
        completed_year=2005,
        floors_above=None,
        floors_below=None,
        gross_area_m2=2600.0,
        environment=Environment.CORROSIVE.value,
        latitude=36.7830,
        longitude=127.0040,
    ),
    dict(
        name="인천 물류창고 B",
        address="인천광역시 서구 정서진로 210",
        facility_class="3종",
        structure_type="PC 라멘조",
        completed_year=2012,
        floors_above=4,
        floors_below=1,
        gross_area_m2=31200.0,
        environment=Environment.HIGH_CORROSIVE.value,
        latitude=37.5460,
        longitude=126.6350,
    ),
]

# (라벨, 부재코드, 초기폭 mm, 연간 진행 mm, 위치설명)
TRACKS = [
    ("CR-A-01", "column", 0.14, 0.125, "1층 기둥 C3 하부 수직균열"),
    ("CR-A-02", "girder", 0.22, 0.030, "3층 큰보 G7 중앙부 휨균열"),
    ("CR-A-03", "wall_shear", 0.09, 0.012, "코어 전단벽 개구부 모서리"),
    ("CR-A-04", "slab", 0.31, 0.165, "지하 1층 슬래브 균열(누수 동반)"),
    ("CR-A-05", "parapet", 0.18, 0.008, "옥상 파라펫 수평균열"),
]

OTHER_DEFECTS = [
    (DefectType.EFFLORESCENCE, "wall_non", 0.030),
    (DefectType.LEAKAGE, "slab", 0.018),
    (DefectType.SPALLING, "girder", 0.012),
    (DefectType.REBAR_EXPOSURE, "column", 0.004),
]


def _crack_width(initial: float, rate: float, years: float, rng) -> float:
    """멱함수형 진행 + 측정 잡음. 초기에 빠르고 이후 완만해진다."""
    grown = initial + rate * (years**0.75)
    return round(max(0.02, grown + rng.normal(0, 0.012)), 3)


def seed_if_empty() -> None:
    with SessionLocal() as db:
        if db.scalar(select(func.count(Building.id))):
            return

        rng = np.random.default_rng(20260830)
        now = datetime.now()

        for bi, spec in enumerate(BUILDINGS):
            b = Building(**spec)
            db.add(b)
            db.flush()

            # 계측 채널
            for code, kind, member, pos in default_channels(b.id):
                unit, _, warn, crit = CHANNEL_SPECS[kind]
                db.add(
                    SensorChannel(
                        building_id=b.id,
                        code=code,
                        kind=kind,
                        member_code=member,
                        unit=unit,
                        warn_threshold=warn,
                        critical_threshold=crit,
                        position_x=pos[0],
                        position_y=pos[1],
                        position_z=pos[2],
                    )
                )

            # 균열 추적 대상 (건물마다 개수를 달리한다)
            n_tracks = [5, 3, 4][bi]
            tracks = []
            for label, member, w0, rate, note in TRACKS[:n_tracks]:
                t = CrackTrack(
                    building_id=b.id,
                    label=f"{label}-{bi + 1}",
                    member_code=member,
                    location_note=note,
                )
                db.add(t)
                db.flush()
                # 건물별로 열화 속도를 달리해 화면이 서로 다르게 보이도록
                tracks.append((t, w0 * (1.0 + 0.25 * bi), rate * (1.0 + 0.35 * bi)))

            # 점검 회차 — 2년 간격 5회 + 최근 1회
            n_insp = [6, 5, 4][bi]
            env = Environment(spec["environment"])
            for k in range(n_insp):
                years_ago = (n_insp - 1 - k) * 2.0
                at = now - timedelta(days=int(years_ago * 365.25))
                kind = "diagnosis" if k == n_insp - 1 else "precise" if k % 2 else "regular"
                insp = Inspection(
                    building_id=b.id,
                    kind=kind,
                    inspected_at=at,
                    inspector=["김재현", "강유정", "이창근"][bi],
                    notes="정기 점검 결과 기록",
                )
                db.add(insp)
                db.flush()

                grouped: dict[str, list] = {}
                elapsed = k * 2.0
                for t, w0, rate in tracks:
                    width = _crack_width(w0, rate, elapsed, rng)
                    a = assess_defect(
                        DefectType.CRACK, width_mm=width, environment=env
                    )
                    db.add(
                        Defect(
                            inspection_id=insp.id,
                            track_id=t.id,
                            defect_type=DefectType.CRACK.value,
                            member_code=t.member_code,
                            width_mm=width,
                            length_mm=round(float(rng.uniform(300, 2400)), 1),
                            grade=a.grade.value,
                            severity=a.severity,
                            repair_required=a.repair_required,
                            confidence=round(float(rng.uniform(0.62, 0.95)), 3),
                            basis=a.basis,
                        )
                    )
                    grouped.setdefault(t.member_code, []).append(a)

                # 균열 외 결함 — 회차가 진행될수록 면적률이 커진다.
                # 철근노출·박리는 열화가 상당히 진행된 뒤에야 나타난다.
                for dtype, member, ratio0 in OTHER_DEFECTS:
                    if dtype is DefectType.REBAR_EXPOSURE and k < n_insp - 2:
                        continue
                    if dtype is DefectType.SPALLING and k < 2:
                        continue
                    ratio = round(ratio0 * (1.0 + 0.28 * elapsed) * (1.0 + 0.4 * bi), 5)
                    a = assess_defect(dtype, area_ratio=ratio, environment=env)
                    db.add(
                        Defect(
                            inspection_id=insp.id,
                            defect_type=dtype.value,
                            member_code=member,
                            area_ratio=ratio,
                            grade=a.grade.value,
                            severity=a.severity,
                            repair_required=a.repair_required,
                            confidence=round(float(rng.uniform(0.55, 0.9)), 3),
                            basis=a.basis,
                        )
                    )
                    grouped.setdefault(member, []).append(a)

                result = assess_inspection(grouped)
                insp.safety_grade = result.safety_grade.value
                insp.defect_index = result.defect_index

        db.commit()
