# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code 에게 고정 맥락을 제공합니다.
**코드를 변경할 때마다 이 문서도 함께 갱신하세요.**

---

## 프로젝트 개요

**NAS Agent** — Synology NAS 위에서 돌아가는 Telegram 봇. 사용자가 자연어/슬래시
커맨드로 요청을 보내면 **LangGraph 워크플로**가 Claude Code CLI 를 구동해
프로젝트를 만들고 수정하고 배포한다.

### 핵심 특징
- **장수 세션**: 프로젝트마다 고유 Claude CLI 세션(UUID). `/work` 시 `--resume` 으로
  이전 대화 맥락을 이어받음
- **자동 배포**: 코드 생성 후 `docker compose up -d --build` 자동 실행
- **GitHub 연동**: repo 자동 생성, 매 작업 후 commit + push (선택)
- **공유 MySQL**: `nas-mysql` 컨테이너에 프로젝트별 database/user 자동 프로비저닝 (선택)
- **Sub-agent**: 프로젝트별로 plan → code → review → fix 4단계 품질 워크플로 (선택)
- **프로젝트별 CLAUDE.md**: 워크플로가 각 프로젝트에 요구사항 기반 문서를 자동 생성/유지

## 절대 원칙

| 원칙 | 근거 |
|------|------|
| **Anthropic API 를 쓰지 않는다** | 모든 LLM 호출은 MAX 구독 CLI 서브프로세스. env 에서 `ANTHROPIC_API_KEY` 제거 필수 (`executor/claude_exec.py:19`). |
| **분류기는 로컬 정규식** | `bot/classifier.py` 가 룰 기반. API 과금 여지를 남기지 않는다. |
| **사용자 응답은 한국어** | 로그·에러 메시지·텔레그램 응답 한국어. 코드 식별자만 영문. |
| **CLI 는 `--permission-mode bypassPermissions`** | 자동화라 사용자 승인 프롬프트가 불가. |
| **이 CLAUDE.md 를 항상 최신 상태로 유지** | 코드 변경 시 관련 섹션도 갱신. 테스트 수·구조·명령어·환경변수 등. |

---

## 아키텍처

### 전체 흐름

```
┌─────────┐      ┌───────────┐      ┌──────────────┐
│ Telegram │ ───→ │ classifier│ ───→ │  LangGraph   │
│ Bot      │      │ (규칙)    │      │  Workflow    │
└─────────┘      └───────────┘      └──────┬───────┘
                                            │
                  ┌─────────────────────────┘
                  │
    ┌─────────────┼──────────────────────────────────────────────┐
    │             ▼                                              │
    │   ┌──────┐  ┌───────────┐  ┌─────────────┐                │
    │   │ load │→│github_init │→│ provision_db │                │
    │   └──────┘  └───────────┘  └──────┬──────┘                │
    │                                    │                       │
    │             ┌──────────────────────┘                       │
    │             ▼                                              │
    │   ┌──────┐  ┌──────┐  ┌────────┐  ┌─────┐                │
    │   │ plan │→│ code │→│ review │→│ fix │  ← sub-agent     │
    │   └──────┘  └──────┘  └────────┘  └──┬──┘  (프로젝트별)   │
    │                                       │                    │
    │             ┌─────────────────────────┘                    │
    │             ▼                                              │
    │   ┌────────┐  ┌─────────────┐  ┌─────────┐               │
    │   │ deploy │→│ github_sync │→│ persist │→ END            │
    │   └────────┘  └─────────────┘  └─────────┘               │
    │                                                            │
    │                    StateGraph (10 노드)                     │
    └────────────────────────────────────────────────────────────┘
```

### 노드 역할

| 노드 | 역할 | 조건 |
|------|------|------|
| `load` | 레지스트리 조회(continue) 또는 생성(new). sub_agents 플래그를 state 에 주입. | 항상 실행. 에러 시 즉시 END. |
| `github_init` | GitHub 빈 repo 생성 + repo_url 저장 | is_new + GITHUB_TOKEN |
| `provision_db` | MySQL database + user 프로비저닝. 자격증명 registry 저장. | is_new + db_required + MYSQL_ROOT_PASSWORD. 에러 시 persist 로 점프. |
| `plan` | **(sub-agent)** 기존 코드 분석 → 구현 계획. 일회용(ephemeral). | project.sub_agents = true |
| `code` | 메인 코딩. plan 결과가 있으면 프롬프트에 주입. 장수 세션 resume. | 항상 실행. |
| `review` | **(sub-agent)** 코드 리뷰. LGTM 또는 이슈 리포트. 일회용. | project.sub_agents = true |
| `fix` | **(sub-agent)** 리뷰 이슈 수정. coder 세션 resume. | sub_agents + review NOT LGTM |
| `deploy` | `docker compose -p <name> up -d --build`. compose 없으면 스킵. | CLI 성공 시 |
| `github_sync` | git init(최초) + add -A + commit + push | GITHUB_TOKEN + repo_url 존재 + CLI 성공 |
| `persist` | task 히스토리를 registry 에 기록 | 항상 실행 (에러 포함) |

### 선택적 노드의 noop 패턴

설정이 비활성이면 `{}` 반환. LangGraph 가 state 를 머지할 때 빈 dict 는 아무것도
변경하지 않으므로, 그래프 edge 를 조건부로 잘라낼 필요 없이 선형 체인으로 유지.

### Sub-agent 상세

| Agent | 세션 | CLI 플래그 | Timeout | 실패 시 |
|-------|------|-----------|---------|---------|
| Planner | 일회용 | `--no-session-persistence` | 5분 | 계획 없이 코딩 속행 (graceful) |
| Coder | 장수 세션 | `--session-id` / `--resume` | 15분 | 에러 반환 → deploy 스킵 |
| Reviewer | 일회용 | `--no-session-persistence` | 5분 | LGTM 으로 간주 |
| Fixer | Coder 세션 resume | `--resume` | 10분 | 에러 반환 |

**프로젝트별 설정**: `/new <name> --agents <desc>` → `projects.sub_agents=1` 저장.
이후 `/work` 시 `load` 노드가 프로젝트 레코드의 `sub_agents` 를 state 에 주입 →
plan/review/fix 노드가 `_agents_on(state)` 로 확인.

---

## 파일 구조

```
nas/                              총 ~3,500줄 (프로덕션 ~2,085 + 테스트 ~1,409)
│
├── bot/                          Telegram 인터페이스 (~640줄)
│   ├── main.py         (464)    핸들러, _init_state() 지연 초기화, _run_workflow_and_reply()
│   ├── classifier.py   (140)    classify(msg, known_projects) → routing dict
│   └── config.py        (37)    환경변수 → Config 클래스
│
├── executor/                     비즈니스 로직 (~1,445줄)
│   ├── workflow.py     (651)    LangGraph StateGraph 10노드, 프롬프트 상수, format_result
│   ├── projects.py     (245)    SQLite 레지스트리 (Project + TaskRecord + 스키마 마이그레이션)
│   ├── github_exec.py  (180)    REST API repo 생성, git CLI init/commit/push
│   ├── mysql_exec.py   (143)    docker exec 으로 MySQL 프로비저닝/삭제
│   ├── docker_exec.py  (113)    container 상태/로그/중지/재시작, system_status
│   └── claude_exec.py  (112)    run_claude() → ClaudeResult (session/ephemeral 지원)
│
├── tests/                        99 케이스 (~1,409줄), pytest + pytest-asyncio (auto mode)
│   ├── test_workflow.py   (714)  워크플로 e2e (sub-agent/GitHub/MySQL 경로 포함)
│   ├── test_github_exec.py(226)  aiohttp mock + git subprocess mock
│   ├── test_claude_exec.py(146)  CLI 플래그/env/timeout/JSON 파싱
│   ├── test_mysql_exec.py (116)  docker exec mock, SQL 검증
│   ├── test_classifier.py (115)  28 케이스 (NL 분류 + DB 키워드 + 한글 경계)
│   ├── test_projects.py    (88)  CRUD, 마이그레이션, validate_name
│   └── conftest.py          (4)  TELEGRAM_BOT_TOKEN 등 env 고정
│
├── docker-compose.yml            nas-agent 본체 (nas-agent-shared 네트워크)
├── docker-compose.infra.yml      nas-mysql (MySQL 8, utf8mb4, 공유 DB)
├── Dockerfile                    python:3.11-slim + Node 20 + Claude CLI + Docker CLI
├── requirements.txt              python-telegram-bot, aiohttp, pydantic, python-dotenv, langgraph
├── pytest.ini                    asyncio_mode = auto
├── .env.example                  모든 환경변수 템플릿
└── CLAUDE.md                     ← 이 파일
```

---

## 영속 상태

| 컨테이너 경로 | NAS 호스트 마운트 | 내용 |
|---|---|---|
| `/app/data/registry.db` | `/volume1/docker/nas-agent/data/` | SQLite: projects 테이블(name, description, session_id, repo_url, db_name, db_user, db_password, sub_agents) + tasks 테이블(히스토리) |
| `/app/projects/<name>/` | `/volume1/docker/nas-agent/projects/` | 프로젝트 소스코드, docker-compose.yml, CLAUDE.md, `.git/` |
| `/root/.claude/` | `/volume1/docker/nas-agent/claude-config/` | Claude CLI MAX 구독 세션 + per-project 대화 세션 |

---

## 주요 모듈 상세

### bot/main.py (464줄)
- `_init_state()` 에서 Registry → GitHubConfig → MySQLConfig → SubAgentConfig →
  `build_workflow()` 지연 초기화. 모듈 import 시점에 `/app/data` 를 만들면 테스트가 깨짐.
- 프로젝트당 `asyncio.Lock` 으로 동시 요청 직렬화 (CLI 세션 충돌 방지).
- `_run_workflow_and_reply()` 가 모든 프로젝트 작업의 공통 진입점. `sub_agents` 플래그를
  `/new --agents` 에서 파싱하거나 `/work` 에서 프로젝트 레코드로부터 로드.
- `/rm --drop-db`: 레지스트리 삭제 + MySQL database/user 삭제.

### bot/classifier.py (140줄)
- `classify(message, known_projects)` — 순수 함수.
- 우선순위: greeting → system_status → list_projects → **new project 정규식** →
  simple target actions(logs/stop/restart) → **project continue**(known_projects 매칭)
  → container status → complex fallback.
- `로그` 는 `블로그/로그인` 오탐 방지로 한글 lookbehind + 조사 lookahead.
- `DB_KEYWORD` 로 자연어 "db/mysql/데이터베이스" → `db_required: True`.

### executor/claude_exec.py (112줄)
- `run_claude(prompt, *, cwd, session_id, resume, timeout, ephemeral)` → `ClaudeResult`.
- 새 세션: `--session-id <uuid>`. 이어받기: `--resume <uuid>`. 일회용: `--no-session-persistence`.
- `--output-format json` → `{session_id, result, is_error}` 파싱.
- env 에서 `ANTHROPIC_API_KEY` 필터링.

### executor/projects.py (245줄)
- 동기 sqlite3 + `asyncio.to_thread` 래퍼.
- `_init_schema()`: `PRAGMA table_info` → 누락 컬럼 `ALTER TABLE ADD COLUMN` 마이그레이션.
- `_row_to_project()`: `sub_agents` INTEGER → Python `bool` 변환.
- `Project` dataclass: name, description, session_id, created_at, updated_at,
  repo_url, db_name, db_user, db_password, sub_agents.
- `create(..., sub_agents=False)` → `--agents` 플래그 반영.

### executor/github_exec.py (180줄)
- `create_repo()` — aiohttp GitHub REST API. user/org 자동 분기.
- `ensure_git_initialized()` — .git 유무로 init/set-url 분기.
  토큰은 `https://x-access-token:TOKEN@github.com/...` remote URL 임베드.
- `commit_and_push()` — `status --porcelain` 으로 변경 여부 확인, 없으면 "변경 없음".

### executor/mysql_exec.py (130줄)
- `provision(project_name, *, root_password, host, port)` → `DBCredentials`.
  식별자 `proj_<sanitized>`. 패스워드 `secrets.token_urlsafe(24)`.
- Synology MariaDB 에 `mysql -h <host> -P <port>` 로 TCP 직접 접속.
  root 비번은 `MYSQL_PWD` env 로 전달 (ps 노출 방지).
- `drop(project_name)` — database + user 삭제.

### executor/workflow.py (651줄)
- `build_workflow(registry, github_cfg, mysql_cfg, sub_agents_cfg)` — 클로저 DI.
- `WorkflowState(TypedDict, total=False)` — 입력 7개 + 중간/출력 14개 필드.
- 프롬프트 상수 8개: `NEW_PROJECT_PROMPT`, `CONTINUE_PROJECT_PROMPT`,
  `CLAUDE_MD_RULES_NEW`, `CLAUDE_MD_RULES_CONTINUE`, `PLAN_NEW_PROMPT`,
  `PLAN_CONTINUE_PROMPT`, `REVIEW_PROMPT`, `FIX_PROMPT`.
- `_db_prompt_section()`: DB 자격증명 + 네트워크 규칙을 프롬프트에 주입.
- `format_workflow_result(state)` → 텔레그램 응답 (3,500자 상한, 리뷰/수정/배포/GitHub 상태 표시).

---

## 인프라 구성

### docker-compose.yml (nas-agent)
- `nas-agent-shared` 외부 네트워크에 연결.
- 볼륨 4개: Docker 소켓, projects, data, claude-config.
- 헬스체크: `http://localhost:9100/health` (aiohttp).

### docker-compose.infra.yml (nas-mysql) — 대체됨
Synology MariaDB 패키지로 전환하여 더 이상 필수가 아님. 파일은 참고용으로 유지.
프로젝트 컴포즈는 NAS 호스트 IP(`MYSQL_HOST`)로 MariaDB 에 직접 접속.

### NAS 배포 순서
```sh
# 1. Synology DSM → 패키지센터 → MariaDB 10 설치 + root 비밀번호 설정
# 2. .env 에 MYSQL_ROOT_PASSWORD, MYSQL_HOST 설정
docker network create nas-agent-shared   # 최초 1회
cd /volume1/docker/nas-agent
docker compose up -d --build             # Bot
```

---

## 환경변수 (.env)

| 변수 | 필수 | 용도 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | O | @BotFather 발급 |
| `ALLOWED_USER_IDS` | O | 쉼표 구분 Telegram user ID |
| `CLAUDE_CLI_PATH` | — | 기본 `/usr/bin/claude` |
| `PROJECTS_DIR` | — | 기본 `/app/projects` |
| `DATA_DIR` | — | 기본 `/app/data` |
| `NAS_HOST` | — | 기본 `nas.local` |
| `HEALTH_PORT` | — | 기본 `9100` |
| `GITHUB_TOKEN` | — | PAT (repo 스코프). 비면 GitHub 비활성 |
| `GITHUB_OWNER` | — | 비면 토큰 소유자 user repo |
| `GITHUB_PRIVATE` | — | 기본 `true` |
| `GIT_USER_NAME` | — | 기본 `NAS Agent` |
| `GIT_USER_EMAIL` | — | 기본 `nas-agent@local` |
| `MYSQL_ROOT_PASSWORD` | — | Synology MariaDB root 비번. 비면 `--db` 거절 |
| `MYSQL_HOST` | — | 기본 NAS_HOST 값 (예: `192.168.0.100`) |
| `MYSQL_PORT` | — | 기본 `3306` |

Sub-agent 는 환경변수 없이 **프로젝트별** `/new --agents` 로 활성화.

---

## Telegram UX

```
── 프로젝트 ──────────────────────────────────────────────────────
/new <이름> [--db] [--agents] <설명>
    새 프로젝트. --db: MySQL, --agents: plan→code→review→fix
/work <이름> <작업>
    이어서 개발 (세션 resume → 재배포 → commit/push)
    sub_agents 는 프로젝트 설정에서 자동 로드
/info <이름>
    메타 + 최근 task 5건 + repo URL + DB 이름 + agents 여부
/projects
    레지스트리 목록
/rm <이름> [--drop-db]
    레지스트리 제거. --drop-db: MySQL DB 도 삭제

── 컨테이너 ──────────────────────────────────────────────────────
/sys                 NAS 리소스 상태 (CPU/MEM/DISK/컨테이너 수)
/status              실행 중 컨테이너 목록
/logs <컨테이너>      최근 30줄 로그
/stop <컨테이너>      중지
/restart <컨테이너>   재시작
```

**자연어 매핑**:
- 첫 토큰이 등록 프로젝트 이름 → `/work` 라우팅
- "db/mysql/데이터베이스" 포함 → `db_required` 자동 감지
- "프로젝트 X 만들어줘" → `/new` 라우팅

---

## 프로젝트별 CLAUDE.md (워크플로가 자동 생성/유지)

`/new` 실행 시 Claude 가 프로젝트 루트에 **CLAUDE.md** 를 반드시 생성하고,
`/work` 실행 시 같은 파일을 업데이트한다.

### 프롬프트 규칙 (executor/workflow.py)

**`CLAUDE_MD_RULES_NEW` (신규)**:
1. 프로젝트 개요 — 목적, 해결하는 문제, 주요 사용자
2. 원본 요구사항 — 사용자의 최초 설명을 그대로 인용
3. 기술 스택
4. 파일 구조 — 주요 디렉터리·파일 한 줄 설명
5. 실행·배포 — 로컬/NAS docker compose 명령
6. 환경변수 — 이름·용도·필수 여부
7. 데이터 모델 — (DB 가 있으면) 테이블·주요 필드
8. 변경 이력 — [YYYY-MM-DD] 형식

**`CLAUDE_MD_RULES_CONTINUE` (이어받기)**:
- 원본 요구사항 유지
- 기술 스택·파일 구조·데이터 모델은 변경 시 최신화
- 변경 이력 맨 위에 `[오늘 날짜] <이번 요청>` 추가

**추가 sub-agent 프롬프트**: `PLAN_NEW_PROMPT`, `PLAN_CONTINUE_PROMPT`,
`REVIEW_PROMPT`, `FIX_PROMPT`.

---

## 테스트

- **pytest + pytest-asyncio** (auto mode). 설정: `pytest.ini`.
- `tests/conftest.py` 가 `TELEGRAM_BOT_TOKEN` 등 필수 env 세팅.
- 서브프로세스(`claude`, `git`, `docker`, `mysql`) 전부 `unittest.mock.patch` 치환.
  실제 CLI/네트워크 호출 금지.
- **비동기 Mock**: `side_effect` 에 반드시 `async def` 함수 사용. sync lambda 로
  코루틴 돌려주면 await 시 unwrap 안 됨.
- **multi_claude 패턴**: sub-agent 테스트에서 prompt 키워드로 agent 구분.
  "계획만 출력" → planner, "코드를 리뷰" → reviewer, "리뷰어가 다음 문제" → fixer.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest
```

현재 **99 케이스**, ~0.5초.

---

## 코드 컨벤션

- Python 3.11. `from __future__ import annotations` 로 타입힌트.
- 에러: 한국어 사용자 메시지 + `logger.exception` 으로 stderr 디버깅.
- 서브프로세스: `decode(errors="replace")`.
- 긴 출력: `_truncate`/`_tail` 헬퍼로 텔레그램 4,096자 전에 압축.
- `TypedDict(total=False)` 사용 시 `dict.get(key)` 로 접근.
- 새 연동 패턴: `executor/<name>_exec.py` + `is_enabled(token)` + `@dataclass Config` +
  `WorkflowState` 필드 + noop 노드.

---

## 변경 시 주의

- **레지스트리 스키마**: `_init_schema` 에서 `PRAGMA table_info` → `ALTER TABLE ADD COLUMN`.
- **워크플로 노드 추가**: `WorkflowState` 필드 → 노드 함수 → `g.add_node` + edge.
  비활성 경로 `{}` 반환.
- **Claude 프롬프트**: 자동화 항목(git, 배포, CLAUDE.md) 은 "직접 하지 말라" 규칙.
- **이 CLAUDE.md**: 코드 변경 커밋 전에 관련 섹션(파일 구조, 줄 수, 환경변수, 테스트 수,
  Telegram UX 등) 최신화.

## 현재 제거된 것 (재도입 금지)

- `anthropic` Python SDK — 분류기가 API 호출하지 않음.
- `ANTHROPIC_API_KEY` 서브프로세스 전달 — `claude_exec.py` 에서 필터링.
- `network_mode: bridge` — `nas-agent-shared` 외부 네트워크로 이관.
- `docker-compose.yml` `environment:` 하드코딩 — `.env` 일원화.
- `SUB_AGENTS_ENABLED` 글로벌 환경변수 — 프로젝트별 `--agents` 로 전환.
- `MYSQL_CONTAINER`, `SHARED_NETWORK` 환경변수 — Synology MariaDB TCP 직접 접속으로 전환.

---

## 변경 이력

| 날짜 | 커밋 | 내용 |
|------|------|------|
| 2026-04-10 | `1f2580c` | 초기 커밋: Telegram 봇, Haiku 분류기, Claude CLI 실행기 |
| 2026-04-10 | `c68f44f` | MAX 구독 로그인으로 전환 |
| 2026-04-10 | `e9cecce` | `/sys` 명령 (NAS CPU/MEM/DISK) |
| 2026-04-14 | `81acfe0` | CLAUDE_CLI_PATH compose 하드코딩 제거, .env 일원화 |
| 2026-04-14 | `68a2e92` | Anthropic API 제거, 룰 기반 분류기, MAX 전용 CLI |
| 2026-04-16 | `e6abd94` | LangGraph 워크플로 + SQLite 레지스트리 + GitHub 연동 |
| 2026-04-16 | `7776409` | 공유 MySQL (nas-mysql) + /new --db 프로비저닝 |
| 2026-04-16 | `bd48f42` | 프로젝트별 CLAUDE.md 자동 생성/유지 규칙 |
| 2026-04-17 | `37a4867` | Sub-agent 워크플로 (plan → code → review → fix) |
| 2026-04-17 | `3b59162` | Sub-agent 를 프로젝트별 설정으로 전환 (/new --agents) |
| 2026-04-17 | — | CLAUDE.md 전면 재구성: 아키텍처 다이어그램, 노드 테이블, 줄 수 갱신 |
| 2026-04-17 | — | Synology MariaDB 전환: docker exec → mysql TCP 직접 접속 |
