# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code 에게 고정 맥락을 제공합니다.
**코드를 변경할 때마다 이 문서도 함께 갱신하세요.**

---

## 프로젝트 개요

**NAS Agent** — Synology NAS 위에서 돌아가는 Telegram 봇. 사용자가 자연어/슬래시
커맨드로 요청을 보내면 **LangGraph 워크플로**가 Claude Code CLI 를 구동해
프로젝트를 만들고 수정하고 배포한다. 하나의 프로젝트에는 **하나의 장수 Claude CLI
세션**(UUID 로 식별)이 이어져서, 후속 `/work` 요청이 이전 대화 맥락을 그대로
물려받는다. GitHub 자동 push 와 공유 MySQL 프로비저닝도 선택적으로 연동된다.

## 절대 원칙

| 원칙 | 근거 |
|------|------|
| **Anthropic API 를 쓰지 않는다** | 모든 LLM 호출은 MAX 구독 CLI 서브프로세스. env 에서 `ANTHROPIC_API_KEY` 제거 필수 (`executor/claude_exec.py:19`). |
| **분류기는 로컬 정규식** | `bot/classifier.py` 가 룰 기반. API 과금 여지를 남기지 않는다. |
| **사용자 응답은 한국어** | 로그·에러 메시지·텔레그램 응답 한국어. 코드 식별자만 영문. |
| **CLI 는 `--permission-mode bypassPermissions`** | 자동화라 사용자 승인 프롬프트가 불가. |
| **이 CLAUDE.md 를 항상 최신 상태로 유지** | 코드 변경 시 관련 섹션도 갱신. 테스트 수·구조·명령어·환경변수 등. |

## 아키텍처

```
Telegram (long polling)
   ↓
bot/main.py          핸들러 + /new /work /info /rm /projects /sys /status /logs /stop /restart
   ↓
bot/classifier.py    규칙 기반 라우팅 (simple / project / chat / complex)
   ↓
executor/workflow.py  LangGraph StateGraph (10 노드)
   ├─ load            registry 조회/생성
   ├─ github_init     GitHub 빈 repo 생성 (is_new + GITHUB_TOKEN)
   ├─ provision_db    MySQL database + user 생성 (is_new + db_required + MYSQL_ROOT_PASSWORD)
   ├─ plan            (sub-agent, 선택) 기존 코드 분석 → 구현 계획 [ephemeral]
   ├─ code            Claude CLI 로 코드 생성/수정 (세션 resume). plan 결과 주입
   ├─ review          (sub-agent, 선택) 코드 리뷰 → LGTM 또는 이슈 [ephemeral]
   ├─ fix             (sub-agent, 선택) 리뷰 이슈 수정 (coder 세션 resume)
   ├─ deploy          docker compose -p <name> up -d --build
   ├─ github_sync     git init(최초 1회) + add/commit/push
   └─ persist         task 히스토리 기록
```

**조건부 라우팅**
- `load` 에서 에러 → 즉시 `END` (프로젝트 없음/중복 등)
- `provision_db` 에서 에러 → `persist` 로 점프 (실패도 히스토리에 기록)
- 각 선택적 노드(github_init, provision_db, github_sync) 는 설정 토큰이 없으면
  `{}` 반환해 noop.

**Sub-agent 워크플로 (SUB_AGENTS_ENABLED=true)**

`plan → code → review → fix` 4단계. 비활성(기본값)이면 `plan`/`review`/`fix`가 noop.

| Agent | 세션 | 역할 | Timeout |
|-------|------|------|---------|
| Planner | 일회용 (`--no-session-persistence`) | 코드 분석 → 구현 계획. 파일 수정 금지. | 5분 |
| Coder | 프로젝트 장수 세션 (resume) | 계획 기반 코드 작성. plan_output 이 있으면 프롬프트에 포함. | 15분 |
| Reviewer | 일회용 | 코드 리뷰. 첫 줄 "LGTM" → pass, 아니면 이슈 리포트. | 5분 |
| Fixer | Coder 세션 resume | 리뷰 이슈 수정. LGTM 이면 noop. | 10분 |

MAX 구독 쿼터가 3~4배 소비되므로 필요할 때만 활성화.

**프로젝트별 설정**: `/new <name> --agents <desc>` 로 생성하면 `projects.sub_agents=1`
이 저장되어 이후 `/work` 시에도 자동으로 sub-agent 경로를 탄다. `_agents_on(state)`
가 `state["sub_agents"]` 를 확인하고, 이 값은 `load` 노드에서 프로젝트 레코드로부터
주입된다. 전역 env 설정 없이 프로젝트 단위로 판단.
Planner 실패 시에도 코딩은 계속 진행(graceful degradation).

## 파일 구조

```
nas/
├── bot/
│   ├── main.py          Telegram 핸들러, 워크플로 연결, 지연 초기화 (_init_state)
│   ├── classifier.py    classify(msg, known_projects) → 라우팅 dict
│   └── config.py        모든 환경변수 → Config 클래스 (37줄)
├── executor/
│   ├── claude_exec.py   run_claude() → ClaudeResult (--output-format json 파싱)
│   ├── docker_exec.py   container_status/logs/stop/restart, deploy_project, system_status
│   ├── github_exec.py   create_repo (REST), ensure_git_initialized, commit_and_push
│   ├── mysql_exec.py    provision / drop (docker exec -i nas-mysql mysql)
│   ├── projects.py      SQLite 레지스트리 (Project + TaskRecord), 스키마 마이그레이션
│   └── workflow.py      LangGraph StateGraph, 프롬프트 상수, format_workflow_result
├── tests/               94 케이스, pytest + pytest-asyncio (auto mode)
│   ├── conftest.py      TELEGRAM_BOT_TOKEN 등 env 설정
│   ├── test_classifier.py
│   ├── test_claude_exec.py
│   ├── test_github_exec.py
│   ├── test_mysql_exec.py
│   ├── test_projects.py
│   └── test_workflow.py
├── docker-compose.yml        nas-agent 본체 (nas-agent-shared 네트워크)
├── docker-compose.infra.yml  nas-mysql (공유 DB 인프라)
├── Dockerfile                python:3.11-slim + Node 20 + Claude CLI + Docker CLI
├── requirements.txt          python-telegram-bot, aiohttp, pydantic, python-dotenv, langgraph
├── pytest.ini
└── CLAUDE.md                 ← 이 파일
```

총 **~3,100줄** (프로덕션 ~1,860 + 테스트 ~1,250).

## 영속 상태

| 컨테이너 경로 | NAS 호스트 마운트 | 내용 |
|---|---|---|
| `/app/data/registry.db` | `/volume1/docker/nas-agent/data/` | SQLite: projects (name, description, session_id, repo_url, db_*) + tasks |
| `/app/projects/<name>/` | `/volume1/docker/nas-agent/projects/` | 프로젝트 소스코드 + `.git/` |
| `/root/.claude/` | `/volume1/docker/nas-agent/claude-config/` | CLI 로그인 세션 + per-project 대화 세션 |

## 주요 모듈 상세

### bot/main.py
- `_init_state()` 에서 Registry → GitHubConfig → MySQLConfig → `build_workflow()` 지연 초기화.
  모듈 import 시점에 `/app/data` 를 만들면 테스트가 깨지므로 반드시 지연.
- 프로젝트당 `asyncio.Lock` 으로 동시 요청 직렬화 (CLI 세션 충돌 방지).
- `_run_workflow_and_reply()` 가 모든 프로젝트 작업의 공통 진입점.

### bot/classifier.py
- `classify(message, known_projects)` — 순수 함수, async 래퍼 `classify_async` 도 제공.
- 우선순위: greeting → system_status → list_projects → **new project 정규식** →
  simple target actions(logs/stop/restart) → **project continue**(known_projects 매칭)
  → container status → complex fallback.
- `로그` 는 `블로그/로그인` 오탐 방지로 한글 경계 lookbehind + 조사 lookahead.
- `DB_KEYWORD` 로 자연어에서 "db/mysql/데이터베이스" 감지 → `db_required: True`.

### executor/claude_exec.py
- `run_claude(prompt, *, cwd, session_id, resume, timeout)` → `ClaudeResult`.
- 새 세션: `--session-id <uuid>` (미리 생성한 UUID). 이어받기: `--resume <uuid>`.
- `--output-format json` → `{session_id, result, is_error}` 파싱.
- env 에서 `ANTHROPIC_API_KEY` 필터링하여 MAX 구독만 사용 강제.

### executor/projects.py
- 동기 sqlite3 + `asyncio.to_thread` 래퍼.
- `_init_schema()` 에서 `PRAGMA table_info` 로 누락 컬럼(`repo_url`, `db_name`,
  `db_user`, `db_password`) 을 `ALTER TABLE ADD COLUMN` 으로 마이그레이션.
- `Project` dataclass 필드: name, description, session_id, created_at, updated_at,
  repo_url, db_name, db_user, db_password, sub_agents.

### executor/github_exec.py
- `create_repo()` — aiohttp 로 GitHub REST API (`POST /user/repos` 또는 `/orgs/{owner}/repos`).
- `ensure_git_initialized()` — `.git/` 없으면 init + remote add, 있으면 remote set-url.
  토큰은 `https://x-access-token:TOKEN@github.com/...` 으로 remote URL 에 임베드.
- `commit_and_push(project_dir, message)` — `status --porcelain` 으로 변경 여부 확인,
  없으면 "변경 없음" 반환.

### executor/mysql_exec.py
- `provision(project_name, *, root_password)` → `DBCredentials`.
  식별자 `proj_<sanitized>`, 패스워드 `secrets.token_urlsafe(24)`.
- `docker exec -i -e MYSQL_PWD=... nas-mysql mysql -uroot` 로 SQL 을 stdin 에 전달
  (ps 노출 방지).
- `drop(project_name, *, root_password)` — database + user 삭제.

### executor/workflow.py
- `build_workflow(registry, github_cfg, mysql_cfg, sub_agents_cfg)` — 클로저로 DI, `StateGraph` 컴파일.
- `WorkflowState` 는 `TypedDict(total=False)` — 각 노드가 부분 dict 만 반환.
- 프롬프트 상수: `NEW_PROJECT_PROMPT`, `CONTINUE_PROJECT_PROMPT`, `CLAUDE_MD_RULES_NEW`,
  `CLAUDE_MD_RULES_CONTINUE`, `_db_prompt_section()`.
- `format_workflow_result(state)` → 텔레그램 응답 문자열 (3,500자 상한).

## 인프라 구성

### docker-compose.yml (nas-agent)
- `nas-agent-shared` 외부 네트워크에 연결.
- 볼륨: Docker 소켓, projects, data, claude-config.
- 헬스체크: `http://localhost:9100/health`.

### docker-compose.infra.yml (nas-mysql)
- MySQL 8 + utf8mb4. `nas-agent-shared` 외부 네트워크.
- 최초 1회 `docker network create nas-agent-shared` 필요.
- 프로젝트 컴포즈(Claude 생성)도 같은 네트워크를 external 로 참조해
  `nas-mysql` hostname 으로 DB 접근.

### NAS 배포 순서
```sh
docker network create nas-agent-shared   # 최초 1회
cd /volume1/docker/nas-agent
docker compose -f docker-compose.infra.yml up -d   # MySQL
docker compose up -d --build                        # Bot
```

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
| `GITHUB_TOKEN` | — | PAT (repo 스코프). 비면 GitHub 연동 비활성 |
| `GITHUB_OWNER` | — | 비면 토큰 소유자 user repo |
| `GITHUB_PRIVATE` | — | 기본 `true` |
| `GIT_USER_NAME` | — | 기본 `NAS Agent` |
| `GIT_USER_EMAIL` | — | 기본 `nas-agent@local` |
| `MYSQL_ROOT_PASSWORD` | — | 비면 MySQL 연동 비활성 |
| `MYSQL_CONTAINER` | — | 기본 `nas-mysql` |
| `MYSQL_HOST` | — | 기본 `nas-mysql` |
| `MYSQL_PORT` | — | 기본 `3306` |
| `SHARED_NETWORK` | — | 기본 `nas-agent-shared` |

## Telegram UX

```
── 프로젝트 ─────────────
/new <이름> [--db] [--agents] <설명>   새 프로젝트 (--db: MySQL, --agents: sub-agent)
/work <이름> <작업>          이어서 개발 (세션 resume → 재배포 → commit/push)
/info <이름>                 메타 + 최근 task 5건 + repo URL + DB 이름
/projects                    프로젝트 목록
/rm <이름> [--drop-db]       레지스트리 제거 (--drop-db: MySQL DB 도 삭제)

── 컨테이너 ─────────────
/sys                        NAS 리소스 상태 (CPU/MEM/DISK)
/status                     실행 중 컨테이너 목록
/logs <컨테이너>             최근 30줄 로그
/stop <컨테이너>             중지
/restart <컨테이너>          재시작
```

자연어도 동작: 첫 토큰이 등록 프로젝트 이름이면 `/work` 로 라우팅.
"db/mysql/데이터베이스" 키워드 포함 시 `db_required` 자동 감지.

## 프로젝트별 CLAUDE.md (워크플로가 자동 생성/유지)

`/new` 실행 시 스캐폴드에 **프로젝트 루트의 CLAUDE.md** 가 반드시 포함되고,
`/work` 실행 시 같은 파일이 업데이트된다.

프롬프트 규칙은 `executor/workflow.py` 상단 상수:
- `CLAUDE_MD_RULES_NEW` — 신규: 개요 / 원본 요구사항 / 기술 스택 / 파일 구조 /
  실행·배포 / 환경변수 / 데이터 모델 / 변경 이력
- `CLAUDE_MD_RULES_CONTINUE` — 이어받기: 원본 요구사항 유지, 관련 섹션 최신화,
  변경 이력 맨 위에 `[YYYY-MM-DD] <요청>` 한 줄 추가
- Sub-agent 프롬프트: `PLAN_NEW_PROMPT`, `PLAN_CONTINUE_PROMPT`, `REVIEW_PROMPT`, `FIX_PROMPT`

이 CLAUDE.md 는 미래 Claude 세션이 읽는 컨텍스트이므로, 규칙 변경 시 신규/이어받기
양쪽 호환성을 챙길 것.

## 테스트

- **pytest + pytest-asyncio** (auto mode). 설정은 `pytest.ini`.
- `tests/conftest.py` 가 `TELEGRAM_BOT_TOKEN` 등 필수 env 를 세팅.
- 서브프로세스(`claude`, `git`, `docker`, `mysql`) 는 전부 `unittest.mock.patch` 로
  치환. 실제 CLI/네트워크 호출 금지.
- 비동기 Mock: `side_effect` 에 반드시 `async def` 함수를 쓸 것. sync lambda 로
  코루틴을 돌려주면 await 시 unwrap 안 되는 버그.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest
```

현재 **99 케이스**, ~0.5초.

## 코드 컨벤션

- Python 3.11. `from __future__ import annotations` 로 타입힌트.
- 에러는 한국어 사용자 메시지, 디버깅 세부는 `logger.exception` 으로 stderr 에만.
- 서브프로세스 stdout/stderr: `decode(errors="replace")`.
- 긴 출력: `_truncate`/`_tail` 헬퍼로 텔레그램 4,096자 제한 전에 압축.
- `TypedDict(total=False)` 사용 시 `dict.get(key)` 로 접근.

## 변경 시 주의

- **레지스트리 스키마 변경**: `executor/projects.py::_init_schema` 의 마이그레이션
  블록에서 `PRAGMA table_info` → `ALTER TABLE ADD COLUMN`. `CREATE TABLE IF NOT
  EXISTS` 는 기존 DB 를 안 건드림.
- **워크플로 노드 추가**: `WorkflowState` 에 필드 추가 → 노드 함수 작성 →
  `g.add_node` + edge 배선. 비활성 경로에서는 `{}` 반환.
- **Claude 프롬프트 수정**: 자동화 항목(git, 배포, CLAUDE.md) 은 "직접 하지 말라"
  규칙 유지. Claude 가 `git commit` 하면 workflow commit 이 빈 커밋이 됨.
- **새 연동 추가**: `executor/<name>_exec.py` + `is_enabled(token)` 패턴 →
  `WorkflowState` 필드 추가 → `build_workflow` 에 설정 객체 주입 → 토큰 없을 때
  noop.
- **이 CLAUDE.md 갱신**: 코드 변경 커밋 전에 관련 섹션(파일 구조, 환경변수, 테스트
  수, Telegram UX 등) 을 최신화할 것.

## 현재 제거된 것 (재도입 금지)

- `anthropic` Python SDK — 분류기가 더 이상 API 호출 안 함.
- `ANTHROPIC_API_KEY` 서브프로세스 전달 — `executor/claude_exec.py` 에서 필터링.
- `network_mode: bridge` — `nas-agent-shared` 외부 네트워크로 이관.
- `docker-compose.yml` 의 `environment:` 하드코딩 — `.env` 로 일원화.

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
| 2026-04-17 | — | CLAUDE.md 전면 갱신, 자동 갱신 정책 명시 |
| 2026-04-17 | — | Sub-agent 워크플로 (plan → code → review → fix). ephemeral CLI 세션 |
| 2026-04-17 | — | Sub-agent 를 프로젝트별 설정으로 전환 (/new --agents, registry 저장) |
