"""LangGraph 기반 프로젝트 개발 워크플로.

한 번의 사용자 요청 = 한 번의 그래프 실행.

노드:
  load         - 레지스트리에서 프로젝트 조회 또는 생성
  github_init  - (선택) 새 프로젝트일 때 GitHub repo 생성
  provision_db - (선택) MySQL database + user 생성
  plan         - (선택, sub-agent) 기존 코드 분석 → 구현 계획
  code         - Claude CLI 로 코드 생성/수정 (세션 resume)
  review       - (선택, sub-agent) 코드 리뷰 → LGTM 또는 이슈 리포트
  fix          - (선택, sub-agent) 리뷰 이슈 수정 (coder 세션 resume)
  deploy       - docker compose up -d --build 로 자동 배포
  github_sync  - (선택) 코드 변경사항을 git commit + push
  persist      - task 히스토리 기록

SUB_AGENTS_ENABLED=false (기본값) 이면 plan/review/fix 는 noop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from executor import github_exec, mysql_exec
from executor.claude_exec import run_claude, ClaudeResult
from executor.github_exec import GitHubError, RepoInfo
from executor.mysql_exec import DBCredentials, MySQLError
from executor.projects import Project, ProjectError, ProjectRegistry


logger = logging.getLogger("workflow")


@dataclass
class GitHubConfig:
    token: str = ""
    owner: str = ""        # 비면 토큰 소유자 (user repo) 로 생성
    private: bool = True
    user_name: str = "NAS Agent"
    user_email: str = "nas-agent@local"

    @property
    def enabled(self) -> bool:
        return github_exec.is_enabled(self.token)


@dataclass
class MySQLConfig:
    root_password: str = ""
    host: str = "192.168.0.100"
    port: int = 3306

    @property
    def enabled(self) -> bool:
        return mysql_exec.is_enabled(self.root_password)


@dataclass
class SubAgentConfig:
    enabled: bool = False

    # 각 agent 타임아웃 (초)
    plan_timeout: int = 300
    review_timeout: int = 300
    fix_timeout: int = 600


class WorkflowState(TypedDict, total=False):
    # 입력
    project_name: str
    task: str
    is_new: bool
    description: str
    projects_dir: str
    sub_agents: bool       # 프로젝트별 sub-agent 사용 여부

    # 중간 / 출력
    project: Optional[Project]
    plan_output: str               # planner agent 출력
    cli_result: Optional[ClaudeResult]
    review_output: str             # reviewer agent 출력
    review_passed: bool            # LGTM 여부
    fix_result: Optional[ClaudeResult]
    deploy_output: str
    deployed: bool
    github_output: str
    github_pushed: bool
    db_credentials: Optional[DBCredentials]
    db_output: str
    error: Optional[str]
    status: str


CLAUDE_MD_RULES_NEW = (
    "- 프로젝트 루트에 CLAUDE.md 를 **반드시** 생성. 다음 섹션을 한국어로 작성:\n"
    "    1. 프로젝트 개요 — 목적, 해결하는 문제, 주요 사용자\n"
    "    2. 원본 요구사항 — 사용자의 최초 설명을 그대로 인용\n"
    "    3. 기술 스택 — 언어/프레임워크/런타임\n"
    "    4. 파일 구조 — 주요 디렉터리·파일 한 줄 설명\n"
    "    5. 실행·배포 — 로컬/NAS docker compose 명령\n"
    "    6. 환경변수 — 이름·용도·필수 여부\n"
    "    7. 데이터 모델 — (DB 가 있으면) 테이블·주요 필드\n"
    "    8. 변경 이력 — [YYYY-MM-DD] 형식으로 이번 스캐폴딩 항목\n"
)

CLAUDE_MD_RULES_CONTINUE = (
    "- 프로젝트 루트의 CLAUDE.md 가 있으면 **반드시 업데이트**. 없으면 새로 생성.\n"
    "    - 원본 요구사항 섹션은 유지, 이번 변경으로 확장된 기능이 있으면 기술 스택·\n"
    "      파일 구조·데이터 모델 섹션에 반영\n"
    "    - 변경 이력 맨 위에 `[오늘 날짜] <이번 요청>` 한 줄 추가\n"
    "    - 기존 내용과 모순되는 부분은 최신화하되, 역사적 맥락은 가능한 보존\n"
)


NEW_PROJECT_PROMPT = (
    "새 프로젝트 '{name}' 를 현재 디렉터리에 스캐폴딩합니다.\n"
    "설명: {description}\n"
    "{db_section}"
    "\n작업 규칙:\n"
    "- 모든 파일을 현재 디렉터리 안에 배치\n"
    "- Dockerfile 과 docker-compose.yml 반드시 포함\n"
    "- docker-compose.yml 의 container_name 은 '{name}'\n"
    "- 포트 충돌이 없도록 합리적인 호스트 포트 선택\n"
    "- 작업이 끝나면 자동으로 docker compose 로 배포되므로 즉시 실행 가능한 상태여야 함\n"
    "- git 작업(add/commit/push) 은 자동 처리되므로 직접 실행하지 마세요\n"
    "{claude_md_rules}"
    "- 마지막에 한국어로 변경 내역을 3~5줄로 요약"
)

CONTINUE_PROJECT_PROMPT = (
    "프로젝트 '{name}' 를 이어서 개발합니다.\n"
    "현재 디렉터리가 프로젝트 루트이며 이전 작업 세션이 복원되어 있습니다.\n\n"
    "요청: {task}\n"
    "{db_section}"
    "\n작업 규칙:\n"
    "- 기존 파일 구조/스타일 유지\n"
    "- Dockerfile / docker-compose.yml 이 이미 있으면 그대로 사용하고 필요 시에만 수정\n"
    "- 작업이 끝나면 자동으로 재배포되므로 즉시 실행 가능한 상태여야 함\n"
    "- git 작업(add/commit/push) 은 자동 처리되므로 직접 실행하지 마세요\n"
    "{claude_md_rules}"
    "- 마지막에 한국어로 변경 내역을 3~5줄로 요약"
)


PLAN_NEW_PROMPT = (
    "프로젝트 '{name}' 를 새로 만들기 위한 **구현 계획**을 작성하세요.\n"
    "설명: {description}\n"
    "{db_section}"
    "\n아래 항목을 한국어로 출력하세요:\n"
    "1. 기술 스택 선택 근거\n"
    "2. 디렉터리·파일 구조\n"
    "3. 각 파일의 핵심 역할 (1줄)\n"
    "4. Dockerfile / docker-compose.yml 설계\n"
    "5. 주의사항·엣지 케이스\n\n"
    "**파일을 생성하거나 수정하지 마세요.** 계획만 출력합니다."
)

PLAN_CONTINUE_PROMPT = (
    "프로젝트 '{name}' 에 대해 다음 작업의 **구현 계획**을 작성하세요.\n"
    "요청: {task}\n"
    "{db_section}"
    "\n현재 디렉터리의 코드를 읽고 아래 항목을 한국어로 출력하세요:\n"
    "1. 수정/생성할 파일 목록\n"
    "2. 각 파일의 변경 내용 요약\n"
    "3. 기존 코드와의 호환성 고려사항\n"
    "4. 예상 위험·엣지 케이스\n\n"
    "**파일을 생성하거나 수정하지 마세요.** 계획만 출력합니다."
)

REVIEW_PROMPT = (
    "프로젝트 '{name}' 의 코드를 리뷰하세요.\n\n"
    "현재 디렉터리에 있는 파일들을 읽고 다음을 확인하세요:\n"
    "1. 버그 가능성·로직 오류\n"
    "2. Dockerfile / docker-compose.yml 올바른지\n"
    "3. 보안 취약점 (하드코딩된 비밀, SQL 인젝션 등)\n"
    "4. 기존 코드와의 불일치\n\n"
    "문제가 없으면 첫 줄에 **LGTM** 이라고 쓰고 간단히 요약.\n"
    "문제가 있으면 각 이슈를 번호로 나열하고 수정 방법을 구체적으로 작성.\n\n"
    "**파일을 수정하지 마세요.** 리뷰만 출력합니다."
)

FIX_PROMPT = (
    "프로젝트 '{name}' 의 리뷰어가 다음 문제를 발견했습니다:\n\n"
    "{review_output}\n\n"
    "위 모든 문제를 수정하세요.\n"
    "작업 규칙:\n"
    "- 기존 파일 구조/스타일 유지\n"
    "- git 작업 직접 하지 마세요\n"
    "- 마지막에 한국어로 수정 내역을 요약"
)


def _db_prompt_section(creds: Optional[DBCredentials]) -> str:
    if creds is None:
        return ""
    return (
        "\n사용 가능한 MySQL/MariaDB (NAS 호스트에서 운영 중):\n"
        f"  HOST: {creds.host}\n"
        f"  PORT: {creds.port}\n"
        f"  DATABASE: {creds.database}\n"
        f"  USER: {creds.user}\n"
        f"  PASSWORD: {creds.password}\n\n"
        f"DB 관련 필수 규칙:\n"
        f"- 위 MariaDB 만 사용하세요. PostgreSQL, SQLite, MongoDB 등 다른 DB 를 설치하거나\n"
        f"  docker-compose 에 DB 서비스를 추가하지 마세요.\n"
        f"- 앱 서비스 environment 에 MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE /\n"
        f"  MYSQL_USER / MYSQL_PASSWORD 를 위 값 그대로 주입하세요.\n"
        f"- ORM 을 쓸 때는 MySQL/MariaDB 호환 드라이버를 사용하세요\n"
        f"  (예: pymysql, mysqlclient, aiomysql).\n"
        f"- network_mode: bridge 로 NAS 호스트에 접근 가능하게 설정\n"
    )


def build_workflow(
    registry: ProjectRegistry,
    github: Optional[GitHubConfig] = None,
    mysql: Optional[MySQLConfig] = None,
    sub_agents: Optional[SubAgentConfig] = None,
    *,
    deploy_timeout: int = 600,
):
    """레지스트리·GitHub·MySQL·Sub-agent 설정을 클로저로 바인딩한 컴파일된 그래프를 돌려준다."""
    gh_cfg = github or GitHubConfig()
    my_cfg = mysql or MySQLConfig()
    sa_cfg = sub_agents or SubAgentConfig()

    async def load(state: WorkflowState) -> dict:
        name = state["project_name"]
        logger.info(f"[load] '{name}' is_new={state['is_new']}")
        try:
            existing = await registry.get(name)
            if state["is_new"]:
                if existing:
                    logger.warning(f"[load] '{name}' 이미 존재")
                    return {"error": f"이미 존재하는 프로젝트: {name}", "status": "error"}
                use_agents = state.get("sub_agents", False)
                project = await registry.create(
                    name, state.get("description", ""), sub_agents=use_agents
                )
                logger.info(f"[load] '{name}' 생성 완료 session={project.session_id[:8]}…")
            else:
                if not existing:
                    logger.warning(f"[load] '{name}' 찾을 수 없음")
                    return {
                        "error": f"프로젝트를 찾을 수 없습니다: {name}",
                        "status": "error",
                    }
                project = existing
                logger.info(f"[load] '{name}' 로드 완료 session={project.session_id[:8]}…")
        except ProjectError as e:
            logger.error(f"[load] '{name}' 오류: {e}")
            return {"error": str(e), "status": "error"}
        return {
            "project": project,
            "sub_agents": project.sub_agents,
            "status": "loaded",
        }

    async def github_init(state: WorkflowState) -> dict:
        """새 프로젝트일 때 GitHub repo 를 만들거나, 이미 있으면 clone 한다."""
        if not state["is_new"] or not gh_cfg.enabled:
            return {}
        project: Project = state["project"]
        logger.info(f"[github_init] '{project.name}' repo 확인/생성 중…")
        try:
            repo: RepoInfo = await github_exec.create_repo(
                project.name,
                project.description or state.get("description", ""),
                gh_cfg.token,
                private=gh_cfg.private,
                owner=gh_cfg.owner or None,
            )
        except GitHubError as e:
            logger.warning(f"[github_init] 실패: {e}")
            return {"github_output": f"GitHub repo 생성 실패: {e}"}

        await registry.set_repo_url(project.name, repo.html_url)

        # 기존 repo 면 clone 해서 프로젝트 디렉터리에 코드를 가져온다
        project_dir = os.path.join(state["projects_dir"], project.name)
        clone_result = await github_exec.clone_or_pull(
            project_dir,
            repo.clone_url,
            token=gh_cfg.token,
            user_name=gh_cfg.user_name,
            user_email=gh_cfg.user_email,
        )
        if clone_result.ok:
            logger.info(f"[github_init] '{project.name}' {clone_result.output}")
        else:
            logger.warning(f"[github_init] '{project.name}' clone/pull 실패: {clone_result.output}")

        refreshed = await registry.get(project.name)
        logger.info(f"[github_init] '{project.name}' repo: {repo.html_url}")
        return {"project": refreshed, "github_output": f"GitHub repo: {repo.html_url}"}

    async def provision_db(state: WorkflowState) -> dict:
        """MySQL 이 설정되어 있으면 자동 프로비저닝. --db 플래그 불필요.
        새 프로젝트: DB 자동 생성. 기존 프로젝트: 저장된 자격증명 로드,
        없으면 소급 생성."""
        project: Optional[Project] = state.get("project")
        if project is None or not my_cfg.enabled:
            return {}

        # 이미 DB 가 있으면 자격증명만 state 에 올린다
        if project.db_name and project.db_user and project.db_password:
            creds = DBCredentials(
                host=my_cfg.host,
                port=my_cfg.port,
                database=project.db_name,
                user=project.db_user,
                password=project.db_password,
            )
            return {"db_credentials": creds}

        # DB 가 없으면 프로비저닝 (새 프로젝트 or 기존 프로젝트 소급)
        try:
            creds = await mysql_exec.provision(
                project.name,
                root_password=my_cfg.root_password,
                host=my_cfg.host,
                port=my_cfg.port,
            )
        except MySQLError as e:
            logger.warning(f"[provision_db] '{project.name}' 프로비저닝 실패: {e}")
            return {}  # DB 실패해도 프로젝트 생성은 계속 진행

        await registry.set_db_info(
            project.name, creds.database, creds.user, creds.password
        )
        refreshed = await registry.get(project.name)
        logger.info(f"[provision_db] '{project.name}' DB 생성: {creds.database}")
        return {
            "project": refreshed,
            "db_credentials": creds,
            "db_output": f"DB 생성: {creds.database} / user {creds.user}",
        }

    def _agents_on(state: WorkflowState) -> bool:
        return bool(state.get("sub_agents", False))

    async def plan(state: WorkflowState) -> dict:
        """(sub-agent) 구현 계획을 세운다. 비활성이면 noop."""
        if not _agents_on(state):
            return {}
        if state.get("error"):
            return {}
        project: Project = state["project"]
        project_dir = os.path.join(state["projects_dir"], project.name)
        os.makedirs(project_dir, exist_ok=True)

        db_section = _db_prompt_section(state.get("db_credentials"))
        if state["is_new"]:
            prompt = PLAN_NEW_PROMPT.format(
                name=project.name,
                description=state.get("description", ""),
                db_section=db_section,
            )
        else:
            prompt = PLAN_CONTINUE_PROMPT.format(
                name=project.name, task=state["task"], db_section=db_section,
            )

        logger.info(f"[plan] '{project.name}' planner 실행 중… (timeout={sa_cfg.plan_timeout}s)")
        result = await run_claude(
            prompt, cwd=project_dir, ephemeral=True, timeout=sa_cfg.plan_timeout,
        )
        if result.is_error:
            logger.warning(f"[plan] '{project.name}' planner 실패: {result.text[:200]}")
            return {"plan_output": ""}
        logger.info(f"[plan] '{project.name}' planner 완료 ({len(result.text)}자)")
        return {"plan_output": result.text}

    async def code(state: WorkflowState) -> dict:
        """메인 코딩 agent. plan_output 이 있으면 프롬프트에 포함."""
        project: Project = state["project"]
        project_dir = os.path.join(state["projects_dir"], project.name)
        os.makedirs(project_dir, exist_ok=True)

        db_section = _db_prompt_section(state.get("db_credentials"))
        plan_output = state.get("plan_output", "")
        plan_section = (
            f"\n아래는 사전 분석(planner)의 구현 계획입니다. 이 계획을 따라 작업하세요:\n"
            f"---\n{plan_output}\n---\n"
        ) if plan_output else ""

        if state["is_new"]:
            prompt = NEW_PROJECT_PROMPT.format(
                name=project.name,
                description=state.get("description", ""),
                db_section=db_section,
                claude_md_rules=CLAUDE_MD_RULES_NEW,
            ) + plan_section
            resume = False
        else:
            prompt = CONTINUE_PROJECT_PROMPT.format(
                name=project.name,
                task=state["task"],
                db_section=db_section,
                claude_md_rules=CLAUDE_MD_RULES_CONTINUE,
            ) + plan_section
            resume = True

        logger.info(f"[code] '{project.name}' coder 실행 중… resume={resume} session={project.session_id[:8]}…")
        result = await run_claude(
            prompt,
            cwd=project_dir,
            session_id=project.session_id,
            resume=resume,
        )
        if result.is_error:
            logger.error(f"[code] '{project.name}' coder 실패: {result.text[:300]}")
        else:
            logger.info(f"[code] '{project.name}' coder 완료 ({len(result.text)}자)")
        return {
            "cli_result": result,
            "status": "cli_error" if result.is_error else "coded",
        }

    async def review(state: WorkflowState) -> dict:
        """(sub-agent) 코드 리뷰. 비활성이면 noop."""
        if not _agents_on(state):
            return {"review_passed": True}
        cli = state.get("cli_result")
        if cli is None or cli.is_error:
            return {"review_passed": True}  # 코딩 실패 시 리뷰 건너뜀

        project: Project = state["project"]
        project_dir = os.path.join(state["projects_dir"], project.name)

        prompt = REVIEW_PROMPT.format(name=project.name)
        logger.info(f"[review] '{project.name}' reviewer 실행 중…")
        result = await run_claude(
            prompt, cwd=project_dir, ephemeral=True, timeout=sa_cfg.review_timeout,
        )
        if result.is_error:
            logger.warning(f"[review] '{project.name}' reviewer 실패: {result.text[:200]}")
            return {"review_passed": True, "review_output": ""}

        text = result.text.strip()
        passed = text.upper().startswith("LGTM")
        logger.info(f"[review] '{project.name}' 결과={'LGTM' if passed else 'ISSUES'}")
        return {"review_output": text, "review_passed": passed}

    async def fix(state: WorkflowState) -> dict:
        """(sub-agent) 리뷰 이슈 수정. LGTM 이면 noop."""
        if not _agents_on(state) or state.get("review_passed", True):
            return {}

        project: Project = state["project"]
        project_dir = os.path.join(state["projects_dir"], project.name)

        logger.info(f"[fix] '{project.name}' fixer 실행 중…")
        prompt = FIX_PROMPT.format(
            name=project.name,
            review_output=state.get("review_output", ""),
        )
        result = await run_claude(
            prompt,
            cwd=project_dir,
            session_id=project.session_id,
            resume=True,
            timeout=sa_cfg.fix_timeout,
        )
        if result.is_error:
            logger.error(f"[fix] '{project.name}' fixer 실패: {result.text[:200]}")
        else:
            logger.info(f"[fix] '{project.name}' fixer 완료")
        return {
            "fix_result": result,
            "status": "fix_error" if result.is_error else "fixed",
        }

    async def deploy(state: WorkflowState) -> dict:
        cli = state.get("fix_result") or state.get("cli_result")
        if cli is None or cli.is_error:
            logger.info(f"[deploy] '{state.get('project_name', '?')}' CLI 오류로 배포 건너뜀")
            return {"deployed": False, "deploy_output": "CLI 오류로 배포 건너뜀"}

        project: Project = state["project"]
        project_dir = os.path.join(state["projects_dir"], project.name)
        compose = os.path.join(project_dir, "docker-compose.yml")
        if not os.path.isfile(compose):
            logger.info(f"[deploy] '{project.name}' docker-compose.yml 없음 — 스킵")
            return {
                "deployed": False,
                "deploy_output": "docker-compose.yml 없음 — 배포 건너뜀",
                "status": "no_compose",
            }

        logger.info(f"[deploy] '{project.name}' docker compose up -d --build 실행 중…")
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose",
            "-f", compose,
            "-p", project.name,
            "up", "-d", "--build",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=deploy_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.error(f"[deploy] '{project.name}' 시간 초과 ({deploy_timeout}s)")
            return {
                "deployed": False,
                "deploy_output": f"배포 시간 초과 ({deploy_timeout}s)",
                "status": "deploy_timeout",
            }

        output = (stdout + b"\n" + stderr).decode(errors="replace").strip()
        deployed = proc.returncode == 0
        if deployed:
            logger.info(f"[deploy] '{project.name}' 배포 성공")
        else:
            logger.error(f"[deploy] '{project.name}' 배포 실패 (exit {proc.returncode}): {output[:300]}")
        return {
            "deployed": deployed,
            "deploy_output": output,
            "status": "deployed" if deployed else "deploy_failed",
        }

    async def github_sync(state: WorkflowState) -> dict:
        """코드 변경이 있으면 init/commit/push. CLI 가 실패했으면 스킵."""
        if not gh_cfg.enabled:
            return {}
        project: Optional[Project] = state.get("project")
        if project is None or not project.repo_url:
            return {}
        cli = state.get("cli_result")
        if cli is None or cli.is_error:
            logger.info(f"[github_sync] CLI 오류로 push 스킵")
            return {"github_output": (state.get("github_output") or "") + " | CLI 오류로 push 스킵"}
        logger.info(f"[github_sync] '{project.name}' commit + push 중…")

        project_dir = os.path.join(state["projects_dir"], project.name)
        clone_url = _to_clone_url(project.repo_url)

        init = await github_exec.ensure_git_initialized(
            project_dir,
            clone_url,
            token=gh_cfg.token,
            user_name=gh_cfg.user_name,
            user_email=gh_cfg.user_email,
        )
        if not init.ok:
            return {"github_output": f"git init 실패: {_tail(init.output, 200)}"}

        if state["is_new"]:
            msg = f"Initial scaffold: {state.get('description', '')[:100]}"
        else:
            msg = state["task"][:120] or "Update"
        push = await github_exec.commit_and_push(project_dir, msg)
        prefix = (state.get("github_output") or "").strip()
        summary = (
            f"{prefix + ' | ' if prefix else ''}"
            f"{'pushed' if push.ok else 'push 실패'}: {_tail(push.output, 200)}"
        )
        return {"github_pushed": push.ok, "github_output": summary}

    async def persist(state: WorkflowState) -> dict:
        project: Optional[Project] = state.get("project")
        if project is None:
            return {}
        cli = state.get("cli_result")
        if state["is_new"]:
            task_text = f"[생성] {state.get('description', '')}"
        else:
            task_text = state["task"]
        result_text = (cli.text if cli else "") or ""
        await registry.record_task(
            project.name,
            task_text,
            result_text,
            bool(state.get("deployed", False)),
        )
        return {}

    def route_after_load(state: WorkflowState) -> str:
        return END if state.get("error") else "github_init"

    def route_after_db(state: WorkflowState) -> str:
        # DB 프로비저닝 실패는 치명적 — 바로 persist 로 가서 기록 후 종료
        return "persist" if state.get("error") else "plan"

    g = StateGraph(WorkflowState)
    g.add_node("load", load)
    g.add_node("github_init", github_init)
    g.add_node("provision_db", provision_db)
    g.add_node("plan", plan)
    g.add_node("code", code)
    g.add_node("review", review)
    g.add_node("fix", fix)
    g.add_node("deploy", deploy)
    g.add_node("github_sync", github_sync)
    g.add_node("persist", persist)

    g.add_edge(START, "load")
    g.add_conditional_edges(
        "load", route_after_load, {"github_init": "github_init", END: END}
    )
    g.add_edge("github_init", "provision_db")
    g.add_conditional_edges(
        "provision_db", route_after_db, {"plan": "plan", "persist": "persist"}
    )
    g.add_edge("plan", "code")
    g.add_edge("code", "review")
    g.add_edge("review", "fix")
    g.add_edge("fix", "deploy")
    g.add_edge("deploy", "github_sync")
    g.add_edge("github_sync", "persist")
    g.add_edge("persist", END)

    return g.compile()


def format_workflow_result(state: WorkflowState) -> str:
    """워크플로 최종 state 를 사용자에게 보여줄 메시지로 변환."""
    if err := state.get("error"):
        return f"❌ {err}"

    cli = state.get("cli_result")
    parts: list[str] = []
    project = state.get("project")
    if project:
        header = f"📦 {project.name}"
        if getattr(project, "repo_url", None):
            header += f"\n{project.repo_url}"
        parts.append(header)

    if cli:
        if cli.is_error:
            parts.append("⚠️ CLI 오류")
        parts.append(cli.text.strip() or "(빈 응답)")

    deploy_output = state.get("deploy_output", "")
    if state.get("deployed"):
        parts.append(f"✅ 배포 성공\n{_tail(deploy_output, 400)}")
    elif deploy_output:
        parts.append(f"ℹ️ {deploy_output.splitlines()[0] if deploy_output else ''}")

    gh_output = state.get("github_output")
    if gh_output:
        icon = "✅" if state.get("github_pushed") else "ℹ️"
        parts.append(f"{icon} GitHub: {gh_output}")

    review_output = state.get("review_output")
    if review_output:
        passed = state.get("review_passed", True)
        icon = "✅" if passed else "🔧"
        parts.append(f"{icon} 리뷰: {_tail(review_output, 300)}")
    fix = state.get("fix_result")
    if fix and not fix.is_error:
        parts.append(f"🔧 수정 완료: {_tail(fix.text, 200)}")

    db_output = state.get("db_output")
    if db_output:
        parts.append(f"🗄 {db_output}")

    text = "\n\n".join(p for p in parts if p)
    return _truncate(text, 3500)


# ── 유틸 ────────────────────────────────────────────────────


def _to_clone_url(html_url: str) -> str:
    """https://github.com/x/y → https://github.com/x/y.git"""
    if html_url.endswith(".git"):
        return html_url
    return html_url.rstrip("/") + ".git"


def _tail(s: str, n: int) -> str:
    return s if len(s) <= n else "…\n" + s[-n:]


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n // 2] + "\n\n... (중략) ...\n\n" + s[-n // 2 :]
