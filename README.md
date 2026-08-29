# KO-Detect — 건축물 안전진단 통합 플랫폼

드론·현장 영상에서 **균열을 검출·정량화**하고, 시특법·KDS 기준으로 **상태등급을
판정**하며, 상시 계측으로 **건전성을 실시간 재평가(MTM)** 하고, 강화학습으로
**유지관리 조치를 추천**하는 프로토타입입니다.

> **AI 판정은 보조 참고자료입니다.** 법적 효력이 있는 안전진단 결과로 사용하려면
> 책임기술자(구조기술사 등)의 현장 확인과 서명이 반드시 필요합니다.

---

## 1. 로컬 실행

### 1-1. 준비

Python **3.11 이상** (3.12·3.13·3.14 확인). Node는 필요 없습니다 — 프론트엔드는
빌드 없는 정적 파일이고 FastAPI가 직접 서빙합니다.

```bash
git clone https://github.com/limpst/proto-kodetect.git
cd proto-kodetect

python -m venv .venv
```

가상환경 활성화 — 셸에 따라 다릅니다.

| 셸 | 명령 |
|---|---|
| PowerShell | `.\.venv\Scripts\Activate.ps1` |
| cmd | `.\.venv\Scripts\activate.bat` |
| Git Bash / WSL / macOS | `source .venv/bin/activate` (Windows는 `source .venv/Scripts/activate`) |

활성화하지 않고 인터프리터를 직접 지정해도 됩니다 —
Windows `.\.venv\Scripts\python.exe`, macOS/Linux `./.venv/bin/python`.

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` 는 **웹 서비스 구동에 필요한 것만** 담고 있습니다.
강화학습을 직접 돌리려면 `requirements-dev.txt` (torch 포함)를 쓰십시오.

### 1-2. 설정

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

`.env` 를 열어 최소 두 가지를 정합니다.

```env
AUTH_USER=admin
AUTH_PASSWORD=원하는-비밀번호
SESSION_SECRET=            # 비워도 로컬은 동작하지만, 재시작 시 로그인이 풀립니다
```

`SESSION_SECRET` 을 고정하려면:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

인증을 끄고 바로 화면을 보려면 `AUTH_ENABLED=0` 으로 두면 됩니다 (개발 전용).

### 1-3. 실행

```bash
python -m uvicorn app.main:app --app-dir backend --port 8077 --reload
```

`--app-dir backend` 가 `backend/` 를 import 경로에 넣습니다.
`--reload` 는 코드 수정 시 자동 재기동합니다(개발용, 배포에서는 빼십시오).

첫 기동에서 **테이블 생성과 시연 데이터 시딩이 자동으로 일어납니다.**
건축물 3동 · 점검 15회차 · 결함 121건 · 균열 추적 12건 · 계측 채널 27개.

→ <http://127.0.0.1:8077> · `.env` 에 넣은 계정으로 로그인

### 1-4. 화면 둘러보기 순서

1. **개요** — 시설물을 바꿔 보십시오. A동은 D등급, 옹벽은 C등급으로 이력이 다릅니다
2. **균열 분석** — `합성 표본으로 시연` 을 누르면 사진 없이도 검출 → 판정 → 저장 →
   종합등급 갱신까지 전 과정이 돕니다
3. **시계열 진행** — 좌측 균열을 클릭하면 진행 곡선과 허용폭 도달 예측이 바뀝니다
4. **실시간 계측** — 상단 표시등이 초록이면 WebSocket 연결됨. 지수가 1초마다 갱신됩니다
5. **3D 뷰** — 드래그로 회전, 휠로 확대. 센서 구체가 실시간 상태에 따라 색·크기가 바뀝니다
6. **판정서** — 브라우저 인쇄(Ctrl+P)로 PDF 저장

### 1-5. 데이터 초기화

```bash
rm kodetect.db          # Windows: del kodetect.db
```

다음 기동에서 시연 데이터가 다시 만들어집니다.

### 1-6. 부가 명령

| 명령 | 용도 |
|---|---|
| `python -m datagen.generate --count 20000 --out data/synth_v1 --workers 8` | 합성 드론 균열 데이터셋 대량 생성 (8워커 ~17장/초) |
| `python -m datagen.evaluate --data data/bench_test` | 검출기 벤치마크 (P/R/F1 · 폭 MAE · 등급 일치율) |
| `python -m datagen.fit_filter --data data/bench_train` | 오검출 분류기 학습 (`models/fp_filter.json`) |
| `python -m rl.train --episodes 600 --out models/rl_v1` | 유지관리 정책 강화학습 (`requirements-dev.txt` 필요) |
| `docker compose -f n8n/docker-compose.yml up -d` | n8n 백엔드 오케스트레이션 (<http://localhost:5678>) |
| `python -m scripts.daily_update --slack` | 일일 진행 보고 생성 + Slack 발송 |

### 1-7. 자주 걸리는 문제

| 증상 | 원인 · 조치 |
|---|---|
| `ModuleNotFoundError: No module named 'app'` | `--app-dir backend` 누락 |
| `ModuleNotFoundError: No module named 'rl'` | 저장소 루트에서 실행하십시오. 또는 `PYTHONPATH=backend:.` |
| 로그인이 계속 풀림 | `SESSION_SECRET` 미설정 — 프로세스마다 키가 새로 생성됨 |
| 포트 충돌 | `--port 8078` 등으로 변경 |
| 정책 화면이 "규칙 기반"으로 표시 | 정상. `models/rl_v1/` 이 없으면 규칙 기반으로 대체됩니다 |

---

## 2. Render 배포 매뉴얼

두 경로가 있습니다. 처음이면 **2-A(Blueprint)** 가 가장 안전합니다.
이미 New Web Service 화면에 들어와 계시면 **2-B** 의 표를 그대로 옮겨 적으십시오.

### 2-A. Blueprint — `render.yaml` 로 한 번에 (권장)

대시보드 → **New → Blueprint** → `limpst/proto-kodetect` 선택 → **Apply**

저장소의 `render.yaml` 이 웹 서비스와 Postgres를 함께 만들고, 빌드·시작 명령과
환경변수를 자동으로 채웁니다. 배포 후 대시보드에서 **`AUTH_PASSWORD` 하나만**
입력하면 끝입니다 (`sync: false` 로 두어 비밀번호가 저장소에 남지 않습니다).

API 키도 CLI 토큰도 필요 없습니다.

### 2-B. 수동 생성 — New Web Service 화면 입력값

#### ① 기본 필드

| 필드 | 넣을 값 |
|---|---|
| Source Code | `limpst / proto-kodetect` |
| Name | `proto-kodetect` |
| Language | **Python 3** |
| Branch | `main` |
| Region | **Oregon (US West)** — 기존 서비스와 같은 리전 |
| Root Directory | *(비움)* |
| Build Command | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir backend` |

> ⚠️ **Start Command는 반드시 바꾸십시오.** Render가 자동입력하는
> `gunicorn your_application.wsgi` 로는 뜨지 않습니다. 이 앱은 WSGI가 아니라
> **ASGI(FastAPI)** 이고, 실시간 계측에 **WebSocket** 을 씁니다.
>
> `--app-dir backend` 가 `backend/` 를 import 경로에 넣습니다. `datagen`·`rl` 은
> 저장소 루트에 있으므로 `PYTHONPATH=backend:.` 도 함께 넣습니다.

#### ② Compute 플랜

| 플랜 | 판단 |
|---|---|
| **Free** ($0 · 0.1 CPU · 512MB) | 시연·검토용. 아래 제약을 먼저 읽으십시오 |
| **Starter** ($7 · 0.5 CPU · 512MB) | 상시 가동 + 영구 디스크가 필요할 때 최소 선택 |
| Standard ($25 · 1 CPU · 2GB) | 4K 드론 원본을 실제로 분석시킬 때 |

**Free 플랜 제약 — 시연 전에 알고 계셔야 합니다**

1. **15분 무접속 시 정지.** 다음 접속은 콜드스타트 30~60초, 실시간 WebSocket이
   끊깁니다(화면이 3초 뒤 자동 재연결).
2. **영구 디스크 없음.** 업로드 원본·오버레이가 재배포 시 사라집니다.
   `STORAGE_DIR=/tmp/storage` 로 두십시오.
3. **512MB 메모리.** numpy+opencv 상주가 약 300MB입니다. 4000×3000 원본을
   그대로 올리면 검출 중 OOM으로 죽을 수 있습니다 — 1600px 이하로 리사이즈하거나
   Standard 이상을 쓰십시오.

#### ③ Environment Variables

**Add from .env** 를 눌러 아래를 통째로 붙여넣는 것이 가장 빠릅니다.

```env
PYTHON_VERSION=3.12.8
PYTHONPATH=backend:.
AUTH_ENABLED=1
AUTH_USER=admin
AUTH_PASSWORD=여기에-강한-비밀번호
STORAGE_DIR=/tmp/storage
WEB_CONCURRENCY=1
OMP_NUM_THREADS=1
```

그리고 **`SESSION_SECRET` 은 따로 추가하면서 값 옆의 `Generate` 버튼**을 누르십시오.

| 변수 | 왜 필요한가 |
|---|---|
| `SESSION_SECRET` | 세션 쿠키 HMAC 서명 키. **비워 두면 프로세스마다 새로 생성**되어 재배포·워커 증설 때마다 전원 로그아웃됩니다 |
| `PYTHON_VERSION` | 3.12 고정. numpy·opencv 휠이 모두 제공되어 빌드가 빠릅니다 |
| `PYTHONPATH` | `backend`(도메인 코드) + `.`(datagen·rl) 둘 다 필요 |
| `WEB_CONCURRENCY=1` | 512MB에서 워커를 늘리면 OOM. 늘리려면 `SESSION_SECRET` 고정이 선행 조건 |
| `STORAGE_DIR` | free는 `/tmp/storage`, 디스크를 붙였으면 `/var/data/storage` |
| `AUTH_PASSWORD` | **저장소에 넣지 마십시오.** 대시보드에서만 입력 |

#### ④ Advanced 설정

| 항목 | 값 | 이유 |
|---|---|---|
| **Health Check Path** | `/healthz` | 넣어야 Render가 앱이 실제로 살아났는지 확인합니다. 비우면 포트만 열려도 성공으로 봅니다 |
| **Auto-Deploy** | `On Commit` | `main` 푸시 시 자동 재배포 |
| **Pre-Deploy Command** | *(비움)* | 기동 시 `init_db()` 가 테이블을 만들고 비어 있으면 시연 데이터를 넣습니다. 별도 마이그레이션 단계가 없습니다 |
| **Build Filters** | *(비움)* | 문서만 고쳐도 재배포되는 게 싫으면 Ignored Paths에 `docs/**`, `n8n/**` |
| **Secret Files** | 선택 · 파일명 `.env` | 환경변수를 하나씩 넣는 대신 `.env` 전체를 올려도 됩니다. 앱이 `pydantic-settings` 로 루트의 `.env` 를 읽습니다 |
| **Persistent Disk** | 유료 전용 · Mount Path `/var/data` · 1GB | 붙였다면 `STORAGE_DIR=/var/data/storage` 로 변경 |
| **Maintenance Mode** | Off | — |

#### ⑤ 데이터베이스 (선택)

**넣지 않으면** SQLite가 임시 디스크에 만들어져 재시작마다 초기화됩니다.
시연용으로는 오히려 편합니다 — 매번 깨끗한 시연 데이터로 시작합니다.

**보존하려면** New → **PostgreSQL** (Oregon, free) 을 만들고 환경변수에 추가:

```
DATABASE_URL = <Postgres의 Internal Database URL>
```

Render가 주는 URL은 `postgres://...` 스킴인데 SQLAlchemy 2.x는 이를 받지 않습니다.
**앱이 `postgresql+psycopg://` 로 자동 정규화**하므로 그대로 붙여넣으면 됩니다
(`backend/app/config.py` 의 `sqlalchemy_url`).

### 2-C. 배포 후 확인

```bash
curl https://proto-kodetect.onrender.com/healthz
# {"ok":true,"app":"KO-Detect","version":"0.1.0"}
```

1. `/login` → `AUTH_USER` / `AUTH_PASSWORD` 로 로그인
2. 개요 화면에 시설물 3동(D·C·D)이 보이면 시딩까지 정상
3. 상단 우측 표시등이 **초록**이면 WebSocket 정상 (지수가 1초마다 갱신)
4. 균열 분석 → **합성 표본으로 시연** 으로 검출 파이프라인 전체 확인

### 2-D. 자주 걸리는 문제

| 증상 | 원인 · 조치 |
|---|---|
| 빌드는 되는데 기동 실패 | Start Command가 gunicorn 그대로 → uvicorn 명령으로 교체 |
| `ModuleNotFoundError: app` | `--app-dir backend` 누락 |
| `ModuleNotFoundError: rl` / `datagen` | `PYTHONPATH=backend:.` 누락 |
| 재배포마다 로그아웃 | `SESSION_SECRET` 미설정 |
| `Can't load plugin: sqlalchemy.dialects:postgres` | 구버전 코드. 최신은 자동 정규화 — pull 후 재배포 |
| 업로드 파일이 사라짐 | free 임시 디스크 → 유료 + 영구 디스크 |
| 첫 요청이 30~60초 | free 콜드스타트 → 유료 플랜은 상시 가동 |
| 검출 중 프로세스 종료 | 512MB OOM → 이미지 리사이즈 또는 Standard |

### 2-E. torch는 배포하지 않습니다

`requirements.txt` 에는 **torch가 없습니다.** 강화학습 *학습* 에만 필요하고
서비스 구동에는 불필요하기 때문입니다 (약 200MB — 512MB 환경에 치명적).

학습된 정책(`models/rl_v1/`)이 없으면 유지관리 정책 화면은 규칙 기반으로 자동
대체되고 나머지 기능은 전부 정상 동작합니다. 학습은 로컬에서 하십시오.

```bash
pip install -r requirements-dev.txt      # torch 포함
python -m rl.train --episodes 600 --out models/rl_v1
```

> 온프레미스·폐쇄망 배포는 저장소의 `Dockerfile` 을 쓰십시오.
> 더 자세한 내용은 **[docs/DEPLOY_RENDER.md](docs/DEPLOY_RENDER.md)**.

---

## 3. 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│  n8n  — 백엔드 오케스트레이션 (수집 · 스케줄 · 카탈로그 · 자동조사)  │
│  관리자가 대화로 워크플로우를 고치고, 백엔드 구조가 스스로 진화한다   │
└───────────────┬─────────────────────────────────────────────┘
                │ HTTP
┌───────────────▼─────────────────────────────────────────────┐
│  KO-Detect API (FastAPI)  — 도메인 판정 · 영상 추론 "계산 커널"  │
│  vision · grading · timeseries · sensors · policy · reports  │
└───────────────┬─────────────────────────────────────────────┘
                │
        ┌───────▼────────┐   ┌──────────────┐   ┌─────────────┐
        │ SQLite/Postgres │   │ datagen 합성 │   │ rl 정책학습 │
        └─────────────────┘   └──────────────┘   └─────────────┘
```

설계 원칙은 하나입니다 — **판정 로직과 배관(plumbing)을 분리한다.**
법령에 근거해 검증돼야 하는 판정 기준은 Python 코드로 고정하고, 자주 바뀌는
수집·스케줄·통합은 n8n 워크플로우로 둡니다. 그래야 관리자가 코드를 건드리지
않고도 백엔드를 바꿔 나갈 수 있습니다.

---

## 4. 균열 검출 엔진

`backend/app/services/vision.py`

1. CLAHE 국소 대비 보정
2. **다중 스케일 헤시안 능선 필터** — 선형 구조만 남긴다 (얼룩·골재 탈락)
3. **암부 영역 내 Otsu 적응 임계 + 히스테리시스** — 전역 분위수를 쓰면 뚜렷한
   균열 하나가 임계 예산을 독식해 나머지를 놓친다
4. **방향 인지 파편 연결** — 끝점의 접선이 서로를 향할 때만 잇는다.
   팽창(dilation)으로 묶으면 나란한 별개 균열이 뭉치고, 일직선으로 이어질
   파편은 간격이 조금만 넓어도 못 잇는다. 균열의 연속은 방향의 연속이다
5. 연결요소 분석 — 길이·세장비·충실도·국소대비·**사행도**로 오검출 제거
6. **중심선 법선 방향 밝기 프로파일의 FWHM** + **MAD 이상치 제거**로 폭 산출
   → 이진화 임계에 좌우되지 않고, 분기 교차점에서 튀는 값에도 무너지지 않는다
7. **촬영계 PSF 이차합 보정** — 흐림이 균열을 넓어 보이게 하는 계통오차 제거
8. **학습 오검출 분류기** — 13개 형상 특징으로 균열/오검출을 판별
9. GSD(mm/px) 적용 + **선명도 게이트**(흐리면 재촬영 권고)
10. **타일 기반 다중해상도** — 1600px 초과 원본은 겹치는 타일로 원해상도 처리

### 벤치마크 — 동일 테스트셋 120장 (학습에 쓰지 않은 표본)

| 지표 | 개선 전 | **개선 후** | DoD |
|---|---:|---:|---|
| 인스턴스 F1 | 0.499 | **0.689** | ≥ 0.70 (근접) |
| 정밀도 | 0.400 | **0.800** | ≥ 0.80 ✅ |
| 정밀도 (타 결함 오분류 제외) | 0.446 | **0.882** | — |
| 재현율 | 0.662 | 0.605 | ≥ 0.75 |
| 균열폭 MAE | 0.375 mm | **0.230 mm** | ±0.3mm ✅ |
| 균열폭 편향 | −0.279 mm | **−0.011 mm** | 0에 가깝게 ✅ |
| 상태등급 일치율 | 0.626 | **0.732** | — |

`data/bench_train` 200장으로 분류기를 학습하고, `data/bench_test` 120장으로만
평가했습니다. **학습과 평가에 같은 표본을 쓰지 않습니다.**

분류기 검증 성능 — AUC **0.958** (학습 0.979), 후보 757건(정 280 / 오 477).
계수는 도메인 지식과 대조 가능합니다.

| 특징 | 계수 | 해석 |
|---|---:|---|
| `orient_entropy` | +0.845 | 방향이 다양할수록 균열 (직선 줄눈은 낮다) |
| `log_area` · `log_length` | +0.81 · +0.74 | 크고 길수록 균열 |
| `solidity` · `fill_ratio` | −0.74 · −0.71 | 꽉 찬 덩어리는 얼룩 |

매칭은 bbox IoU가 아니라 **중심선 버퍼 겹침**(선형 구조 평가의 표준)을 씁니다.
화면을 가로지르는 균열들은 외접 사각형이 거의 같아 IoU로는 구분되지 않습니다.

> **한계** — 재현율 0.605로 DoD(0.75) 미달입니다. 놓친 균열의 대부분은 배경 대비가
> 낮아 분할 단계에서 후보 자체가 만들어지지 않은 것으로, 분류기로는 회복되지
> 않습니다. 학습 모델(Mask R-CNN / Y-MaskNet) 전환이 정공법이며,
> `CrackDetector.detect()` 인터페이스를 고정해 두었습니다.
>
> PSF 계수 2.2px는 합성 벤치마크로 보정한 값입니다. **1.8을 쓰면 등급 일치율이
> 0.793으로 더 높지만 +0.16mm 과대평가가 남습니다.** 안전 쪽으로 치우친 값이라
> 유혹적이지만 채택하지 않았습니다 — 계측기는 편향이 없어야 하고, 보수적 여유는
> 측정값이 아니라 판정 기준이 갖습니다. 현장 배포 전 균열 게이지 대조 촬영으로
> 재보정해야 합니다.

---

## 5. 판정 기준

`backend/app/domain.py` · `backend/app/grading.py` — 기준이 개정되면 이 두 파일만 고칩니다.

- **허용균열폭** (KDS 14 20 30): 건조 0.40 / 습윤 0.30 / 부식성 0.20 / 고부식성 0.10 mm
- **균열폭 상태등급**: a <0.1 · b 0.1~0.2 · c 0.2~0.3 · d 0.3~1.0 · e ≥1.0 mm
- **결함 7종**: 균열 · 박리박락 · 백태 · 누수 · 철근노출 · 재료분리 · 손상
- **부재 가중치**: 주요부재(기둥·보·슬래브·전단벽·기초) > 보조부재
- **종합 안전등급 A~E**: 가중평균 결함도에 **주요부재 최악값을 하한**으로 걸어,
  보조부재 다수가 주요부재 심각결함을 가리지 못하게 합니다.

---

## 6. 건강검진 — BHC-STD-2026

`backend/app/bhc.py` · `opinion.py` — 법정 안전등급은 이산적이라 "C에서 C로" 머무는
동안 진행된 악화를 드러내지 못합니다. 본 표준은 그 산출물을 입력으로 받아
**연속 건강지수 · 건강나이 · 폐루프 사후관리**를 부가합니다. 법정 등급을 대체하지
않고 나란히 표기합니다.

| 조항 | 구현 |
|---|---|
| §4.2 | 6대 계통 S1~S6 · 가중치 하한(S1 0.30 / S4 0.20) 검증 |
| §8.1 | 심각도 D1~D5 + 노출환경별 보정 (허용균열폭 비로 환산) |
| §8.2 | 확산도 `s' = 100−(100−s)(0.5+0.5ρ)` · **D5 제외** |
| §8.3 | 부재 중요도 가중평균 · D5 있으면 계통 상한 30점 · 미실시 65점 |
| §8.4~5 | BHI 가중합 → **적신호 RF-1~RF-8** 강제 적용 (복수 시 최저 상한) |
| §8.7~8 | 건강나이 BHA · 노화편차 Δ · 열화속도 v |
| §9.2~3 | 소견 3요소 분리 · 금지표현 검사 · 처방 P0~P4 기한 |
| §10 | CAPA 6상태 · 에스컬레이션 E1~E4 |

**적신호가 실제로 작동합니다** — 서울 A동은 가중합 BHI 74.7점(C 상당)이었으나
D5 결함 15건으로 RF-1이 발동해 **39.9점 E등급**으로 강제되었습니다.

**소견 문장을 LLM으로 만들지 않습니다.** 완료기준이 "환각 0건"인데 생성형 모델은
이를 보장할 수 없습니다. 측정값이 이미 구조화되어 있으므로 템플릿 조립이면 환각이
원리적으로 불가능하고, 관측→해석→권고 구조도 항상 표준을 지킵니다.

```
관측  3층 서측 기둥 C-7에 균열 · 최대폭 0.62mm(±0.02) · 동종 부재 25%에서 발현 관측.
해석  D4(위험)에 해당한다. 습윤환경의 허용균열폭 0.30mm(KDS 14 20 30)를 0.32mm 초과한다.
권고  P1(긴급). 2026-09-29까지 단면복구 및 방청처리, 시공 전 철근 부식도 확인.
```

> **자동으로 채울 수 없는 것은 채우지 않습니다.** RF-3~RF-6·RF-8(소방설비 기능정지,
> 피난동선 적치, 내력벽 제거)은 영상으로 판정할 수 없어 수동 입력으로 둡니다.
> 확산도 ρ의 분모도 데이터가 없어 가정값을 쓰고, API 응답의 `assumptions`에 명시합니다.

---

## 7. 시계열 — 균열 진행과 실시간 MTM

`backend/app/services/timeseries.py`

**점검 회차 축** — 같은 균열(`CrackTrack`)의 폭 이력에 선형/멱함수를 적합해
잔차가 작은 쪽을 채택하고, 허용균열폭 도달 시점을 외삽합니다. 이것이 보수
우선순위의 근거가 됩니다.

**상시 계측 축** — IoT 채널 값을 즉시 건전성 지수로 환산해 계속 재평가합니다.
금융의 Mark-to-Market과 같은 개념으로, "지금 이 순간의 구조 건전성"을 하나의
수치로 계속 다시 매깁니다. 점검(수개월 주기)과 계측(초 단위) 두 정보원을
가중 결합하되, **최악 채널이 지배**하도록 해 단일 이상을 놓치지 않습니다.

화면에서는 TradingView `lightweight-charts` 캔들로 표시합니다 — 구간 내
고가·저가 폭이 곧 구조 응답의 진폭이므로 선보다 정보량이 많습니다.

---

## 8. 계측 시뮬레이션

`backend/app/services/sensors.py` — 실계측기 연결 전까지 대시보드·경보·지수를
실제와 같은 파형으로 구동합니다. 시드를 고정하면 항상 같은 파형이 나오므로
시연과 회귀 테스트가 재현됩니다.

| 채널 | 재현한 물리 성분 |
|---|---|
| 균열게이지 | 장기 진행 + 일일 온도 신축(위상 반전) + 계측 잡음 |
| 경사계 | 평균회귀 + 완만한 부등침하 추세 |
| 진동 | 배경 잡음 + 간헐적 버스트(장비·교통) + 주간 상승 |
| 침하계 | 압밀 곡선(√t 수렴) |
| 변형률 | 열변형 + 활하중 변동 |
| 온·습도 | 일주기 정현파 |

---

## 9. 강화학습 — 유지관리 정책

`rl/` — 예산 제약 하의 점검·보수 의사결정을 **POMDP**로 모델링합니다.
부재의 실제 열화 등급은 볼 수 없고, 점검해야만 잡음 섞인 관측을 얻습니다.

**계층 구조**
- Manager (3년 주기): 예산 기조 — 관찰 / 예방보수 / 집중보수 / 긴급대응
- Worker (매년, 부재별): 무조치 / 점검 / 표면보수 / 단면보수·보강 / 교체

**적용 기법**
- **Branching Dueling** — 부재 N개 × 5행동의 결합 공간(5^N)을 선형(N×5)으로 축소
- **분포형 C51 + CVaR 행동선택** — 안전 문제에서는 기댓값이 아니라 꼬리가
  중요하다. 수익 분포를 학습하고 하위 35% 조건부 기댓값을 최대화한다
- **NoisyNet** — 예산 제약 장기 문제에서 ε-greedy보다 일관된 탐색
- **PER + n-step** — 드물게 발생하는 심각 열화 전이를 우선 학습

기준정책(사후보수 / 정기보수)과 동일 조건으로 비교해 `models/rl_v1/report.json`에
기록하고, 웹 화면이 그대로 읽어 표시합니다.

---

## 10. 합성 데이터 생성

`datagen/synth.py` — 실촬영 데이터가 수백 장일 때 수만 장 규모의 사전학습
세트를 절차적으로 만듭니다. 모든 표본이 픽셀 마스크와 **정답 균열폭(mm)** 을
함께 갖습니다.

- 콘크리트 표면 — 다중 옥타브 값잡음 + 골재 반점 + 습기 얼룩 + 거푸집 이음선
- 균열 — 분기하는 랜덤워크, 경로를 따라 변하는 폭(중앙 최대·끝단 수렴)
- 부가 결함 6종 — 박리·백태·누수·철근노출·재료분리·손상
- 드론 효과 — 원근 왜곡 · 모션블러 · 비네팅 · 노출 변동 · 센서 노이즈 · JPEG

정답 폭은 **최종 렌더링된 마스크**에서 검출기와 동일한 정의로 측정합니다.
그리기 단계의 명목 두께를 쓰면 안티에일리어싱·분기 중첩·원근 왜곡 때문에
실제 화소상의 폭과 어긋나, 오차가 알고리즘 성능을 반영하지 못합니다.

---

## 11. n8n 백엔드 — 스스로 진화하는 구조

`n8n/` — 수집·스케줄·카탈로그·자동조사를 **코드가 아니라 워크플로우**로 둡니다.

상세 설계와 구현 절차는 **[docs/N8N_BACKEND.md](docs/N8N_BACKEND.md)** 에 있습니다 —
n8n Public REST API 레퍼런스, MCP 3가지 연결 패턴 비교, Step 0~10 구현 가이드,
감사 로그 스키마, 실패 모드 대응표.

| 워크플로우 | 트리거 | 역할 |
|---|---|---|
| `01_data_collection` | 15분 + 웹훅 | 수집 → 메타 파싱 → 검출 API → 품질 분류 → 카탈로그 |
| `02_admin_agent` | 채팅 | 관리자 자연어 조회·수정. n8n 자기수정 API를 도구로 보유 |
| `03_auto_research` | 주 1회 | 약점 탐지 → 조사 → **사람 승인 대기**로 제안 등록 |
| `04_capa_watchdog` | 매일 08:07 | 처방 기한 감시 → E1~E4 에스컬레이션 Slack 통보 |
| `05_daily_progress` | 평일 18:07 | 진행 요약 Slack 발송 + `DAILY_UPDATE_<yyyymmdd>.md` 생성 |

**자기진화의 안전장치 — 4중**

| 겹 | 장치 | 내용 |
|---|---|---|
| 1 | **권한 프록시** | n8n API 키를 에이전트에 직접 주지 않는다. 허용목록 통과분만 전달하며, `DELETE`·`/credentials`·`/users` 는 항상 차단. 감사 기록이 실패하면 전달하지 않는다 |
| 2 | **판정 기준 불변식** | 워크플로우 JSON을 정적 검사해 `/api/bhc/*` 쓰기, 허용균열폭·등급경계 하드코딩을 거부 |
| 3 | **사람 승인 대기** | 판정에 영향을 주는 제안은 `pending_review` 로만 등록. 자동 반영 없음 |
| 4 | **Git 버전관리** | n8n Source Control로 워크플로우를 커밋. 잘못된 자기수정은 되돌리기 한 번으로 원복 |

> 되돌리기 경로(4겹)가 확보되기 전에는 에이전트에게 수정 권한을 주지 않습니다.

안전이 걸린 시스템에서 자동화의 경계는 명확해야 합니다. **배관은 스스로 바꿔도,
판정 기준은 사람이 승인합니다.**

---

## 12. 웹 화면

| 화면 | 내용 |
|---|---|
| 개요 | 종합 안전등급 · 건전성 지수 추이 · 부재별 상태등급 · 점검 이력 |
| **건강검진** | BHI 게이지 · 6계통 레이더 · 적신호 배너 · CAPA 보드 · 소견 3요소 · 금지표현 검사 |
| 균열 분석 | 사진 업로드 또는 합성 표본 시연 → 검출 오버레이 · 폭 측정표 |
| 시계열 진행 | 균열별 진행 곡선 + 예측 + 허용폭선 · 도달 예상 시점 |
| 실시간 계측 | 채널 칩 · 건전성 MTM 캔들 · 경보 · 채널 시계열 (WebSocket 1초) |
| 3D 뷰 | 층별 등급 색상 + 센서 마커, 실시간 상태에 반응 (three.js) |
| 유지관리 정책 | 부재별 권장 조치 · 기준정책 대비 비교 · 학습 곡선 |
| 판정서 | 시설물 개요·종합판정·부재별·결함목록·적용기준 (인쇄로 PDF) |
| **건강소견서** | BHC-STD §9 5부 구성 — 1면 요약(단일 페이지 강제)·계통별·결함상세·처방(개략공사비)·부록 |

**디자인 원칙** — 수치가 주인공입니다. 모든 수치는 `tabular-nums` 고정폭에
최고 대비로 표시하고 **절대 말줄임하지 않습니다.** 공간이 부족하면 라벨을 줄입니다.

---

## 13. 구조

```
backend/app/        도메인 · 판정 · 서비스 · API
  domain.py         기준 상수 (법령·설계기준)
  grading.py        상태등급 · 안전등급 판정 엔진 (시특법)
  bhc.py            건강검진 판정 엔진 (BHC-STD-2026)
  opinion.py        소견 문장 생성 · 금지표현 검사
  services/         vision · timeseries · sensors
  routers/          auth · buildings · detect · live · policy · reports
frontend/           로그인 · 대시보드 (정적, FastAPI가 서빙)
datagen/            합성 데이터 생성 · 벤치마크
rl/                 POMDP 환경 · Branching C51 · PER · 학습
n8n/                docker-compose · 워크플로우 정의
scripts/            일일 보고 생성 등 운영 스크립트
docs/               설계 문서 · 배포 가이드 · 벤치마크 · 일일 기록
```

## 14. 로드맵

- [ ] 검출기 재현율 개선 — 분할 단계에서 놓치는 저대비 균열. 학습 모델 전환이 정공법
- [ ] 균열 게이지 대조 촬영으로 PSF 계수 현장 보정 (현재 2.2px는 합성 기준)
- [ ] 실계측기(로거) 연동 — 시뮬레이터를 어댑터 뒤로 이동
- [ ] KALIS-FMS · 건축물대장 연동 (n8n 워크플로우)
- [ ] 판정서 PDF 직접 출력 및 전자서명
