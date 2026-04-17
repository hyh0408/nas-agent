"""Synology MariaDB 에 프로젝트 전용 database + user 를 프로비저닝한다.

- NAS 호스트의 MariaDB 에 TCP 로 직접 접속 (mysql CLI).
- root 암호는 MYSQL_PWD 환경변수로 전달 (ps 노출 방지).
- 프로젝트 컨테이너는 NAS 호스트 IP(MYSQL_HOST) 로 DB 에 접근.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional


@dataclass
class DBCredentials:
    host: str
    port: int
    database: str
    user: str
    password: str

    @property
    def mysql_url(self) -> str:
        """SQLAlchemy 스타일 URL (드라이버는 앱이 결정)."""
        return (
            f"mysql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"
        )


class MySQLError(Exception):
    pass


# ── 이름 정규화 ───────────────────────────────────────────────


_IDENT_SAFE = re.compile(r"[^a-zA-Z0-9_]")


def _db_ident(project_name: str) -> str:
    """MySQL 식별자로 쓸 수 있게 정규화. proj_<sanitized>."""
    sanitized = _IDENT_SAFE.sub("_", project_name).lower()
    if sanitized and sanitized[0].isdigit():
        sanitized = f"p{sanitized}"
    return f"proj_{sanitized}"[:63]


def _gen_password() -> str:
    return secrets.token_urlsafe(24)


# ── 코어 동작 ────────────────────────────────────────────────


async def _exec_sql(
    sql: str,
    *,
    host: str,
    port: int,
    root_password: str,
    timeout: int = 30,
) -> None:
    """mysql CLI 로 TCP 접속해 SQL 실행. 실패하면 MySQLError."""
    env = {**os.environ, "MYSQL_PWD": root_password}

    proc = await asyncio.create_subprocess_exec(
        "mysql",
        "-h", host,
        "-P", str(port),
        "-uroot",
        "--batch",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(sql.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise MySQLError(f"MySQL 명령 시간 초과: {sql[:80]!r}")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        raise MySQLError(f"MySQL 실행 실패 (exit {proc.returncode}): {err}")


async def provision(
    project_name: str,
    *,
    root_password: str,
    host: str = "192.168.0.100",
    port: int = 3306,
) -> DBCredentials:
    """프로젝트 전용 database + user 를 생성하고 권한을 부여한다."""
    if not root_password:
        raise MySQLError("MYSQL_ROOT_PASSWORD 가 설정되지 않았습니다.")

    ident = _db_ident(project_name)
    password = _gen_password()

    pw_sql = password.replace("'", "''").replace("\\", "\\\\")

    sql = (
        f"CREATE DATABASE IF NOT EXISTS `{ident}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
        f"CREATE USER IF NOT EXISTS '{ident}'@'%' IDENTIFIED BY '{pw_sql}';\n"
        f"ALTER USER '{ident}'@'%' IDENTIFIED BY '{pw_sql}';\n"
        f"GRANT ALL PRIVILEGES ON `{ident}`.* TO '{ident}'@'%';\n"
        "FLUSH PRIVILEGES;\n"
    )

    await _exec_sql(sql, host=host, port=port, root_password=root_password)
    return DBCredentials(
        host=host, port=port, database=ident, user=ident, password=password
    )


async def drop(
    project_name: str,
    *,
    root_password: str,
    host: str = "192.168.0.100",
    port: int = 3306,
) -> None:
    """프로젝트 database + user 제거."""
    ident = _db_ident(project_name)
    sql = (
        f"DROP DATABASE IF EXISTS `{ident}`;\n"
        f"DROP USER IF EXISTS '{ident}'@'%';\n"
        "FLUSH PRIVILEGES;\n"
    )
    await _exec_sql(sql, host=host, port=port, root_password=root_password)


def is_enabled(root_password: Optional[str]) -> bool:
    return bool(root_password and root_password.strip())
