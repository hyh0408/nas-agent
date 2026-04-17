"""LangGraph 워크플로 end-to-end (mock 으로 CLI / docker subprocess 격리)."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from executor import workflow as wf
from executor import github_exec, mysql_exec
from executor.claude_exec import ClaudeResult
from executor.github_exec import GitResult, RepoInfo
from executor.mysql_exec import DBCredentials, MySQLError
from executor.projects import ProjectRegistry


@pytest.fixture
def registry(tmp_path):
    return ProjectRegistry(str(tmp_path / "registry.db"))


@pytest.fixture
def projects_dir(tmp_path):
    d = tmp_path / "projects"
    d.mkdir()
    return str(d)


class _FakeDockerProc:
    def __init__(self, stdout=b"", stderr=b"", rc=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = rc

    async def communicate(self):
        return (self._stdout, self._stderr)

    def kill(self):
        pass


def _patch_claude(result: ClaudeResult):
    async def fake(*args, **kwargs):
        return result
    return patch.object(wf, "run_claude", side_effect=fake)


def _patch_docker(stdout=b"built", rc=0):
    async def fake_exec(*args, **kwargs):
        return _FakeDockerProc(stdout=stdout, rc=rc)
    return patch.object(wf.asyncio, "create_subprocess_exec", side_effect=fake_exec)


async def test_new_project_happy_path(registry, projects_dir):
    graph = wf.build_workflow(registry)

    # claude 가 compose 파일을 만들었다고 가정
    project_name = "myapp"
    project_dir = os.path.join(projects_dir, project_name)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    claude_res = ClaudeResult(session_id="sess-1", text="만들었어요", is_error=False, raw={})

    with _patch_claude(claude_res), _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name,
            "task": "",
            "is_new": True,
            "description": "todo api",
            "projects_dir": projects_dir,
        })

    assert state["project"].name == project_name
    assert state["cli_result"].text == "만들었어요"
    assert state["deployed"] is True

    # 레지스트리에 프로젝트와 태스크 기록됨
    persisted = await registry.get(project_name)
    assert persisted is not None
    history = await registry.history(project_name)
    assert len(history) == 1
    assert history[0].deployed is True


async def test_continue_project_uses_existing_session(registry, projects_dir):
    graph = wf.build_workflow(registry)
    project = await registry.create("myapp", "initial")
    # compose 이미 있음
    project_dir = os.path.join(projects_dir, "myapp")
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    captured = {}

    async def fake_claude(prompt, *, cwd, session_id, resume, **kwargs):
        captured["prompt"] = prompt
        captured["session_id"] = session_id
        captured["resume"] = resume
        return ClaudeResult(session_id=session_id, text="이어서 했음", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=fake_claude), _patch_docker():
        state = await graph.ainvoke({
            "project_name": "myapp",
            "task": "로그인 추가",
            "is_new": False,
            "description": "",
            "projects_dir": projects_dir,
        })

    assert captured["session_id"] == project.session_id
    assert captured["resume"] is True
    assert "로그인 추가" in captured["prompt"]
    assert state["deployed"] is True


async def test_missing_project_on_continue_short_circuits(registry, projects_dir):
    graph = wf.build_workflow(registry)

    async def should_not_run(*args, **kwargs):
        raise AssertionError("claude 노드가 실행되면 안 됨")

    with patch.object(wf, "run_claude", side_effect=should_not_run):
        state = await graph.ainvoke({
            "project_name": "missing",
            "task": "do stuff",
            "is_new": False,
            "description": "",
            "projects_dir": projects_dir,
        })

    assert state.get("error")
    assert "찾을 수 없습니다" in state["error"]


async def test_cli_error_skips_deploy(registry, projects_dir):
    graph = wf.build_workflow(registry)
    await registry.create("myapp", "initial")

    err_res = ClaudeResult(session_id="s", text="터졌어요", is_error=True, raw={})

    docker_called = {"called": False}

    async def fake_exec(*args, **kwargs):
        docker_called["called"] = True
        return _FakeDockerProc()

    async def fake_claude(*a, **k):
        return err_res

    with patch.object(wf, "run_claude", side_effect=fake_claude):
        with patch.object(wf.asyncio, "create_subprocess_exec", side_effect=fake_exec):
            state = await graph.ainvoke({
                "project_name": "myapp",
                "task": "something",
                "is_new": False,
                "description": "",
                "projects_dir": projects_dir,
            })

    assert state["deployed"] is False
    assert docker_called["called"] is False
    # 실패한 태스크도 히스토리에는 기록되어야 함 (deployed=False)
    history = await registry.history("myapp")
    assert len(history) == 1
    assert history[0].deployed is False


async def test_missing_compose_skips_deploy_but_records_task(registry, projects_dir):
    graph = wf.build_workflow(registry)
    await registry.create("myapp", "initial")

    claude_res = ClaudeResult(session_id="s", text="compose 는 안 만듦", is_error=False, raw={})

    docker_called = {"called": False}

    async def fake_exec(*args, **kwargs):
        docker_called["called"] = True
        return _FakeDockerProc()

    with _patch_claude(claude_res):
        with patch.object(wf.asyncio, "create_subprocess_exec", side_effect=fake_exec):
            state = await graph.ainvoke({
                "project_name": "myapp",
                "task": "docs only",
                "is_new": False,
                "description": "",
                "projects_dir": projects_dir,
            })

    assert docker_called["called"] is False
    assert state["deployed"] is False
    assert "없음" in state["deploy_output"]


async def test_duplicate_new_project_returns_error(registry, projects_dir):
    graph = wf.build_workflow(registry)
    await registry.create("dup", "first")

    state = await graph.ainvoke({
        "project_name": "dup",
        "task": "",
        "is_new": True,
        "description": "second attempt",
        "projects_dir": projects_dir,
    })
    assert "이미 존재" in state.get("error", "")


def test_format_workflow_result_handles_error():
    text = wf.format_workflow_result({"error": "boom"})
    assert "boom" in text


def test_format_workflow_result_deployed():
    text = wf.format_workflow_result({
        "project": type("P", (), {"name": "myapp", "repo_url": None})(),
        "cli_result": ClaudeResult(session_id="s", text="만들었어요", is_error=False, raw={}),
        "deploy_output": "Container myapp Started",
        "deployed": True,
    })
    assert "myapp" in text
    assert "만들었어요" in text
    assert "배포 성공" in text


def test_format_workflow_result_shows_repo_and_github():
    text = wf.format_workflow_result({
        "project": type("P", (), {"name": "myapp", "repo_url": "https://github.com/me/myapp"})(),
        "cli_result": ClaudeResult(session_id="s", text="ok", is_error=False, raw={}),
        "deploy_output": "",
        "deployed": False,
        "github_output": "pushed: main -> main",
        "github_pushed": True,
    })
    assert "https://github.com/me/myapp" in text
    assert "GitHub" in text


# ── GitHub 연동 경로 ──────────────────────────────────────────


async def test_new_project_with_github_creates_repo_and_pushes(registry, projects_dir):
    gh_cfg = wf.GitHubConfig(token="tok", owner="", user_name="N", user_email="e@x")
    graph = wf.build_workflow(registry, gh_cfg)

    project_name = "myapp"
    project_dir = os.path.join(projects_dir, project_name)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    repo = RepoInfo(
        html_url="https://github.com/me/myapp",
        clone_url="https://github.com/me/myapp.git",
        full_name="me/myapp",
    )

    async def fake_create(*a, **k):
        return repo

    async def fake_ensure(*a, **k):
        return GitResult(True, "init ok")

    async def fake_commit(*a, **k):
        return GitResult(True, "pushed main -> main")

    async def fake_claude(*a, **k):
        return ClaudeResult(session_id="s", text="만들었어요", is_error=False, raw={})

    with patch.object(github_exec, "create_repo", side_effect=fake_create), \
         patch.object(github_exec, "ensure_git_initialized", side_effect=fake_ensure), \
         patch.object(github_exec, "commit_and_push", side_effect=fake_commit), \
         patch.object(wf, "run_claude", side_effect=fake_claude), \
         _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name,
            "task": "",
            "is_new": True,
            "description": "todo api",
            "projects_dir": projects_dir,
        })

    assert state["project"].repo_url == repo.html_url
    assert state["github_pushed"] is True
    assert "pushed" in state["github_output"]

    persisted = await registry.get(project_name)
    assert persisted.repo_url == repo.html_url


async def test_github_disabled_skips_all_github_nodes(registry, projects_dir):
    """GITHUB_TOKEN 없으면 github_init / github_sync 모두 no-op."""
    gh_cfg = wf.GitHubConfig(token="")  # 비활성
    graph = wf.build_workflow(registry, gh_cfg)

    project_name = "myapp"
    project_dir = os.path.join(projects_dir, project_name)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    async def fake_claude(*a, **k):
        return ClaudeResult(session_id="s", text="ok", is_error=False, raw={})

    with patch.object(github_exec, "create_repo", side_effect=AssertionError("must not call")), \
         patch.object(github_exec, "ensure_git_initialized", side_effect=AssertionError("must not call")), \
         patch.object(github_exec, "commit_and_push", side_effect=AssertionError("must not call")), \
         patch.object(wf, "run_claude", side_effect=fake_claude), \
         _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name,
            "task": "",
            "is_new": True,
            "description": "x",
            "projects_dir": projects_dir,
        })

    assert state["deployed"] is True
    assert state.get("github_pushed") is None or state.get("github_pushed") is False
    assert not state.get("project").repo_url


async def test_github_init_failure_still_runs_workflow(registry, projects_dir):
    """GitHub repo 생성이 실패해도 claude/deploy 는 계속 진행."""
    gh_cfg = wf.GitHubConfig(token="tok")
    graph = wf.build_workflow(registry, gh_cfg)

    project_name = "myapp"
    project_dir = os.path.join(projects_dir, project_name)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    async def fake_create(*a, **k):
        raise github_exec.GitHubError("API rate limit")

    async def fake_claude(*a, **k):
        return ClaudeResult(session_id="s", text="ok", is_error=False, raw={})

    with patch.object(github_exec, "create_repo", side_effect=fake_create), \
         patch.object(github_exec, "ensure_git_initialized", side_effect=AssertionError("skipped")), \
         patch.object(github_exec, "commit_and_push", side_effect=AssertionError("skipped")), \
         patch.object(wf, "run_claude", side_effect=fake_claude), \
         _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name,
            "task": "",
            "is_new": True,
            "description": "x",
            "projects_dir": projects_dir,
        })

    assert state["deployed"] is True
    assert "API rate limit" in state["github_output"]
    # repo_url 이 설정되지 않았으므로 github_sync 도 스킵됨 (assert 안 걸림)


async def test_new_project_prompt_requires_claude_md(registry, projects_dir):
    graph = wf.build_workflow(registry)

    project_name = "myapp"
    project_dir = os.path.join(projects_dir, project_name)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    captured = {}

    async def fake_claude(prompt, **kwargs):
        captured["p"] = prompt
        return ClaudeResult(session_id="s", text="ok", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=fake_claude), _patch_docker():
        await graph.ainvoke({
            "project_name": project_name,
            "task": "",
            "is_new": True,
            "description": "할일 API",
            "projects_dir": projects_dir,
        })

    p = captured["p"]
    assert "CLAUDE.md" in p
    assert "원본 요구사항" in p
    assert "변경 이력" in p


async def test_continue_project_prompt_updates_claude_md(registry, projects_dir):
    graph = wf.build_workflow(registry)
    await registry.create("myapp", "x")
    project_dir = os.path.join(projects_dir, "myapp")
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    captured = {}

    async def fake_claude(prompt, **kwargs):
        captured["p"] = prompt
        return ClaudeResult(session_id="s", text="ok", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=fake_claude), _patch_docker():
        await graph.ainvoke({
            "project_name": "myapp",
            "task": "로그인 추가",
            "is_new": False,
            "description": "",
            "projects_dir": projects_dir,
        })

    p = captured["p"]
    assert "CLAUDE.md" in p
    assert "업데이트" in p
    assert "변경 이력" in p


async def test_new_project_with_db_provisions_and_feeds_prompt(registry, projects_dir):
    my_cfg = wf.MySQLConfig(root_password="rootpw", host="nas-mysql")
    graph = wf.build_workflow(registry, wf.GitHubConfig(), my_cfg)

    project_name = "myapp"
    project_dir = os.path.join(projects_dir, project_name)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    creds = DBCredentials(
        host="nas-mysql", port=3306,
        database="proj_myapp", user="proj_myapp", password="secret123",
    )

    captured_prompt = {}

    async def fake_provision(name, **kwargs):
        return creds

    async def fake_claude(prompt, **kwargs):
        captured_prompt["p"] = prompt
        return ClaudeResult(session_id="s", text="ok", is_error=False, raw={})

    with patch.object(mysql_exec, "provision", side_effect=fake_provision), \
         patch.object(wf, "run_claude", side_effect=fake_claude), \
         _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name,
            "task": "",
            "is_new": True,
            "description": "블로그",
            "db_required": True,
            "projects_dir": projects_dir,
        })

    # 프롬프트에 DB 정보가 실렸다
    p = captured_prompt["p"]
    assert "proj_myapp" in p
    assert "secret123" in p
    assert "nas-agent-shared" in p

    # 레지스트리에도 저장
    persisted = await registry.get(project_name)
    assert persisted.db_name == "proj_myapp"
    assert persisted.db_user == "proj_myapp"
    assert persisted.db_password == "secret123"
    assert state["db_credentials"].database == "proj_myapp"


async def test_db_required_without_mysql_config_errors(registry, projects_dir):
    graph = wf.build_workflow(registry, wf.GitHubConfig(), wf.MySQLConfig(root_password=""))

    async def must_not_call(*a, **k):
        raise AssertionError("CLI 호출되면 안 됨")

    with patch.object(wf, "run_claude", side_effect=must_not_call):
        state = await graph.ainvoke({
            "project_name": "myapp",
            "task": "",
            "is_new": True,
            "description": "x",
            "db_required": True,
            "projects_dir": projects_dir,
        })

    assert "MySQL" in state.get("error", "")


async def test_continue_project_reloads_existing_db_credentials(registry, projects_dir):
    my_cfg = wf.MySQLConfig(root_password="pw", host="nas-mysql")
    graph = wf.build_workflow(registry, wf.GitHubConfig(), my_cfg)

    p = await registry.create("myapp", "x")
    await registry.set_db_info("myapp", "proj_myapp", "proj_myapp", "stored-pw")

    project_dir = os.path.join(projects_dir, "myapp")
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    captured = {}

    async def fake_claude(prompt, **kwargs):
        captured["p"] = prompt
        return ClaudeResult(session_id="s", text="ok", is_error=False, raw={})

    async def must_not_provision(*a, **k):
        raise AssertionError("계속 작업에서는 신규 프로비저닝 안 됨")

    with patch.object(mysql_exec, "provision", side_effect=must_not_provision), \
         patch.object(wf, "run_claude", side_effect=fake_claude), \
         _patch_docker():
        state = await graph.ainvoke({
            "project_name": "myapp",
            "task": "추가 기능",
            "is_new": False,
            "description": "",
            "db_required": False,  # continue 에서는 기존 DB 가 있으면 자동으로 실림
            "projects_dir": projects_dir,
        })

    assert "proj_myapp" in captured["p"]
    assert "stored-pw" in captured["p"]
    assert state["db_credentials"].password == "stored-pw"


async def test_github_sync_skipped_when_cli_errors(registry, projects_dir):
    """CLI 가 실패하면 push 하지 않는다."""
    gh_cfg = wf.GitHubConfig(token="tok")
    graph = wf.build_workflow(registry, gh_cfg)

    p = await registry.create("myapp", "x")
    await registry.set_repo_url("myapp", "https://github.com/me/myapp")

    err_res = ClaudeResult(session_id="s", text="망했어요", is_error=True, raw={})

    async def fake_claude(*a, **k):
        return err_res

    with patch.object(github_exec, "create_repo", side_effect=AssertionError("new only")), \
         patch.object(github_exec, "ensure_git_initialized", side_effect=AssertionError("must skip")), \
         patch.object(github_exec, "commit_and_push", side_effect=AssertionError("must skip")), \
         patch.object(wf, "run_claude", side_effect=fake_claude):
        state = await graph.ainvoke({
            "project_name": "myapp",
            "task": "do",
            "is_new": False,
            "description": "",
            "projects_dir": projects_dir,
        })

    assert state["deployed"] is False
    assert "CLI 오류" in state["github_output"]


# ── Sub-agent 경로 ────────────────────────────────────────────


async def test_sub_agents_disabled_skips_plan_review_fix(registry, projects_dir):
    """기본값(disabled) 에서 plan/review/fix 는 noop. 기존 동작과 동일."""
    sa_cfg = wf.SubAgentConfig(enabled=False)
    graph = wf.build_workflow(registry, sub_agents=sa_cfg)
    project_name = "myapp"
    _setup_compose(projects_dir, project_name)

    async def fake_code(prompt, **kwargs):
        return ClaudeResult(session_id="s", text="ok", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=fake_code), _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name, "task": "", "is_new": True,
            "description": "x", "projects_dir": projects_dir,
        })

    assert state["cli_result"].text == "ok"
    assert state.get("review_passed") is True
    assert state.get("plan_output") in (None, "", {})
    assert state["deployed"] is True


async def test_sub_agents_enabled_runs_plan_code_review_lgtm(registry, projects_dir):
    """LGTM 리뷰 → fix 스킵."""
    sa_cfg = wf.SubAgentConfig(enabled=True)
    graph = wf.build_workflow(registry, sub_agents=sa_cfg)
    project_name = "myapp"
    _setup_compose(projects_dir, project_name)

    call_log = []

    async def multi_claude(prompt, *, cwd, ephemeral=False, timeout=900, **kwargs):
        if "계획만 출력" in prompt:
            call_log.append("plan")
            return ClaudeResult(session_id="", text="1. FastAPI 사용\n2. main.py 생성", is_error=False, raw={})
        if "코드를 리뷰" in prompt:
            call_log.append("review")
            return ClaudeResult(session_id="", text="LGTM — 깔끔합니다", is_error=False, raw={})
        call_log.append("code")
        return ClaudeResult(session_id="s", text="코드 생성 완료", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=multi_claude), _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name, "task": "", "is_new": True,
            "description": "FastAPI 앱", "projects_dir": projects_dir,
        })

    assert call_log == ["plan", "code", "review"]
    assert "FastAPI 사용" in state["plan_output"]
    assert state["review_passed"] is True
    assert state["cli_result"].text == "코드 생성 완료"
    assert state["deployed"] is True


async def test_sub_agents_enabled_review_issues_triggers_fix(registry, projects_dir):
    """리뷰 이슈 발견 → fix 실행."""
    sa_cfg = wf.SubAgentConfig(enabled=True)
    graph = wf.build_workflow(registry, sub_agents=sa_cfg)
    project_name = "myapp"
    _setup_compose(projects_dir, project_name)

    call_log = []

    async def multi_claude(prompt, *, cwd, ephemeral=False, timeout=900, **kwargs):
        if "계획만 출력" in prompt:
            call_log.append("plan")
            return ClaudeResult(session_id="", text="계획", is_error=False, raw={})
        if "코드를 리뷰" in prompt:
            call_log.append("review")
            return ClaudeResult(session_id="", text="1. port 가 하드코딩됨\n2. 에러핸들링 누락", is_error=False, raw={})
        if "리뷰어가 다음 문제를 발견" in prompt:
            call_log.append("fix")
            return ClaudeResult(session_id="s", text="수정 완료", is_error=False, raw={})
        call_log.append("code")
        return ClaudeResult(session_id="s", text="코드 생성", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=multi_claude), _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name, "task": "", "is_new": True,
            "description": "앱", "projects_dir": projects_dir,
        })

    assert call_log == ["plan", "code", "review", "fix"]
    assert state["review_passed"] is False
    assert state.get("fix_result").text == "수정 완료"
    assert state["deployed"] is True


async def test_sub_agents_plan_failure_still_proceeds(registry, projects_dir):
    """플래너 실패해도 코딩은 진행된다."""
    sa_cfg = wf.SubAgentConfig(enabled=True)
    graph = wf.build_workflow(registry, sub_agents=sa_cfg)
    project_name = "myapp"
    _setup_compose(projects_dir, project_name)

    async def multi_claude(prompt, *, cwd, ephemeral=False, timeout=900, **kwargs):
        if "계획만 출력" in prompt:
            return ClaudeResult(session_id="", text="에러남", is_error=True, raw={})
        if "코드를 리뷰" in prompt:
            return ClaudeResult(session_id="", text="LGTM", is_error=False, raw={})
        return ClaudeResult(session_id="s", text="그래도 만듦", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=multi_claude), _patch_docker():
        state = await graph.ainvoke({
            "project_name": project_name, "task": "", "is_new": True,
            "description": "앱", "projects_dir": projects_dir,
        })

    assert state["plan_output"] == ""
    assert state["cli_result"].text == "그래도 만듦"
    assert state["deployed"] is True


async def test_sub_agents_continue_project_uses_resume(registry, projects_dir):
    """이어작업 시에도 sub-agent 작동, coder 는 resume=True."""
    sa_cfg = wf.SubAgentConfig(enabled=True)
    graph = wf.build_workflow(registry, sub_agents=sa_cfg)
    p = await registry.create("myapp", "x")
    _setup_compose(projects_dir, "myapp")

    captured = {}

    async def multi_claude(prompt, *, cwd, session_id=None, resume=False, ephemeral=False, **kwargs):
        if "계획만 출력" in prompt:
            assert ephemeral is True
            return ClaudeResult(session_id="", text="계획", is_error=False, raw={})
        if "코드를 리뷰" in prompt:
            assert ephemeral is True
            return ClaudeResult(session_id="", text="LGTM", is_error=False, raw={})
        # coder
        captured["resume"] = resume
        captured["session_id"] = session_id
        return ClaudeResult(session_id=session_id, text="이어서 했음", is_error=False, raw={})

    with patch.object(wf, "run_claude", side_effect=multi_claude), _patch_docker():
        state = await graph.ainvoke({
            "project_name": "myapp", "task": "로그인", "is_new": False,
            "description": "", "projects_dir": projects_dir,
        })

    assert captured["resume"] is True
    assert captured["session_id"] == p.session_id


# ── 헬퍼 ────────────────────────────────────────────────────


def _setup_compose(projects_dir, name):
    d = os.path.join(projects_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "docker-compose.yml"), "w") as f:
        f.write("services: {}")
