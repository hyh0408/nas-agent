# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code 에게 고정 맥락을 제공합니다.

## 프로젝트 개요

**NAS Agent** — Synology NAS 위에서 돌아가는 Telegram 봇. 사용자가 자연어/슬래시
커맨드로 요청을 보내면 Claude Code CLI 가 그 자리에서 프로젝트를 만들고 수정하고
배포한다. 한 프로젝트는 **하나의 장수 Claude CLI 세션**으로 이어져서, 후속 요청이
이전 대화 맥락을 그대로 물려받은 채 개발을 계속할 수 있다.

## 절대 원칙

- **Anthropic API 를 쓰지 않는다.** 모든 LLM 호출은 MAX 구독으로 로그인한 Claude
  Code CLI 서브프로세스를 통한다. 서브프로세스 env 에서 `ANTHROPIC_API_KEY` 를
  제거한다 (`executor/claude_exec.py` 참조).
- **분류기는 로컬 정규식이다.** 과거 Haiku 호출을 제거하고 `bot/classifier.py` 가
  룰 기반으로 동작한다. API 과금이 생길 여지를 남기지 않는다.
- **댓글·로그·사용자 응답은 한국어.** 코드 식별자만 영문.
- **`--permission-mode bypassPermissions`** 로 CLI 를 돌린다 — 사용자가 프롬프트
  앞에 없으므로 권한 프롬프트를 건너뛸 수밖에 없다.

## 아키텍처

```
Telegram
   ↓ (python-telegram-bot long polling)
bot/main.py         handlers + /new /work /info /rm /projects
   ↓
bot/classifier.py   규칙 기반 라우팅 (simple / project / chat / complex)
   ↓
executor/workflow.py  LangGraph StateGraph
   ├─ load          registry 조회/생성
   ├─ github_init   GitHub 빈 repo 생성 (is_new + GITHUB_TOKEN)
   ├─ provision_db  MySQL database + user 생성 (is_new + db_required + MYSQL_ROOT_PASSWORD)
   ├─ claude        Claude CLI 실행 (세션 resume / new --session-id)
   ├─ deploy        docker compose -p <name> up -d --build
   ├─ github_sync   git init(한 번) + add/commit/push
   └─ persist       task 히스토리 기록
```

각 노드는 필요 없을 때 `{}` 를 반환해 안전히 건너뛴다. `load` 에서 에러가 나면
`route_after_load` 가 그래프 실행을 즉시 종료시키고, `provision_db` 에러는
`persist` 로 점프해 실패도 기록한다.

## 영속 상태

| 위치 | 내용 |
|-|-|
| `/app/data/registry.db` (SQLite) | 프로젝트 메타 (name, description, session_id, repo_url, db_name/user/password) 와 task 히스토리 |
| `/app/projects/<name>/` | 각 프로젝트의 소스. `.git/` 포함 |
| `/root/.claude/` | Claude CLI 로그인 세션 + per-project 대화 세션 |

NAS 볼륨 마운트는 `docker-compose.yml` 에 정의. 레지스트리 스키마 변경은
`executor/projects.py::_init_schema` 에서 `PRAGMA table_info` 로 누락 컬럼을
`ALTER TABLE ADD COLUMN` 한다.

## 주요 모듈

- **`bot/main.py`** — Telegram 핸들러. `_init_state()` 에서 Registry/Workflow 를
  지연 초기화 (모듈 import 시점에 `/app/data` 를 만들면 테스트가 깨지므로).
  프로젝트당 `asyncio.Lock` 으로 동시 요청을 직렬화.
- **`bot/classifier.py`** — 순수 함수 `classify(message, known_projects)`.
  - 단순 액션(상태/로그/중지/재시작)이 프로젝트 이어받기보다 우선.
  - `로그` 는 `블로그/로그인` 오탐 방지로 한글 경계 룰 적용.
  - `DB_KEYWORD` 로 자연어 `--db` 감지.
- **`executor/claude_exec.py`** — `run_claude(prompt, *, cwd, session_id, resume)` →
  `ClaudeResult`. 새 세션은 `--session-id <uuid>`, 이어받기는 `--resume <uuid>`.
  `--output-format json` 으로 구조화된 결과(`{session_id, result, is_error}`)를 받는다.
- **`executor/projects.py`** — 동기 sqlite3 + `asyncio.to_thread` 래핑. 한 번만
  import 되니 connection pool 은 불필요.
- **`executor/github_exec.py`** — REST API (aiohttp) 로 repo 생성, git CLI
  서브프로세스로 init/commit/push. 토큰은 `https://x-access-token:TOKEN@...` 로
  remote URL 에 임베드해서 credential helper 없이 push 가능.
- **`executor/mysql_exec.py`** — `docker exec -i nas-mysql mysql ...` 로 SQL 을
  stdin 에 흘려 넣는다. root 비번은 `MYSQL_PWD` env 로 전달 (ps 노출 방지).
  식별자는 `proj_<sanitized>` 로 정규화, 패스워드는 `secrets.token_urlsafe(24)`.
- **`executor/workflow.py`** — `build_workflow(registry, github_cfg, mysql_cfg)` 로
  StateGraph 컴파일. `WorkflowState` 는 `TypedDict(total=False)` 라 부분 업데이트만
  반환하면 된다.

## 인프라 컴포즈

- `docker-compose.yml` — nas-agent 본체. `nas-agent-shared` 외부 네트워크에 붙는다.
- `docker-compose.infra.yml` — `nas-mysql`. 최초 1회
  `docker network create nas-agent-shared` 필요.

프로젝트 컴포즈(Claude 가 생성)는 `nas-agent-shared` 를 external 로 참조해
`nas-mysql` hostname 으로 DB 에 붙는다.

## 테스트

- **pytest + pytest-asyncio** (auto mode). 설정은 `pytest.ini`.
- `tests/conftest.py` 가 `TELEGRAM_BOT_TOKEN` 등 필수 env 를 세팅해 import-time
  실패를 막는다.
- 서브프로세스(`claude`, `git`, `docker`, `mysql`) 는 전부 `unittest.mock.patch` 로
  가짜 프로세스로 치환한다. 실제 CLI/네트워크 호출 금지.
- 비동기 Mock 패턴: `side_effect` 에 `async def` 함수를 쓰고, 코루틴을 돌려주는
  sync `lambda` 는 쓰지 말 것 (await 시 unwrap 안 됨).

로컬 실행:
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest
```

현재 기준 **92 케이스** 가 0.3초 안에 완료된다.

## Telegram UX

```
/new <이름> [--db] <설명>   새 프로젝트 + (선택) MySQL + 자동 배포 + GitHub push
/work <이름> <작업>          이어서 개발. 세션 resume → 재배포 → commit/push
/info <이름>                 메타 + 최근 task 5건 + repo URL + DB 이름
/projects                    레지스트리 목록
/rm <이름> [--drop-db]       레지스트리 제거. --drop-db 로 MySQL DB 도 삭제
/sys /status /logs /stop /restart   컨테이너 운영 명령
```

자연어도 동일하게 동작. 첫 토큰이 등록된 프로젝트 이름이면 `/work` 로 라우팅.

## 코드 컨벤션

- Python 3.11, `from __future__ import annotations` 로 타입힌트 표기.
- 에러는 한국어 사용자 메시지로 응답, 디버깅용 세부는 `logger.exception` 으로
  stderr 에만.
- 서브프로세스 stdout/stderr 는 `decode(errors="replace")` 로 안전하게.
- 긴 CLI 출력은 `_truncate`/`_tail` 헬퍼로 텔레그램 4096자 제한 전에 압축.
- `TypedDict(total=False)` 사용 시 항상 `dict.get(key)` 로 접근해 KeyError 회피.

## 프로젝트별 CLAUDE.md (워크플로가 자동 생성/유지)

`/new` 실행 시 스캐폴드에 **프로젝트 루트의 CLAUDE.md** 가 반드시 포함되고,
`/work` 실행 시 같은 파일이 업데이트된다. 프롬프트 규칙(`CLAUDE_MD_RULES_NEW`
/ `CLAUDE_MD_RULES_CONTINUE`) 은 `executor/workflow.py` 상단에 상수로 있다.

포함 섹션: 개요 / 원본 요구사항 / 기술 스택 / 파일 구조 / 실행·배포 / 환경변수
/ 데이터 모델 / 변경 이력. 이어작업 세션은 "원본 요구사항" 섹션을 유지한 채
변경 이력 맨 위에 `[YYYY-MM-DD] <요청>` 한 줄을 추가한다.

이 CLAUDE.md 는 해당 프로젝트에서 **미래의 Claude 세션이 읽을 컨텍스트**이므로,
규칙을 바꿀 때는 두 방향(신규/이어받기)의 호환성을 같이 챙겨야 한다.

## 변경 시 주의

- **레지스트리 스키마 변경**: `_init_schema` 의 마이그레이션 블록에만 반영.
  `CREATE TABLE IF NOT EXISTS` 는 기존 DB 를 안 건드리니 새 컬럼은 반드시
  `ALTER TABLE ADD COLUMN` 으로 추가해야 한다.
- **워크플로 노드 추가**: `WorkflowState` 에 필드 추가 → 노드 함수 작성 →
  `g.add_node` + edge 배선. 비활성 경로(예: 토큰 없음) 에서는 `{}` 반환.
- **Claude 프롬프트**: `NEW_PROJECT_PROMPT` / `CONTINUE_PROJECT_PROMPT` 에 규칙을
  추가할 때, 자동화되는 항목(git, 배포)은 "직접 하지 말라"고 못 박는다.
  Claude 가 수동으로 `git commit` 해버리면 workflow 의 commit 이 빈 커밋이 된다.
- **새로운 연동 추가 (Slack/Discord/S3 등)**: `executor/<name>_exec.py` 에
  async 래퍼 + `is_enabled(token)` 패턴으로 만들고, `WorkflowState` 에 관련 필드
  추가, `build_workflow` 에 설정 객체 주입. 토큰 없을 때 noop 이 되도록.

## 현재 제거된 것 (재도입 금지)

- `anthropic` Python SDK — 제거 완료. 분류기가 더 이상 API 호출 안 함.
- `ANTHROPIC_API_KEY` 서브프로세스 전달 — 제거 완료.
- `network_mode: bridge` — `nas-agent-shared` 네트워크로 이관.
