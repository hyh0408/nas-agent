"""SQLite 기반 프로젝트 레지스트리.

각 프로젝트는 Claude CLI 세션 UUID 와 1:1 로 대응되며, 이름으로 조회·이어서
작업할 수 있다. 실제 소스 파일들은 PROJECTS_DIR/<name>/ 아래에 위치한다.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")


class ProjectError(Exception):
    pass


@dataclass
class Project:
    name: str
    description: str
    session_id: str
    created_at: str
    updated_at: str
    repo_url: Optional[str] = None
    db_name: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None


@dataclass
class TaskRecord:
    id: int
    project_name: str
    task: str
    result: str
    deployed: bool
    created_at: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        raise ProjectError(
            f"프로젝트 이름은 소문자로 시작하고 2~31자의 소문자/숫자/-/_ 만 허용됩니다: {name!r}"
        )


class ProjectRegistry:
    """동기 sqlite3 를 asyncio.to_thread 로 감싼 간단한 레지스트리."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_schema()

    # ── 스키마 ────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    name         TEXT PRIMARY KEY,
                    description  TEXT NOT NULL,
                    session_id   TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name  TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
                    task          TEXT NOT NULL,
                    result        TEXT NOT NULL,
                    deployed      INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_name);
                """
            )
            # 마이그레이션: 새 컬럼들
            cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
            for col in ("repo_url", "db_name", "db_user", "db_password"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT")

    # ── 동기 구현 ─────────────────────────────────────────────

    def _create_sync(self, name: str, description: str) -> Project:
        validate_name(name)
        now = _utcnow()
        session_id = str(uuid.uuid4())
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO projects (name, description, session_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, description, session_id, now, now),
                )
            except sqlite3.IntegrityError as e:
                raise ProjectError(f"이미 존재하는 프로젝트: {name}") from e
        return Project(name, description, session_id, now, now)

    _SELECT = (
        "SELECT name, description, session_id, created_at, updated_at, "
        "repo_url, db_name, db_user, db_password "
        "FROM projects"
    )

    def _get_sync(self, name: str) -> Optional[Project]:
        with self._connect() as conn:
            row = conn.execute(f"{self._SELECT} WHERE name = ?", (name,)).fetchone()
        if not row:
            return None
        return Project(**dict(row))

    def _list_sync(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute(f"{self._SELECT} ORDER BY updated_at DESC").fetchall()
        return [Project(**dict(r)) for r in rows]

    def _touch_sync(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE projects SET updated_at = ? WHERE name = ?", (_utcnow(), name))

    def _record_task_sync(
        self, name: str, task: str, result: str, deployed: bool
    ) -> TaskRecord:
        now = _utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (project_name, task, result, deployed, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, task, result, 1 if deployed else 0, now),
            )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE name = ?", (now, name)
            )
            task_id = cur.lastrowid
        return TaskRecord(task_id, name, task, result, deployed, now)

    def _history_sync(self, name: str, limit: int = 10) -> list[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, project_name, task, result, deployed, created_at "
                "FROM tasks WHERE project_name = ? "
                "ORDER BY id DESC LIMIT ?",
                (name, limit),
            ).fetchall()
        return [
            TaskRecord(
                id=r["id"],
                project_name=r["project_name"],
                task=r["task"],
                result=r["result"],
                deployed=bool(r["deployed"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def _delete_sync(self, name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        return cur.rowcount > 0

    def _set_repo_url_sync(self, name: str, repo_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET repo_url = ?, updated_at = ? WHERE name = ?",
                (repo_url, _utcnow(), name),
            )

    def _set_db_info_sync(
        self, name: str, db_name: str, db_user: str, db_password: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET db_name = ?, db_user = ?, db_password = ?, "
                "updated_at = ? WHERE name = ?",
                (db_name, db_user, db_password, _utcnow(), name),
            )

    # ── async 래퍼 ────────────────────────────────────────────

    async def create(self, name: str, description: str) -> Project:
        return await asyncio.to_thread(self._create_sync, name, description)

    async def get(self, name: str) -> Optional[Project]:
        return await asyncio.to_thread(self._get_sync, name)

    async def list(self) -> list[Project]:
        return await asyncio.to_thread(self._list_sync)

    async def touch(self, name: str) -> None:
        await asyncio.to_thread(self._touch_sync, name)

    async def record_task(
        self, name: str, task: str, result: str, deployed: bool
    ) -> TaskRecord:
        return await asyncio.to_thread(self._record_task_sync, name, task, result, deployed)

    async def history(self, name: str, limit: int = 10) -> list[TaskRecord]:
        return await asyncio.to_thread(self._history_sync, name, limit)

    async def delete(self, name: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, name)

    async def set_repo_url(self, name: str, repo_url: str) -> None:
        await asyncio.to_thread(self._set_repo_url_sync, name, repo_url)

    async def set_db_info(
        self, name: str, db_name: str, db_user: str, db_password: str
    ) -> None:
        await asyncio.to_thread(
            self._set_db_info_sync, name, db_name, db_user, db_password
        )
