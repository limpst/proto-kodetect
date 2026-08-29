"""일일 진행 보고 생성 — docs/DAILY_UPDATE_<yyyymmdd>.md + Slack 발송.

매일 한 번 돌려 그날의 상태를 하나의 문서로 남긴다. 사람이 손으로 쓰면 빠뜨리는
것들(격리 큐 건수, 기한 초과 처방, 적신호 발동)을 시스템이 직접 읽어 채운다.

사용
----
    python -m scripts.daily_update                      # 문서만 생성
    python -m scripts.daily_update --slack              # Slack 발송까지
    python -m scripts.daily_update --date 2026-08-30    # 날짜 지정

Slack 발송은 환경변수 `SLACK_WEBHOOK_URL` 이 있을 때만 동작한다.
없으면 문서만 만들고 그 사실을 알린다 — 조용히 건너뛰지 않는다.

발송 문안 규칙 (사내 공통)
--------------------------
파일·링크만 던지지 않는다. 항상 (1) 무엇이 몇 건인지 (2) 핵심 수치 요약
(3) 유의점 (4) 읽는 순서를 함께 붙인다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

API_BASE = os.environ.get("KODETECT_API", "http://127.0.0.1:8077")
API_USER = os.environ.get("KODETECT_USER", "admin")
API_PASSWORD = os.environ.get("KODETECT_PASSWORD", "admin")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")


# ─── 수집 ──────────────────────────────────────────────────────
def git_activity(since: date, until: date) -> list[dict]:
    """해당 기간의 커밋. 저장소가 아니거나 커밋이 없으면 빈 목록."""
    try:
        out = subprocess.run(
            [
                "git", "log",
                f"--since={since.isoformat()} 00:00:00",
                f"--until={until.isoformat()} 23:59:59",
                "--pretty=format:%h\x1f%s\x1f%an",
                "--shortstat",
            ],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []

    commits: list[dict] = []
    current: dict | None = None
    for line in out.stdout.splitlines():
        if "\x1f" in line:
            h, subject, author = line.split("\x1f")
            current = {"hash": h, "subject": subject, "author": author, "stat": ""}
            commits.append(current)
        elif line.strip() and current is not None:
            current["stat"] = line.strip()
    return commits


def _client() -> httpx.Client | None:
    """계산 커널 세션. 서버가 안 떠 있으면 None — 실패를 감추지 않는다."""
    try:
        c = httpx.Client(base_url=API_BASE, timeout=30)
        c.get("/healthz")
        r = c.post(
            "/api/auth/login",
            json={"username": API_USER, "password": API_PASSWORD},
        )
        if r.status_code not in (200, 401):
            return None
        return c
    except Exception:
        return None


def collect_platform(c: httpx.Client) -> dict:
    """운영 지표 — 시설물별 등급·BHI·적신호·처방 현황."""
    data: dict = {"buildings": [], "totals": {}}
    buildings = c.get("/api/buildings").json()

    tot = {
        "buildings": len(buildings),
        "inspections": 0,
        "defects": 0,
        "red_flags": 0,
        "p0": 0,
        "p1": 0,
        "overdue": 0,
        "escalated": 0,
    }

    for b in buildings:
        tot["inspections"] += b.get("inspection_count", 0)
        tot["defects"] += b.get("defect_count", 0)
        row = {
            "id": b["id"],
            "name": b["name"],
            "statutory_grade": b.get("latest_grade"),
            "bhi": None,
            "bhc_grade": None,
            "red_flags": [],
            "p0": 0,
            "p1": 0,
            "overdue": 0,
            "escalated": 0,
            "health_age_delta": None,
        }
        try:
            d = c.get(f"/api/bhc/{b['id']}").json()
            row["bhi"] = d["bhi"]
            row["bhc_grade"] = d["grade"]
            row["red_flags"] = [f["code"] for f in d["red_flags"]]
            row["p0"] = sum(1 for p in d["prescriptions"] if p["priority"] == "P0")
            row["p1"] = sum(1 for p in d["prescriptions"] if p["priority"] == "P1")
            row["health_age_delta"] = d["health_age"]["deviation"]
            tot["red_flags"] += len(d["red_flags"])
            tot["p0"] += row["p0"]
            tot["p1"] += row["p1"]
        except Exception:
            pass
        try:
            capa = c.get(f"/api/bhc/{b['id']}/capa").json()
            row["overdue"] = capa["metrics"]["overdue"]
            row["escalated"] = capa["metrics"]["escalated"]
            tot["overdue"] += row["overdue"]
            tot["escalated"] += row["escalated"]
        except Exception:
            pass
        data["buildings"].append(row)

    data["totals"] = tot
    return data


def read_benchmark() -> dict | None:
    p = DOCS / "benchmark_baseline.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ─── 문서 ──────────────────────────────────────────────────────
# 이 표식 아래는 사람이 쓴 영역이다. 재생성해도 그대로 보존한다.
MANUAL_MARKER = "<!-- ↓↓↓ 수기 기록 — 자동 생성이 덮어쓰지 않습니다 ↓↓↓ -->"


def carry_over_manual(path: Path) -> str:
    """기존 문서에서 수기 구간을 그대로 살려낸다.

    표식이 없는 옛 문서(전부 수기)는 통째로 수기 구간으로 본다. 자동화가
    사람의 기록을 지우는 일은 없어야 한다.
    """
    if not path.exists():
        return ""
    old = path.read_text(encoding="utf-8")
    if MANUAL_MARKER in old:
        return old.split(MANUAL_MARKER, 1)[1].lstrip("\n")
    return old.strip() + "\n"


def build_markdown(day: date, commits: list[dict], platform: dict | None,
                   bench: dict | None) -> str:
    L: list[str] = []
    A = L.append

    A(f"# 🗓️ DAILY UPDATE — {day:%Y-%m-%d} · KO-Detect")
    A("")
    A("> 자동 생성 문서입니다. 저장소 커밋 이력과 운영 API를 직접 읽어 채웁니다.")
    A(f"> 생성 시각 {datetime.now():%Y-%m-%d %H:%M} · `scripts/daily_update.py`")
    A("")
    A("---")
    A("")

    # 1. 오늘의 변경
    A("## 1. 오늘의 변경")
    A("")
    if commits:
        A(f"커밋 **{len(commits)}건**")
        A("")
        A("| 커밋 | 내용 | 변경량 |")
        A("|---|---|---|")
        for cm in commits:
            subject = cm["subject"].replace("|", "\\|")
            A(f"| `{cm['hash']}` | {subject} | {cm['stat'] or '—'} |")
    else:
        A("커밋 없음.")
    A("")

    # 2. 운영 현황
    A("## 2. 운영 현황")
    A("")
    if platform is None:
        A("> ⚠️ 계산 커널에 접속하지 못해 운영 지표를 수집하지 못했습니다.")
        A(f"> `{API_BASE}` 가 기동 중인지 확인하십시오.")
    else:
        t = platform["totals"]
        A(f"시설물 **{t['buildings']}동** · 점검 **{t['inspections']}회차** · "
          f"결함 **{t['defects']}건**")
        A("")
        A("| 시설물 | 법정등급 | BHI | 검진등급 | 적신호 | P0 | P1 | 기한초과 | 노화편차 |")
        A("|---|:---:|---:|:---:|---|---:|---:|---:|---:|")
        for b in platform["buildings"]:
            flags = " ".join(b["red_flags"]) or "—"
            bhi = f"{b['bhi']:.1f}" if b["bhi"] is not None else "—"
            dev = f"{b['health_age_delta']:+.1f}년" if b["health_age_delta"] is not None else "—"
            A(f"| {b['name']} | {b['statutory_grade'] or '—'} | {bhi} | "
              f"{b['bhc_grade'] or '—'} | {flags} | {b['p0']} | {b['p1']} | "
              f"{b['overdue']} | {dev} |")
        A("")

        # 주의가 필요한 항목만 따로 뽑는다 — 표를 훑지 않아도 보이게
        alerts: list[str] = []
        if t["p0"]:
            alerts.append(
                f"**P0(응급) 처방 {t['p0']}건** — 소견서 완성 여부와 무관하게 "
                "즉시 통보 대상입니다 (BHC-STD §9.3)"
            )
        if t["overdue"]:
            alerts.append(f"기한 초과 처방 **{t['overdue']}건** — 에스컬레이션 {t['escalated']}건")
        if t["red_flags"]:
            alerts.append(f"적신호 발동 **{t['red_flags']}건** — 종합등급이 강제 하향된 시설물이 있습니다")
        if alerts:
            A("### 조치가 필요한 항목")
            A("")
            for a in alerts:
                A(f"- {a}")
        else:
            A("조치가 시급한 항목 없음.")
    A("")

    # 3. 검출 성능
    A("## 3. 검출 엔진 성능")
    A("")
    if bench:
        d = bench["detection"]
        w = bench["width_mm"]
        g = bench["grade"]
        A("| 지표 | 값 | DoD |")
        A("|---|---:|---|")
        A(f"| 인스턴스 F1 | {d['f1']:.3f} | mAP@0.5 ≥ 0.70 |")
        A(f"| 정밀도 / 재현율 | {d['precision']:.3f} / {d['recall']:.3f} | P ≥ 0.80 / R ≥ 0.75 |")
        A(f"| 균열폭 MAE | {w['mae']:.3f} mm | ±0.3mm |")
        A(f"| 균열폭 편향 | {w['bias']:+.3f} mm | 0에 가깝게 |")
        A(f"| 상태등급 일치율 | {g['accuracy']:.3f} | — |")
        A("")
        if d["f1"] < 0.70:
            A("> ⚠️ **DoD 미달** — 고전 영상처리 베이스라인의 한계입니다. "
              "학습 모델(Mask R-CNN / Y-MaskNet) 전환이 유일한 P0 항목입니다.")
    else:
        A("벤치마크 기록이 없습니다. `python -m datagen.evaluate` 로 생성하십시오.")
    A("")

    # 4. 유의점
    A("## 4. 함께 밝힐 유의점")
    A("")
    A("- AI 판정은 **보조 참고자료**이며, 법적 효력을 가지려면 책임기술자의 확인·서명이 필요합니다")
    A("- 확산도 ρ의 분모(조사 동종 부재 수)는 **가정값**입니다. 표본계획 도입 전까지 유효합니다")
    A("- 상시 계측은 **시뮬레이션**이며 실계측기 데이터가 아닙니다")
    A("- 강화학습 환경의 열화 전이확률·비용은 **가정치**입니다")
    A("")

    # 5. 읽는 순서
    A("## 5. 읽는 순서")
    A("")
    A("1. 위 §2 표에서 **적신호·P0·기한초과** 열만 먼저 봅니다")
    A("2. `docs/DESIGN_STEP_BY_STEP.md` — 13단계 설계와 각 단계의 미달 항목")
    A("3. `docs/N8N_BACKEND.md` — 배관 계층의 안전장치")
    A("4. `README.md` §4 — 검출 엔진 파이프라인")
    A("")
    A(f"<sub>KO-Detect · 자동 생성 {datetime.now():%Y-%m-%d %H:%M}</sub>")
    A("")
    return "\n".join(L)


# ─── Slack ─────────────────────────────────────────────────────
def build_slack_text(day: date, commits: list[dict], platform: dict | None,
                     bench: dict | None, doc_path: Path) -> str:
    lines: list[str] = []
    A = lines.append

    A(f"*KO-Detect 진행 보고 — {day:%Y-%m-%d}*")
    A("")

    # (1) 무엇이 몇 건인지
    A(f"• 커밋 {len(commits)}건")
    if platform:
        t = platform["totals"]
        A(f"• 시설물 {t['buildings']}동 · 점검 {t['inspections']}회차 · 결함 {t['defects']}건")
        A(f"• 적신호 {t['red_flags']}건 · P0 {t['p0']}건 · P1 {t['p1']}건 · 기한초과 {t['overdue']}건")
    else:
        A("• 운영 지표 수집 실패 — 계산 커널 미기동")
    A("")

    # (2) 핵심 수치 요약
    if platform and platform["buildings"]:
        A("*시설물별 현황*")
        for b in platform["buildings"]:
            bhi = f"{b['bhi']:.1f}" if b["bhi"] is not None else "—"
            flags = f" ⚠️{','.join(b['red_flags'])}" if b["red_flags"] else ""
            A(f"  · {b['name']} — 법정 {b['statutory_grade'] or '—'} / "
              f"BHI {bhi} ({b['bhc_grade'] or '—'}){flags}")
        A("")

    if bench:
        d, w = bench["detection"], bench["width_mm"]
        A(f"*검출 엔진* F1 {d['f1']:.3f} · 폭 MAE {w['mae']:.3f}mm · 편향 {w['bias']:+.3f}mm")
        A("")

    # (3) 유의점
    A("*유의점*")
    if platform and platform["totals"]["p0"]:
        A(f"  · P0 처방 {platform['totals']['p0']}건은 소견서와 무관하게 즉시 통보 대상입니다")
    A("  · AI 판정은 보조 참고자료이며 책임기술자 확인·서명이 필요합니다")
    A("  · 계측 데이터는 시뮬레이션이며 실계측기가 아닙니다")
    A("")

    # (4) 읽는 순서
    A(f"*문서* `{doc_path.relative_to(ROOT).as_posix()}` — §2 적신호·P0 열부터 보십시오")
    return "\n".join(lines)


def post_slack(text: str) -> tuple[bool, str]:
    if not SLACK_WEBHOOK:
        return False, "SLACK_WEBHOOK_URL 미설정 — 발송을 건너뜁니다"
    try:
        r = httpx.post(SLACK_WEBHOOK, json={"text": text}, timeout=20)
        if r.status_code == 200:
            return True, "발송 완료"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, f"발송 실패: {exc}"


# ─── 진입점 ────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="일일 진행 보고 생성")
    p.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--slack", action="store_true", help="Slack 발송까지 수행")
    p.add_argument("--since-days", type=int, default=0,
                   help="커밋 수집 시작을 며칠 앞으로 (기본 당일만)")
    p.add_argument("--print", dest="show", action="store_true", help="본문 출력")
    a = p.parse_args(argv)

    day = date.fromisoformat(a.date) if a.date else date.today()
    since = day - timedelta(days=a.since_days)

    commits = git_activity(since, day)
    client = _client()
    platform = collect_platform(client) if client else None
    bench = read_benchmark()

    DOCS.mkdir(parents=True, exist_ok=True)
    out = DOCS / f"DAILY_UPDATE_{day:%Y%m%d}.md"
    md = build_markdown(day, commits, platform, bench)

    # 사람이 덧붙인 기록은 절대 덮어쓰지 않는다. 자동 구간만 갈아끼운다.
    md = md + "\n" + MANUAL_MARKER + "\n\n" + carry_over_manual(out)
    out.write_text(md, encoding="utf-8")

    print(f"생성: {out.relative_to(ROOT).as_posix()}  ({len(md)} chars)")
    print(f"  커밋 {len(commits)}건 · 운영지표 {'수집' if platform else '실패'}"
          f" · 벤치마크 {'있음' if bench else '없음'}")

    if a.show:
        print()
        print(md)

    if a.slack:
        text = build_slack_text(day, commits, platform, bench, out)
        ok, msg = post_slack(text)
        print(f"Slack: {msg}")
        if not ok and SLACK_WEBHOOK:
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
