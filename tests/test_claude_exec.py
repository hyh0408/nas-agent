"""claude_exec 가 서브프로세스 env 에서 ANTHROPIC_API_KEY 를 제거하는지 검증."""

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from executor import claude_exec


class _FakeProc:
    def __init__(self):
        self.returncode = 0

    async def communicate(self):
        return (b"ok", b"")

    def kill(self):
        pass


async def test_api_key_stripped_from_subprocess_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("OTHER_VAR", "keep-me")

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        await claude_exec.run_claude("hello", work_dir=str(tmp_path))

    env = captured["env"]
    assert env is not None
    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("OTHER_VAR") == "keep-me"


async def test_cli_invoked_with_print_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc()

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        await claude_exec.run_claude("build me an app", work_dir=str(tmp_path))

    args = captured["args"]
    assert "-p" in args
    assert "build me an app" in args
    assert "--output-format" in args
    assert "text" in args


async def test_timeout_returns_korean_message(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _HangingProc:
        returncode = None

        def communicate(self):
            return asyncio.get_event_loop().create_future()  # never resolves

        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        return _HangingProc()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "cancel"):
            coro.cancel()
        raise asyncio.TimeoutError()

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        with patch.object(claude_exec.asyncio, "wait_for", side_effect=fake_wait_for):
            result = await claude_exec.run_claude("hi", work_dir=str(tmp_path))

    assert "시간 초과" in result


async def test_nonzero_returncode_surfaces_stderr(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _FailingProc:
        returncode = 1

        async def communicate(self):
            return (b"partial output", b"boom")

        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        return _FailingProc()

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await claude_exec.run_claude("hi", work_dir=str(tmp_path))

    assert "오류" in result
    assert "boom" in result
