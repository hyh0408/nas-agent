"""LangGraph 기반 프로젝트 개발 워크플로.

한 번의 사용자 요청 = 한 번의 그래프 실행. 노드:
  load         - 레지스트리에서 프로젝트 조회 또는 생성
  github_init  - (선택) 새 프로젝트일 때 GitHub repo 생성
  claude       - Claude CLI 로 코드 생성/수정 (세션 resume)
  deploy       - docker compose up -d --build 로 자동 배포
  github_sync  - (선택) 코드 변경사항을 git commit + push
  persist      - task 히스토리 기록
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from executor import github_exec
from executor.claude_exec import run_claude, ClaudeResult
from executor.github_exec import GitHubError, RepoInfo
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


class WorkflowState(TypedDict, total=False):
    # 입력
    project_name: str
    task: str
    is_new: bool
    description: str
    projects_dir: str

    # 중간 / 출력
    project: Optional[Project]
    cli_result: Optional[ClaudeResult]
    deploy_output: str
    deployed: bool
    github_output: str
    github_pushed: bool
    error: Optional[str]
    status: str


NEW_PROJECT_PROMPT = (
    "새 프로젝트 '{name}' 를 현재 디렉터리에 스캐폴딩합니다.\n"
    "설명: {description}\n\n"
    "작업 규칙:\n"
    "- 모든 파일을 현재 디렉터리 안에 배치\n"
    "- Dockerfile 과 docker-compose.yml 반드시 포함\n"
    "- docker-compose.yml 의 container_name 은 '{name}'\n"
    "- 포트 충돌이 없도록 합리적인 호스트 포트 선택\n"
    "- 작업이 끝나면 자동으로 docker compose 로 배포되므로 즉시 실행 가능한 상태여야 함\n"
    "- git 작업(add/commit/push) 은 자동 처리되므로 직접 실행하지 마세요\n"
    "- 마지막에 한국어로 변경 내역을 3~5줄로 요약"
)

CONTINUE_PROJECT_PROMPT = (
    "프로젝트 '{name}' 를 이어서 개발합니다.\n"
    "현재 디렉터리가 프로젝트 루트이며 이전 작업 세션이 복원되어 있습니다.\n\n"
    "요청: {task}\n\n"
    "작업 규칙:\n"
    "- 기존 파일 구조/스타일 유지\n"
    "- Dockerfile / docker-compose.yml 이 이미 있으면 그대로 사용하고 필요 시에만 수정\n"
    "- 작업이 끝나면 자동으로 재배포되므로 즉시 실행 가능한 상태여야 함\n"
    "- git 작업(add/commit/push) 은 자동 처리되므로 직접 실행하지 마세요\n"
    "- 마지막에 한국어로 변경 내역을 3~5줄로 요약"
)


def build_workflow(
    registry: ProjectRegistry,
    github: Optional[GitHubConfig] = None,
    *,
    deploy_timeout: int = 600,
):
    """레지스트리·GitHub 설정을 클로저로 바인딩한 컴파일된 그래프를 돌려준다."""
    gh_cfg = github or GitHubConfig()

    async def load(state: WorkflowState) -> dict:
        name = state["project_name"]
        try:
            existing = await registry.get(name)
            if state["is_new"]:
                if existing:
                    return {"error": f"이미 존재하는 프로젝트: {name}", "status": "error"}
                project = await registry.create(name, state.get("description", ""))
            else:
                if not existing:
                    return {
                        "error": f"프로젝트를 찾을 수 없습니다: {name}",
                        "status": "error",
                    }
                project = existing
        except ProjectError as e:
            return {"error": str(e), "status": "error"}
        return {"project": project, "status": "loaded"}

    async def github_init(state: WorkflowState) -> dict:
        """새 프로젝트일 때만 GitHub repo 를 만든다."""
        if not state["is_new"] or not gh_cfg.enabled:
            return {}
        project: Project = state["project"]
        try:
            repo: RepoInfo = await github_exec.create_repo(
                project.name,
                project.description or state.get("description", ""),
                gh_cfg.token,
                private=gh_cfg.private,
                owner=gh_cfg.owner or None,
            )
        except GitHubError as e:
            logger.warning(f"github_init 실패: {e}")
            return {"github_output": f"GitHub repo 생성 실패: {e}"}

        await registry.set_repo_url(project.name, repo.html_url)
        refreshed = await registry.get(project.name)
        return {"project": refreshed, "github_output": f"GitHub repo: {repo.html_url}"}

    async def claude(state: WorkflowState) -> dict:
        project: Project = state["project"]
        project_dir = os.path.join(state["projects_dir"], project.name)
        os.makedirs(project_dir, exist_ok=True)

        if state["is_new"]:
            prompt = NEW_PROJECT_PROMPT.format(
                name=project.name, description=state.get("description", "")
            )
            resume = False
        else:
            prompt = CONTINUE_PROJECT_PROMPT.format(
                name=project.name, task=state["task"]
            )
            resume = True

        result = await run_claude(
            prompt,
            cwd=project_dir,
            session_id=project.session_id,
            resume=resume,
        )
        return {
            "cli_result": result,
            "status": "cli_error" if result.is_error else "coded",
        }

    async def deploy(state: WorkflowState) -> dict:
        cli = state.get("cli_result")
        if cli is None or cli.is_error:
            return {"deployed": False, "deploy_output": "CLI 오류로 배포 건너뜀"}

        project: Project = state["project"]
        project_dir = os.path.join(state["projects_dir"], project.name)
        compose = os.path.join(project_dir, "docker-compose.yml")
        if not os.path.isfile(compose):
            return {
                "deployed": False,
                "deploy_output": "docker-compose.yml 없음 — 배포 건너뜀",
                "status": "no_compose",
            }

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
            return {
                "deployed": False,
                "deploy_output": f"배포 시간 초과 ({deploy_timeout}s)",
                "status": "deploy_timeout",
            }

        output = (stdout + b"\n" + stderr).decode(errors="replace").strip()
        deployed = proc.returncode == 0
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
            return {"github_output": (state.get("github_output") or "") + " | CLI 오류로 push 스킵"}

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

    g = StateGraph(WorkflowState)
    g.add_node("load", load)
    g.add_node("github_init", github_init)
    g.add_node("claude", claude)
    g.add_node("deploy", deploy)
    g.add_node("github_sync", github_sync)
    g.add_node("persist", persist)

    g.add_edge(START, "load")
    g.add_conditional_edges(
        "load", route_after_load, {"github_init": "github_init", END: END}
    )
    g.add_edge("github_init", "claude")
    g.add_edge("claude", "deploy")
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
