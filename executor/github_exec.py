"""GitHub 연동 헬퍼.

- REST API 로 repo 생성 (aiohttp)
- git CLI subprocess 로 init / commit / push
- GITHUB_TOKEN 이 비어 있으면 모든 함수가 no-op 로 동작 (gracefully skip)

토큰은 remote URL 에 임베드해서 별도 자격증명 헬퍼 없이 push 를 수행한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

import aiohttp

logger = logging.getLogger("github_exec")

GITHUB_API = "https://api.github.com"


@dataclass
class GitResult:
    ok: bool
    output: str


@dataclass
class RepoInfo:
    html_url: str       # 사람이 볼 수 있는 URL
    clone_url: str      # https://github.com/owner/repo.git
    full_name: str      # owner/repo


class GitHubError(Exception):
    pass


# ── Repo API ──────────────────────────────────────────────────

async def create_repo(
    name: str,
    description: str,
    token: str,
    *,
    private: bool = True,
    owner: Optional[str] = None,
    timeout: int = 30,
) -> RepoInfo:
    """GitHub 에 빈 repo 를 만든다. owner 가 주어지면 organization repo, 아니면 user repo."""
    url = f"{GITHUB_API}/orgs/{owner}/repos" if owner else f"{GITHUB_API}/user/repos"
    payload = {
        "name": name,
        "description": description[:350],  # GitHub description 한도 안전하게 자름
        "private": private,
        "auto_init": False,
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url, headers=headers, data=json.dumps(payload)) as resp:
            body = await resp.text()
            if resp.status == 422 and "name already exists" in body:
                raise GitHubError(f"이미 존재하는 GitHub repo: {name}")
            if resp.status not in (200, 201):
                raise GitHubError(f"GitHub repo 생성 실패 ({resp.status}): {body[:300]}")
            data = json.loads(body)

    return RepoInfo(
        html_url=data["html_url"],
        clone_url=data["clone_url"],
        full_name=data["full_name"],
    )


async def delete_repo(
    repo_url: str,
    token: str,
    *,
    timeout: int = 30,
) -> None:
    """GitHub repo 를 삭제한다. repo_url 에서 owner/name 을 추출."""
    # https://github.com/owner/name → owner/name
    path = urlparse(repo_url).path.strip("/").rstrip(".git")
    if not path or "/" not in path:
        raise GitHubError(f"repo URL 에서 owner/name 을 추출할 수 없음: {repo_url}")

    url = f"{GITHUB_API}/repos/{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.delete(url, headers=headers) as resp:
            if resp.status == 204:
                return
            if resp.status == 404:
                return  # 이미 없음
            body = await resp.text()
            raise GitHubError(f"GitHub repo 삭제 실패 ({resp.status}): {body[:300]}")


# ── git CLI ────────────────────────────────────────────────────

def _remote_with_token(clone_url: str, token: str) -> str:
    """https://github.com/x/y.git → https://<token>@github.com/x/y.git"""
    parsed = urlparse(clone_url)
    if not parsed.scheme.startswith("http"):
        return clone_url
    netloc = f"x-access-token:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _run_git(*args: str, cwd: str, timeout: int = 60) -> GitResult:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return GitResult(False, f"git {args[0]} timed out")

    out = (stdout + b"\n" + stderr).decode(errors="replace").strip()
    return GitResult(proc.returncode == 0, out)


async def ensure_git_initialized(
    project_dir: str,
    clone_url: str,
    *,
    token: str,
    user_name: str,
    user_email: str,
) -> GitResult:
    """프로젝트 폴더에 .git 이 없으면 init + remote 설정. 있으면 remote 만 최신화."""
    os.makedirs(project_dir, exist_ok=True)
    git_dir = os.path.join(project_dir, ".git")
    remote = _remote_with_token(clone_url, token)

    if not os.path.isdir(git_dir):
        r = await _run_git("init", "-b", "main", cwd=project_dir)
        if not r.ok:
            return r
        r = await _run_git("remote", "add", "origin", remote, cwd=project_dir)
        if not r.ok:
            return r
    else:
        # remote 가 이미 있으면 URL 을 갱신, 없으면 추가
        r = await _run_git("remote", "set-url", "origin", remote, cwd=project_dir)
        if not r.ok:
            r = await _run_git("remote", "add", "origin", remote, cwd=project_dir)
            if not r.ok:
                return r

    # 로컬 user.name / user.email 설정
    for key, val in (("user.name", user_name), ("user.email", user_email)):
        r = await _run_git("config", key, val, cwd=project_dir)
        if not r.ok:
            return r

    return GitResult(True, "git initialized")


_NOTHING_TO_COMMIT = re.compile(r"nothing to commit", re.IGNORECASE)


async def commit_and_push(project_dir: str, message: str) -> GitResult:
    """add -A → commit → push. 변경 없으면 no-op 성공으로 처리."""
    r = await _run_git("add", "-A", cwd=project_dir)
    if not r.ok:
        return r

    # status --porcelain 가 비어 있으면 변경 없음
    status = await _run_git("status", "--porcelain", cwd=project_dir)
    if status.ok and not status.output.strip():
        return GitResult(True, "변경 없음 — 커밋 생략")

    r = await _run_git("commit", "-m", message, cwd=project_dir)
    if not r.ok:
        if _NOTHING_TO_COMMIT.search(r.output):
            return GitResult(True, "변경 없음 — 커밋 생략")
        return r

    r = await _run_git("push", "-u", "origin", "HEAD", cwd=project_dir, timeout=120)
    return r


# ── 공통 ───────────────────────────────────────────────────────

def is_enabled(token: Optional[str]) -> bool:
    return bool(token and token.strip())
