"""mysql_exec 단위 테스트 — mysql CLI 서브프로세스를 mock."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from executor import mysql_exec
from executor.mysql_exec import MySQLError


class _FakeProc:
    def __init__(self, rc=0, out=b"", err=b""):
        self._out, self._err, self.returncode = out, err, rc

    async def communicate(self, stdin=None):
        self.last_stdin = stdin
        return (self._out, self._err)

    def kill(self):
        pass


def _patch_exec(proc):
    async def fake_exec(*args, **kwargs):
        proc.args = args
        proc.kwargs = kwargs
        return proc
    return patch.object(mysql_exec.asyncio, "create_subprocess_exec", side_effect=fake_exec)


# ── 이름 정규화 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("myapp", "proj_myapp"),
        ("todo-api", "proj_todo_api"),
        ("Hello-World", "proj_hello_world"),
        ("9lives", "proj_p9lives"),
    ],
)
def test_db_ident_sanitizes(name, expected):
    assert mysql_exec._db_ident(name) == expected


# ── provision ────────────────────────────────────────────────


async def test_provision_runs_expected_sql():
    proc = _FakeProc()
    with _patch_exec(proc):
        creds = await mysql_exec.provision(
            "myapp", root_password="rootpw", host="192.168.0.100"
        )

    assert creds.database == "proj_myapp"
    assert creds.user == "proj_myapp"
    assert creds.password  # 자동 생성
    assert creds.host == "192.168.0.100"

    sql = proc.last_stdin.decode()
    assert "CREATE DATABASE IF NOT EXISTS `proj_myapp`" in sql
    assert "CREATE USER IF NOT EXISTS 'proj_myapp'@'%'" in sql
    assert "GRANT ALL PRIVILEGES ON `proj_myapp`.*" in sql
    assert "FLUSH PRIVILEGES" in sql

    # mysql CLI 가 TCP 로 직접 접속
    args = proc.args
    assert args[0] == "mysql"
    assert "-h" in args
    assert "192.168.0.100" in args
    assert "-uroot" in args
    # root 비밀번호는 MYSQL_PWD env 로 전달
    env = proc.kwargs.get("env", {})
    assert env.get("MYSQL_PWD") == "rootpw"


async def test_provision_raises_when_mysql_fails():
    proc = _FakeProc(rc=1, err=b"Access denied")
    with _patch_exec(proc):
        with pytest.raises(MySQLError, match="Access denied"):
            await mysql_exec.provision("x", root_password="pw")


async def test_provision_raises_without_root_password():
    with pytest.raises(MySQLError, match="설정되지 않았"):
        await mysql_exec.provision("x", root_password="")


async def test_mysql_url_shape():
    proc = _FakeProc()
    with _patch_exec(proc):
        creds = await mysql_exec.provision("x", root_password="pw", host="h", port=3307)
    assert creds.mysql_url.startswith(f"mysql://{creds.user}:{creds.password}@h:3307/")


# ── drop ─────────────────────────────────────────────────────


async def test_drop_emits_drop_statements():
    proc = _FakeProc()
    with _patch_exec(proc):
        await mysql_exec.drop("myapp", root_password="pw")
    sql = proc.last_stdin.decode()
    assert "DROP DATABASE IF EXISTS `proj_myapp`" in sql
    assert "DROP USER IF EXISTS 'proj_myapp'@'%'" in sql


def test_is_enabled():
    assert mysql_exec.is_enabled("x")
    assert not mysql_exec.is_enabled("")
    assert not mysql_exec.is_enabled(None)
    assert not mysql_exec.is_enabled("   ")
