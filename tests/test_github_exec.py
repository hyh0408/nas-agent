"""github_exec 단위 테스트 — aiohttp / git subprocess 를 mock."""

from __future__ import annotations

import json
import os
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from executor import github_exec


# ── create_repo ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, response, get_response=None):
        self._response = response
        self._get_response = get_response or response
        self.calls = []

    def post(self, url, headers=None, data=None):
        self.calls.append({"method": "post", "url": url, "headers": headers, "data": data})
        return self._response

    def get(self, url, headers=None):
        self.calls.append({"method": "get", "url": url, "headers": headers})
        return self._get_response

    def delete(self, url, headers=None):
        self.calls.append({"method": "delete", "url": url, "headers": headers})
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_create_repo_for_user_hits_user_endpoint():
    body = json.dumps({
        "html_url": "https://github.com/me/myapp",
        "clone_url": "https://github.com/me/myapp.git",
        "full_name": "me/myapp",
    })
    session = _FakeSession(_FakeResponse(201, body))

    with patch.object(github_exec.aiohttp, "ClientSession", return_value=session):
        repo = await github_exec.create_repo("myapp", "desc", "tok")

    assert repo.full_name == "me/myapp"
    assert session.calls[0]["url"].endswith("/user/repos")
    sent = json.loads(session.calls[0]["data"])
    assert sent["name"] == "myapp"
    assert sent["private"] is True


async def test_create_repo_for_org_hits_org_endpoint():
    body = json.dumps({
        "html_url": "https://github.com/acme/myapp",
        "clone_url": "https://github.com/acme/myapp.git",
        "full_name": "acme/myapp",
    })
    session = _FakeSession(_FakeResponse(201, body))

    with patch.object(github_exec.aiohttp, "ClientSession", return_value=session):
        await github_exec.create_repo("myapp", "desc", "tok", owner="acme")

    assert session.calls[0]["url"].endswith("/orgs/acme/repos")


async def test_create_repo_existing_returns_info_via_get():
    """이미 존재하는 repo 면 get_repo 로 fallback 해서 정보를 돌려준다."""
    conflict_resp = _FakeResponse(422, '{"errors":[{"message":"name already exists"}]}')
    repo_resp = _FakeResponse(200, json.dumps({
        "html_url": "https://github.com/acme/dup",
        "clone_url": "https://github.com/acme/dup.git",
        "full_name": "acme/dup",
    }))

    # owner 를 명시하면 get_repo 가 /user 를 호출하지 않아 mock 이 단순해짐
    sessions = [
        _FakeSession(conflict_resp),             # create → 422
        _FakeSession(None, get_response=repo_resp),  # get /repos/acme/dup
    ]
    idx = {"i": 0}

    def fake_session(**kwargs):
        s = sessions[min(idx["i"], len(sessions) - 1)]
        idx["i"] += 1
        return s

    with patch.object(github_exec.aiohttp, "ClientSession", side_effect=fake_session):
        repo = await github_exec.create_repo("dup", "desc", "tok", owner="acme")

    assert repo.full_name == "acme/dup"
    assert repo.html_url == "https://github.com/acme/dup"


async def test_create_repo_other_failures_raise():
    session = _FakeSession(_FakeResponse(500, "kaboom"))
    with patch.object(github_exec.aiohttp, "ClientSession", return_value=session):
        with pytest.raises(github_exec.GitHubError):
            await github_exec.create_repo("x", "d", "tok")


# ── remote URL token embedding ───────────────────────────────


def test_remote_with_token_embeds_credentials():
    url = github_exec._remote_with_token("https://github.com/me/myapp.git", "abc123")
    assert url.startswith("https://x-access-token:abc123@github.com/")
    assert url.endswith("/me/myapp.git")


def test_remote_with_token_passes_through_ssh():
    # 비 https URL 은 그대로
    url = github_exec._remote_with_token("git@github.com:me/myapp.git", "abc")
    assert url == "git@github.com:me/myapp.git"


# ── git 서브프로세스 ──────────────────────────────────────────


class _FakeGitProc:
    def __init__(self, rc=0, out=b"", err=b""):
        self._out, self._err, self.returncode = out, err, rc

    async def communicate(self):
        return (self._out, self._err)

    def kill(self):
        pass


def _git_patch(scripts):
    """scripts: list of _FakeGitProc, consumed in order for each git invocation."""
    iterator = iter(scripts)
    invocations = []

    async def fake_exec(*args, **kwargs):
        invocations.append(args)
        return next(iterator)

    return patch.object(github_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec), invocations


async def test_ensure_git_initialized_runs_init_when_missing(tmp_path):
    project_dir = str(tmp_path / "p")
    # init, remote add, config name, config email — 4 calls
    scripts = [_FakeGitProc() for _ in range(4)]
    patcher, invocations = _git_patch(scripts)
    with patcher:
        r = await github_exec.ensure_git_initialized(
            project_dir,
            "https://github.com/me/p.git",
            token="tok",
            user_name="N",
            user_email="e@x",
        )
    assert r.ok
    # 첫 호출이 init
    assert invocations[0][1:3] == ("init", "-b")
    # 두 번째: remote add origin with token URL
    remote_args = invocations[1]
    assert remote_args[1:4] == ("remote", "add", "origin")
    assert "x-access-token:tok@github.com" in remote_args[4]


async def test_ensure_git_initialized_updates_remote_when_exists(tmp_path):
    project_dir = str(tmp_path / "p")
    os.makedirs(os.path.join(project_dir, ".git"))
    # remote set-url, config name, config email — 3 calls
    scripts = [_FakeGitProc() for _ in range(3)]
    patcher, invocations = _git_patch(scripts)
    with patcher:
        r = await github_exec.ensure_git_initialized(
            project_dir,
            "https://github.com/me/p.git",
            token="tok",
            user_name="N",
            user_email="e@x",
        )
    assert r.ok
    # init 은 호출되지 않아야 함
    assert invocations[0][1:4] == ("remote", "set-url", "origin")


async def test_commit_and_push_handles_no_changes(tmp_path):
    project_dir = str(tmp_path / "p")
    os.makedirs(project_dir)
    # add -A ok, status --porcelain returns empty → short-circuit
    scripts = [_FakeGitProc(), _FakeGitProc(out=b"")]
    patcher, _ = _git_patch(scripts)
    with patcher:
        r = await github_exec.commit_and_push(project_dir, "msg")
    assert r.ok
    assert "변경 없음" in r.output


async def test_commit_and_push_full_cycle(tmp_path):
    project_dir = str(tmp_path / "p")
    os.makedirs(project_dir)
    # add ok, status "M file.py", commit ok, push ok
    scripts = [
        _FakeGitProc(),
        _FakeGitProc(out=b" M file.py"),
        _FakeGitProc(),
        _FakeGitProc(out=b"To github.com:me/p.git\n  a..b main -> main"),
    ]
    patcher, invocations = _git_patch(scripts)
    with patcher:
        r = await github_exec.commit_and_push(project_dir, "add feature")
    assert r.ok
    assert invocations[2][1:3] == ("commit", "-m")
    assert invocations[2][3] == "add feature"
    assert invocations[3][1] == "push"


async def test_commit_and_push_propagates_commit_failure(tmp_path):
    project_dir = str(tmp_path / "p")
    os.makedirs(project_dir)
    scripts = [
        _FakeGitProc(),                      # add
        _FakeGitProc(out=b" M file.py"),     # status (dirty)
        _FakeGitProc(rc=1, err=b"commit broke unexpectedly"),  # commit fails
    ]
    patcher, _ = _git_patch(scripts)
    with patcher:
        r = await github_exec.commit_and_push(project_dir, "msg")
    assert not r.ok
    assert "broke" in r.output


def test_is_enabled():
    assert github_exec.is_enabled("x")
    assert not github_exec.is_enabled("")
    assert not github_exec.is_enabled(None)
    assert not github_exec.is_enabled("   ")
