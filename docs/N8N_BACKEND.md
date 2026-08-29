# n8n 백엔드 — 설계 및 Step-by-Step 구현 가이드

> KO-Detect의 **배관(수집 · 스케줄 · 카탈로그 · 자동조사 · 통보)** 을 코드가 아니라
> n8n 워크플로우로 두는 구조의 상세 설계입니다. 관리자가 자연어로 백엔드를 고칠 수
> 있고, 백엔드가 스스로 구조를 바꾸되, **판정 기준만은 사람이 승인**합니다.
>
> 근거: n8n 공식 문서 (Public REST API · MCP Server Trigger · MCP Client Tool ·
> 내장 MCP 서버), BHC-STD-2026 §10 CAPA

---

## 0. 경계선 — 무엇을 워크플로우로 두고 무엇을 코드로 두는가

이 구분이 이 문서 전체의 전제입니다. 틀리면 안전이 걸린 판정이 대화로 바뀝니다.

| | 코드 (Python) | 워크플로우 (n8n) |
|---|---|---|
| **무엇** | 판정 기준 · 산식 · 영상 추론 | 수집 · 스케줄 · 통합 · 통보 · 카탈로그 |
| **왜** | 법령 근거로 검증돼야 하고, 회귀 테스트로 고정해야 함 | 현장·기관마다 다르고 자주 바뀜 |
| **바뀌는 주기** | 지침 개정 시 (연 단위) | 주 단위 |
| **누가 바꾸나** | 개발자 + 책임기술자 검토 | 관리자가 대화로 |
| **파일** | `backend/app/{domain,grading,bhc,opinion}.py` | `n8n/workflows/*.json` |

```
┌──────────────────────────────────────────────────────────────────┐
│ ① MCP 계층 — 관리자·AI 도구가 n8n에 접속                          │
│   Claude Desktop / Claude Code  ──MCP──▶  n8n 내장 MCP 서버        │
│   외부 MCP 서버  ──MCP Client Tool──▶  n8n AI Agent               │
│   n8n 워크플로우  ──MCP Server Trigger──▶  외부 AI 클라이언트       │
└───────────────────────────┬──────────────────────────────────────┘
                            │ n8n Public REST API (X-N8N-API-KEY)
┌───────────────────────────▼──────────────────────────────────────┐
│ ② 워크플로우 계층 — 배관                                          │
│   01 수집 · 02 관리자 에이전트 · 03 자동조사 · 04 CAPA · 05 일일보고 │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTP (세션 쿠키)
┌───────────────────────────▼──────────────────────────────────────┐
│ ③ 계산 커널 — KO-Detect API (FastAPI)                            │
│   검출 · 판정 · BHI · 소견 · 정책. 여기는 대화로 바뀌지 않는다.      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 1. n8n Public REST API 레퍼런스

### 1.1 접속

| 항목 | 값 |
|---|---|
| **Base URL** | 셀프호스트 `https://<instance>/api/v1` · 클라우드 `https://<name>.app.n8n.cloud/api/v1` |
| **인증 헤더** | `X-N8N-API-KEY: <key>` |
| **키 발급** | Settings → **n8n API** → Create an API key (라벨 · 만료 지정) |
| **스코프** | Enterprise만 지원 (`workflow:read`, `credential:create`, `execution:list` 등 20여 종). 그 외 플랜의 키는 **계정 전체 권한** |
| **비활성화** | 공개 API를 쓰지 않는 배포에서는 반드시 끈다 (`disable-the-public-api`) |

> ⚠️ **비-Enterprise 키는 스코프가 없습니다.** 에이전트에게 그대로 쥐어주면 자격증명
> 삭제·사용자 관리까지 가능합니다. §4의 프록시로 반드시 감쌉니다.

### 1.2 워크플로우 엔드포인트

| Method | Path | 용도 |
|---|---|---|
| `POST` | `/workflows` | 생성 |
| `GET` | `/workflows` | 목록 |
| `GET` | `/workflows/{id}` | 조회 |
| `PUT` | `/workflows/{id}` | 수정 |
| `DELETE` | `/workflows/{id}` | 삭제 |
| `GET` | `/workflows/{id}/{versionId}` | 특정 버전 조회 |
| `POST` | `/workflows/{id}/activate` | 활성화 *(deprecated)* |
| `POST` | `/workflows/{id}/deactivate` | 비활성화 *(deprecated)* |

**생성 요청 본문 (필수)** — `name`, `nodes`, `connections`, `settings`
**선택** — `description`, `staticData`, `pinData`, `nodeGroups`, `projectId`, `parentFolderId`

**목록 쿼리** — `offset`(0) · `limit`(100, 최대 250) · `cursor` · `active` · `tags` ·
`name` · `projectId` · `excludePinnedData`(false)

### 1.3 그 밖의 리소스

| 리소스 | 이 프로젝트에서의 쓰임 |
|---|---|
| `executions` | 수집 워크플로우 실패 추적 · 재실행 |
| `credentials` | 자격증명 생성만 허용, **조회·삭제는 프록시에서 차단** |
| `tags` | `kodetect` / `ingest` / `agent` / `capa` 로 분류 |
| `source-control`, `git-connections` | 워크플로우를 Git으로 버전관리 — **자기진화의 되돌리기 수단** |
| `audit` | n8n 자체 보안 감사 리포트 |
| `variables`, `projects`, `folders` | 기관별 격리(멀티테넌트) 기반 |
| `insights` | 실행 성공률 · 처리시간 대시보드 |

---

## 2. MCP 연결 — 세 가지 패턴

n8n은 MCP에서 **서버도 되고 클라이언트도 됩니다.** 셋을 섞으면 혼란해지므로 용도를
분명히 나눕니다.

| | 패턴 A | 패턴 B | 패턴 C |
|---|---|---|---|
| **이름** | n8n 내장 MCP 서버 | MCP Server Trigger | MCP Client Tool |
| **방향** | AI 도구 → n8n **전체** | AI 도구 → n8n **특정 워크플로우** | n8n 에이전트 → **외부** MCP |
| **노드** | (n8n 기능) | `n8n-nodes-langchain.mcptrigger` | `n8n-nodes-langchain.toolmcp` |
| **할 수 있는 일** | 워크플로우 생성·편집·실행, 데이터테이블 관리 | 지정한 도구만 노출 | 외부 도구를 에이전트에 주입 |
| **인증** | OAuth 또는 API 키 | Bearer 또는 헤더 | Bearer · 헤더 · 다중헤더 · OAuth2 · 없음 |
| **전송** | — | SSE + streamable HTTP | SSE |
| **KO-Detect 용도** | 개발자가 워크플로우를 대화로 만들 때 | **운영 중 관리자에게 노출하는 창구** | KO-Detect API·검색·Notion을 에이전트 도구로 |

### 2.1 왜 운영에는 패턴 B인가

패턴 A는 n8n 인스턴스 **전체**에 대한 권한입니다. 워크플로우 삭제도, 자격증명 조회도
됩니다. 개발 단계에서는 편하지만 운영 관리자에게 줄 권한이 아닙니다.

패턴 B는 **내가 연결한 도구만** 노출됩니다. "시설물 조회 · 검진 결과 조회 · 처방
상태 전이" 세 개만 붙여 두면, 관리자는 그 셋만 할 수 있습니다. 최소권한 원칙이
노드 연결로 표현됩니다.

### 2.2 MCP Server Trigger 설정

| 항목 | 값 |
|---|---|
| URL | 테스트용 · 운영용 두 개가 생성됨 |
| Path | 무작위 생성이 기본. 고정 엔드포인트가 필요하면 직접 지정 |
| 인증 | Bearer 토큰 또는 헤더 (HTTP Request 자격증명으로 구성) |
| 워크플로우 노출 | **Custom n8n Workflow Tool** 노드로 감싸 붙인다 |

> ⚠️ **큐 모드 배포 주의** — 웹훅 레플리카가 여러 개면 `/mcp*` 요청이 **반드시 하나의
> 전용 레플리카로 라우팅**되어야 합니다. MCP는 지속 연결을 쓰기 때문에 로드밸런서가
> 라운드로빈하면 세션이 끊깁니다.

### 2.3 MCP Client Tool 설정

- 외부 MCP 서버의 **SSE 엔드포인트 URL** 지정
- 도구 필터: **All / Selected / All Except** — 에이전트에 불필요한 도구를 노출하면
  판단이 흐려지므로 `Selected` 를 기본으로 씁니다
- 커뮤니티 MCP 노드를 에이전트 도구로 쓰려면 환경변수 필수:
  `N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE=true`

---

## 3. 워크플로우 카탈로그

| # | 이름 | 트리거 | 역할 | 안전등급 |
|---|---|---|---|---|
| 01 | `data_collection` | 15분 주기 + 웹훅 | 수집 → 메타 파싱 → 검출 API → 품질 분류 → 카탈로그 | 안전 (읽기·생성만) |
| 02 | `admin_agent` | 채팅 | 관리자 자연어 조회·수정 | **위험 (자기수정 권한)** |
| 03 | `auto_research` | 주 1회 (월 06:00) | 약점 탐지 → 조사 → 제안 등록 | 중간 (제안만) |
| 04 | `capa_watchdog` | 일 1회 (08:07) | 처방 기한 감시 → E1~E4 에스컬레이션 통보 | 중간 (통보) |
| 05 | `daily_progress` | 일 1회 (18:07) | 진행 상황 요약 → Slack 발송 | 안전 |

---

## 4. 자기진화 안전장치 — 4중

에이전트가 자기 자신을 고칠 수 있게 하는 순간, "무엇이 언제 왜 바뀌었는가"를 추적할
수 없으면 시스템은 신뢰를 잃습니다. 네 겹으로 막습니다.

### 겹 1 — 권한 프록시 (가장 중요)

**n8n API 키를 에이전트에게 직접 주지 않습니다.** 계산 커널에 얇은 프록시를 두고,
허용 목록에 있는 메서드·경로만 통과시킵니다.

```
에이전트  ──▶  KO-Detect /api/n8n/proxy  ──▶  n8n /api/v1
                     │
                     ├─ 허용:  GET  /workflows, /workflows/{id}
                     │        POST /workflows          (생성)
                     │        PUT  /workflows/{id}     (수정)
                     │        GET  /executions
                     │
                     └─ 차단:  DELETE *                (삭제 일체)
                              *  /credentials*         (자격증명 전부)
                              *  /users*, /roles*      (계정 관리)
                              POST /source-control/pull (원격 강제 반영)
```

프록시는 **모든 호출을 감사 로그에 먼저 기록한 뒤** 전달합니다. 기록 실패 시 전달하지
않습니다 — 추적 불가능한 변경을 만들지 않기 위함입니다.

### 겹 2 — 판정 기준 불변식

에이전트가 만들거나 고친 워크플로우가 다음을 건드리면 **거부**합니다.

| 금지 대상 | 이유 |
|---|---|
| `backend/app/domain.py`·`grading.py`·`bhc.py` 를 쓰는 노드 | 판정 기준은 코드로 고정 |
| `/api/bhc/*` 에 대한 `POST`·`PUT`·`DELETE` | 검진 결과는 조작 대상이 아니다 |
| 허용균열폭·등급 경계·적신호 상한을 하드코딩한 Code 노드 | 기준 이원화 = 반드시 어긋난다 |

정적 검사로 워크플로우 JSON을 훑어 위 패턴을 찾습니다 (§5 Step 7).

### 겹 3 — 사람 승인 대기

판정에 영향을 주는 변경 제안은 **`pending_review` 상태로만 등록**됩니다.
자동으로 반영되지 않습니다.

```json
{
  "status": "pending_review",
  "requires_human_approval": true,
  "proposed_change": "...",
  "sources": ["..."],
  "created_at": "2026-08-30T06:00:00+09:00"
}
```

### 겹 4 — Git 버전관리 + 되돌리기

n8n Source Control(`/source-control`, `/git-connections`)로 워크플로우를 Git에
커밋합니다. 잘못된 자기수정은 **커밋 되돌리기 한 번으로 원복**됩니다.
이것이 없으면 "에이전트가 뭔가 바꿨는데 이전 상태를 모른다"가 됩니다.

---

## 5. Step-by-Step 구현

### Step 0. 사전 준비

```bash
cp .env.example .env       # KODETECT_* 값 확인
docker compose -f n8n/docker-compose.yml up -d
```

→ <http://localhost:5678> 접속, 최초 관리자 계정 생성

확인할 환경변수 (`n8n/docker-compose.yml`):

| 변수 | 값 | 이유 |
|---|---|---|
| `N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE` | `true` | 커뮤니티 MCP 노드를 에이전트 도구로 사용 |
| `GENERIC_TIMEZONE`, `TZ` | `Asia/Seoul` | 크론이 한국시간으로 돌게 |
| `KODETECT_API` | `http://host.docker.internal:8077` | 컨테이너에서 호스트의 계산 커널 접근 |

### Step 1. API 키 발급

Settings → **n8n API** → Create an API key → 라벨 `kodetect-proxy`, 만료 90일

```bash
export N8N_API_KEY='n8n_api_...'
export N8N_URL='http://localhost:5678'

curl -s -H "X-N8N-API-KEY: $N8N_API_KEY" "$N8N_URL/api/v1/workflows?limit=5" | jq '.data[].name'
```

키가 동작하면 목록이 나옵니다. **이 키는 `.env`에만 두고 저장소에 커밋하지 않습니다.**

### Step 2. 워크플로우 임포트

UI에서 하나씩 붙여넣어도 되지만, API로 넣는 편이 재현됩니다.

```bash
for f in n8n/workflows/*.json; do
  echo "importing $f"
  curl -s -X POST "$N8N_URL/api/v1/workflows" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H 'Content-Type: application/json' \
    --data-binary @"$f" | jq -r '.id + "  " + .name'
done
```

> 생성 시 `name`·`nodes`·`connections`·`settings` 가 모두 필요합니다. 저장소의
> 워크플로우 JSON은 이 네 키를 갖추고 있습니다.

### Step 3. 자격증명 등록 (UI에서만)

| 자격증명 | 쓰는 곳 |
|---|---|
| Anthropic API | 02 관리자 에이전트 · 03 자동조사의 LLM |
| Slack (Bot Token) | 04 CAPA 감시 · 05 일일보고 |
| Notion | 03 자동조사의 리서치 노트 발행 |
| HTTP Header Auth (`X-N8N-API-KEY`) | 02의 자기수정 도구 |

**API로 자격증명을 만들지 않습니다.** 비밀값이 셸 히스토리와 감사 로그에 남기 때문입니다.

### Step 4. 계산 커널에 프록시 붙이기

`backend/app/routers/n8n_proxy.py` 를 만들고 §4 겹1의 허용목록을 구현합니다.

```python
ALLOWED = {
    ("GET",    r"^/workflows$"),
    ("GET",    r"^/workflows/[\w-]+$"),
    ("POST",   r"^/workflows$"),
    ("PUT",    r"^/workflows/[\w-]+$"),
    ("GET",    r"^/executions$"),
    ("GET",    r"^/executions/\d+$"),
}
DENIED_ALWAYS = (r"/credentials", r"/users", r"/roles", r"/source-control/pull")
```

호출 순서는 **감사 기록 → 검사 → 전달** 입니다. 기록이 실패하면 전달하지 않습니다.

### Step 5. 관리자 에이전트 연결 (패턴 B)

1. `02_admin_agent` 워크플로우를 열고 **MCP Server Trigger** 노드를 추가
2. Path를 `kodetect-admin` 으로 고정 (무작위 경로는 문서화가 안 됨)
3. Authentication = **Bearer**, HTTP Request 자격증명으로 토큰 등록
4. 노출할 도구만 트리거에 연결 — 시설물 조회 · 검진 결과 · 처방 상태 전이
5. Claude Desktop 연결:

```json
{
  "mcpServers": {
    "kodetect": {
      "command": "npx",
      "args": ["-y", "mcp-remote",
               "http://localhost:5678/mcp/kodetect-admin/sse",
               "--header", "Authorization: Bearer ${KODETECT_MCP_TOKEN}"],
      "env": { "KODETECT_MCP_TOKEN": "..." }
    }
  }
}
```

### Step 6. 외부 도구를 에이전트에 주입 (패턴 C)

`02_admin_agent` 의 **MCP Client Tool** 노드에서 외부 MCP 서버의 SSE URL을 지정하고,
도구 필터를 **Selected** 로 두어 필요한 것만 고릅니다. 도구가 많을수록 에이전트의
판단이 흐려집니다.

### Step 7. 워크플로우 정적 검사기

에이전트가 워크플로우를 만들거나 고칠 때마다 §4 겹2의 금지 패턴을 검사합니다.

```python
FORBIDDEN_IN_WORKFLOW = [
    r'/api/bhc/.*"method"\s*:\s*"(POST|PUT|DELETE)"',   # 검진 결과 변조
    r'허용균열폭|allowable_crack|BHI_CAP|RED_FLAG',       # 기준 하드코딩
    r'"method"\s*:\s*"DELETE"',                          # 삭제 일체
]
```

위반 시 워크플로우를 **비활성 상태로 생성**하고 사람 승인 대기로 등록합니다.

### Step 8. Git 버전관리 연결

Settings → Source Control → Git 저장소 연결 후, 변경마다 커밋합니다.
API로도 됩니다 (`/source-control`, `/git-connections`).
되돌리기 경로가 확보되기 전에는 에이전트에게 수정 권한을 주지 않습니다.

### Step 9. CAPA 감시 워크플로우 (04)

매일 08:07에 `GET /api/bhc/{id}/capa` 를 호출해 기한 초과 처방을 찾고,
에스컬레이션 단계에 따라 통보합니다.

| 단계 | 조건 | 통보 |
|---|---|---|
| E1 | 기한 초과 1일 | Slack 담당자 채널 |
| E2 | 초과 15일 또는 P0 24h 초과 | Slack + 관리책임자 멘션 |
| E3 | 초과 30일 | Slack + Notion 이슈 생성 · RF-7 검토 알림 |
| E4 | P0 7일 초과 미착수 | Slack 긴급 + 관계기관 통보 검토 서면 |

> 크론 분은 `:00` 을 피해 `:07` 로 둡니다. 모든 시스템이 정각에 몰리면 외부 API가
> 레이트리밋에 걸립니다.

### Step 10. 일일 진행 보고 (05)

매일 18:07에 오늘의 검출·판정·처방·에스컬레이션을 집계해 Slack에 올리고,
`docs/DAILY_UPDATE_<yyyymmdd>.md` 를 생성해 저장소에 커밋합니다.

---

## 6. 감사 로그 스키마

`/data/catalog/agent_audit/YYYYMM.jsonl` — 1줄 1레코드

```json
{
  "ts": "2026-08-30T15:04:05+09:00",
  "actor": "admin-agent",
  "session": "chat-9f2c",
  "action": "workflow.update",
  "target": "wf_7Kd2",
  "request": {"method": "PUT", "path": "/workflows/wf_7Kd2"},
  "allowed": true,
  "guard": {"static_check": "pass", "forbidden_hits": []},
  "tools_used": ["도구: 시설물 조회", "도구: 워크플로우 자기수정"],
  "rationale": "수집 주기를 15분에서 5분으로 단축 — 옹벽 현장 촬영 빈도 증가",
  "human_approval": null,
  "diff_summary": "scheduleTrigger.minutesInterval 15 -> 5"
}
```

**`rationale` 은 필수입니다.** 에이전트가 이유를 쓰지 못하면 그 변경은 통과시키지
않습니다. 나중에 사람이 읽고 판단할 수 있어야 합니다.

---

## 7. 실패 모드와 대응

| 실패 | 증상 | 원인 | 대응 |
|---|---|---|---|
| MCP 세션 끊김 | 도구 목록이 비거나 중간에 멈춤 | 큐 모드에서 `/mcp*` 가 여러 레플리카로 분산 | 전용 레플리카로 라우팅 고정 |
| 커뮤니티 MCP 노드가 도구로 안 뜸 | 에이전트가 도구를 못 봄 | `N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE` 미설정 | 환경변수 추가 후 재기동 |
| 에이전트가 자격증명을 조회 | — | API 키에 스코프가 없음(비-Enterprise) | 프록시에서 `/credentials` 차단 |
| 컨테이너에서 계산 커널 접속 불가 | `ECONNREFUSED` | `localhost` 는 컨테이너 자신 | `host.docker.internal` + `extra_hosts` |
| 수집 워크플로우가 조용히 0건 | 실행은 성공인데 결과 없음 | 파일명 규약 위반이 전부 격리 큐로 | 격리 큐 건수를 일일보고에 포함 |
| 크론이 UTC로 돔 | 새벽에 실행 | `GENERIC_TIMEZONE` 미설정 | `Asia/Seoul` 지정 |
| 자기수정 후 원복 불가 | 이전 상태를 모름 | Source Control 미연결 | Git 연결이 선행 조건 |

---

## 8. 도입 체크리스트

**연결 전**
- [ ] Source Control(Git) 연결 — 되돌리기 경로 확보가 **선행 조건**
- [ ] API 키 발급 및 만료일 설정, `.env` 에만 보관
- [ ] 프록시 허용목록 구현 및 `DELETE`·`/credentials` 차단 검증
- [ ] 정적 검사기로 금지 패턴 탐지 확인

**연결 후**
- [ ] MCP Server Trigger 경로 고정 · Bearer 인증 설정
- [ ] MCP Client Tool 도구 필터를 `Selected` 로 제한
- [ ] 감사 로그가 실제로 쌓이는지 1건 확인
- [ ] `pending_review` 제안이 자동 반영되지 않는지 반증 테스트
- [ ] 크론 분이 `:00`·`:30` 을 피하는지 확인

**운영**
- [ ] 일일보고에 격리 큐 건수 · 에스컬레이션 건수 포함
- [ ] 주간 `insights` 로 실행 성공률 확인
- [ ] API 키 만료 30일 전 갱신 알림

---

<sub>근거: n8n 공식 문서 (Public REST API · MCP Server Trigger · MCP Client Tool ·
내장 MCP 서버) 2026-08-30 조회 · BHC-STD-2026:0.9 §10<br/>
**배관은 스스로 바꿔도, 판정 기준은 사람이 승인합니다.**</sub>
