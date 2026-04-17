"""claude_exec 래퍼 테스트 — 실제 CLI 를 호출하지 않고 서브프로세스를 mock 한다."""

import asyncio
import json
from unittest.mock import patch

import pytest

from executor import claude_exec


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", rc: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = rc

    async def communicate(self):
        return (self._stdout, self._stderr)

    def kill(self):
        pass


def _ok_payload(session_id="sess-123", result="done"):
    return json.dumps({
        "session_id": session_id,
        "result": result,
        "is_error": False,
    }).encode()


async def test_new_session_uses_session_id_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _FakeProc(stdout=_ok_payload(session_id="new-sess"))

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await claude_exec.run_claude(
            "hello", cwd=str(tmp_path), session_id="new-sess", resume=False
        )

    args = captured["args"]
    assert "--session-id" in args
    assert args[args.index("--session-id") + 1] == "new-sess"
    assert "--resume" not in args
    assert "--dangerously-skip-permissions" in args
    assert result.session_id == "new-sess"
    assert result.text == "done"
    assert result.is_error is False


async def test_resume_uses_resume_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(stdout=_ok_payload(session_id="old-sess", result="continued"))

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await claude_exec.run_claude(
            "continue", cwd=str(tmp_path), session_id="old-sess", resume=True
        )

    args = captured["args"]
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "old-sess"
    assert "--session-id" not in args
    assert result.text == "continued"


async def test_api_key_stripped_from_subprocess_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("KEEP_ME", "yes")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc(stdout=_ok_payload())

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        await claude_exec.run_claude("x", cwd=str(tmp_path))

    env = captured["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert env["KEEP_ME"] == "yes"


async def test_timeout_returns_error_result(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _Hanging:
        returncode = None
        def communicate(self):
            return asyncio.get_event_loop().create_future()
        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        return _Hanging()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "cancel"):
            coro.cancel()
        raise asyncio.TimeoutError()

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        with patch.object(claude_exec.asyncio, "wait_for", side_effect=fake_wait_for):
            result = await claude_exec.run_claude(
                "hi", cwd=str(tmp_path), session_id="s", timeout=1
            )

    assert result.is_error
    assert "시간 초과" in result.text


async def test_nonzero_exit_without_json_is_error(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout=b"", stderr=b"boom", rc=1)

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await claude_exec.run_claude("hi", cwd=str(tmp_path))

    assert result.is_error
    assert "boom" in result.text


async def test_is_error_flag_from_json_propagates(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = json.dumps({"session_id": "s", "result": "failed halfway", "is_error": True}).encode()

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout=payload, rc=0)

    with patch.object(claude_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await claude_exec.run_claude("hi", cwd=str(tmp_path))

    assert result.is_error is True
    assert result.text == "failed halfway"
