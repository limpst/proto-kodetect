# Render 배포 가이드 — KO-Detect

두 가지 경로가 있습니다. **Blueprint(A)** 를 권합니다. 이미 New Web Service 화면에
들어와 계시면 **수동(B)** 으로 진행하고 아래 값을 그대로 넣으십시오.

---

## A. Blueprint — 저장소의 `render.yaml` 로 한 번에 (권장)

Render 대시보드 → **New → Blueprint** → `limpst/proto-kodetect` 선택 → **Apply**

`render.yaml` 이 웹 서비스와 Postgres를 함께 만들고, 빌드·시작 명령과 환경변수를
자동으로 채웁니다. 배포 후 대시보드에서 **`AUTH_PASSWORD` 하나만** 입력하면 됩니다
(`sync: false` 로 두어 저장소에 비밀번호가 남지 않습니다).

API 키·CLI 토큰이 필요 없습니다.

---

## B. 수동 생성 — New Web Service 화면 입력값

### B-1. 기본 필드

| 필드 | 값 |
|---|---|
| Source Code | `limpst / proto-kodetect` |
| Name | `proto-kodetect` |
| Language | **Python 3** |
| Branch | `main` |
| Region | **Oregon (US West)** — 기존 13개 서비스와 같은 리전 |
| Root Directory | *(비움)* |
| Build Command | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir backend` |

> **Start Command 는 자동입력된 `gunicorn your_application.wsgi` 를 반드시 지우고
> 위 값으로 바꾸십시오.** 이 앱은 WSGI가 아니라 ASGI(FastAPI)이고, WebSocket을
> 쓰기 때문에 gunicorn 단독으로는 뜨지 않습니다.

`--app-dir backend` 가 `backend/` 를 import 경로에 넣습니다. `datagen`·`rl` 은
저장소 루트에 있으므로 `PYTHONPATH=backend:.` 도 함께 넣습니다.

### B-2. Compute 플랜

| 플랜 | 판단 |
|---|---|
| **Free** ($0, 0.1 CPU / 512MB) | 시연·검토용으로 충분. 아래 제약을 먼저 읽으십시오 |
| **Starter** ($7, 0.5 CPU / 512MB) | 상시 가동 + 영구 디스크 필요 시 최소 선택 |
| Standard ($25, 1 CPU / 2GB) | 대형 원본(4K 드론 사진) 분석을 실제로 돌릴 때 |

**Free 플랜의 제약 — 시연 전에 알고 계셔야 합니다**

1. **15분 무접속 시 정지합니다.** 다음 접속은 콜드스타트로 30~60초 걸리고,
   실시간 계측 WebSocket이 끊깁니다. 화면의 재연결 로직이 3초 뒤 다시 붙습니다.
2. **영구 디스크가 없습니다.** 업로드한 원본과 오버레이가 재시작·재배포 시
   사라집니다. `STORAGE_DIR=/tmp/storage` 로 두고 쓰십시오.
3. **512MB 메모리.** numpy+opencv 상주가 약 300MB입니다. 4000×3000 원본을
   그대로 올리면 검출 중 OOM으로 죽을 수 있습니다. 1600px 이하로 리사이즈해
   올리거나 Standard 이상을 쓰십시오.
4. Postgres free 인스턴스는 **생성 30일 뒤 만료**됩니다.

### B-3. Environment Variables

화면의 **Add from .env** 를 누르고 아래를 통째로 붙여넣는 것이 가장 빠릅니다.

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

그리고 **`SESSION_SECRET` 은 따로 추가하면서 값 옆의 `Generate` 버튼을 누르십시오.**

| 변수 | 이유 |
|---|---|
| `SESSION_SECRET` | 세션 쿠키 HMAC 서명 키. **비워 두면 프로세스마다 새로 생성되어** 재시작·워커 증설 때마다 전원 로그아웃됩니다. 반드시 고정값으로 넣으십시오 |
| `PYTHON_VERSION` | 3.12로 고정. numpy·opencv 휠이 모두 제공되어 빌드가 빠릅니다 |
| `PYTHONPATH` | `backend`(도메인 코드) + `.`(datagen·rl) 둘 다 필요 |
| `WEB_CONCURRENCY=1` | 512MB에서 워커를 늘리면 OOM. 늘릴 거면 `SESSION_SECRET` 고정이 선행 조건 |
| `STORAGE_DIR` | free는 `/tmp/storage`, 디스크를 붙였으면 `/var/data/storage` |
| `AUTH_PASSWORD` | **저장소에 넣지 마십시오.** 대시보드에서만 입력 |

### B-4. Advanced 설정

화면 하단 **Advanced** 를 펼치면 나오는 항목들입니다.

| 항목 | 설정값 | 이유 |
|---|---|---|
| **Health Check Path** | `/healthz` | 이 값을 넣어야 Render가 앱이 실제로 살아났는지 확인하고 무중단 전환을 합니다. 비워 두면 포트만 열려도 성공으로 봅니다 |
| **Auto-Deploy** | `On Commit` (기본) | `main` 푸시 시 자동 재배포 |
| **Pre-Deploy Command** | *(비움)* | 앱 기동 시 `init_db()` 가 테이블을 만들고 비어 있으면 시연 데이터를 넣습니다. 별도 마이그레이션 단계가 없습니다 |
| **Build Filters** | *(비움)* | 필요하면 `Ignored Paths` 에 `docs/**`, `n8n/**` 를 넣어 문서 수정만으로 재배포되지 않게 할 수 있습니다 |
| **Secret Files** | 선택 — 파일명 `.env` | 환경변수를 하나씩 넣는 대신 `.env` 파일 전체를 올릴 수 있습니다. 앱이 `pydantic-settings` 로 저장소 루트의 `.env` 를 읽으므로 그대로 동작합니다 |
| **Persistent Disk** | 유료 플랜에서만 · Mount Path `/var/data`, 1GB | 붙였다면 `STORAGE_DIR=/var/data/storage` 로 변경 |
| **Docker Build Context / Dockerfile Path** | *(해당 없음)* | Python 런타임을 쓰는 경우 나타나지 않습니다. Docker로 가려면 Language를 Docker로 바꾸고 저장소의 `Dockerfile` 을 지정 |
| **Maintenance Mode** | Off | — |

### B-5. 데이터베이스 (선택)

**넣지 않으면** SQLite가 `/tmp` 에 만들어져 재시작마다 초기화됩니다.
시연용으로는 오히려 편합니다 — 매번 깨끗한 시연 데이터로 시작합니다.

**보존하려면** Render → New → **PostgreSQL** (Oregon, free) 를 만들고,
웹 서비스 환경변수에 추가하십시오.

```
DATABASE_URL = <Postgres의 Internal Database URL>
```

Render가 주는 URL은 `postgres://...` 스킴인데 SQLAlchemy 2.x는 이 스킴을 받지
않습니다. **앱이 `postgresql+psycopg://` 로 자동 정규화**하므로 그대로 붙여넣으면
됩니다 (`backend/app/config.py` 의 `sqlalchemy_url`).

---

## 배포 후 확인

```bash
curl https://proto-kodetect.onrender.com/healthz
# {"ok":true,"app":"KO-Detect","version":"0.1.0"}
```

1. `/login` 접속 → `AUTH_USER` / `AUTH_PASSWORD` 로 로그인
2. 개요 화면에 시설물 3동이 보이면 시딩까지 정상입니다
3. 상단 우측 표시등이 **초록**이면 WebSocket 연결 정상 (건전성 지수가 1초마다 갱신)
4. 균열 분석 → **합성 표본으로 시연** 버튼으로 검출 파이프라인 전체 확인

---

## 자주 걸리는 문제

| 증상 | 원인 | 조치 |
|---|---|---|
| 빌드는 되는데 기동 실패 | Start Command가 gunicorn 그대로 | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir backend` 로 교체 |
| `ModuleNotFoundError: app` | `--app-dir backend` 누락 | Start Command 확인 |
| `ModuleNotFoundError: rl` / `datagen` | `PYTHONPATH` 에 `.` 없음 | `PYTHONPATH=backend:.` |
| 재배포마다 로그아웃 | `SESSION_SECRET` 미설정 | 고정값 지정 |
| `Can't load plugin: sqlalchemy.dialects:postgres` | 구버전 URL 스킴 | 최신 코드는 자동 정규화. 코드가 오래됐으면 pull |
| 업로드 파일이 사라짐 | free 플랜 임시 디스크 | 유료 + 영구 디스크, `STORAGE_DIR` 변경 |
| 첫 요청이 30~60초 | free 플랜 콜드스타트 | 유료 플랜은 상시 가동 |
| 검출 중 프로세스 종료 | 512MB OOM | 이미지 리사이즈 또는 Standard 플랜 |

---

## torch는 배포하지 않습니다

`requirements.txt` 에는 **torch가 없습니다.** 강화학습 정책 *학습* 에만 필요하고,
서비스 구동에는 불필요하기 때문입니다 (약 200MB, 512MB 환경에 치명적).

학습된 정책 파일(`models/rl_v1/`)이 없으면 유지관리 정책 화면은 규칙 기반으로
자동 대체되고, 나머지 기능은 모두 정상 동작합니다. 학습은 로컬에서 하십시오.

```bash
pip install -r requirements-dev.txt      # torch 포함
python -m rl.train --episodes 600 --out models/rl_v1
```
