"""Claude Code CLI 실행기.

LangGraph 노드가 호출하는 저수준 래퍼. 세션 ID 를 명시적으로 지정해서 새로
만들거나, 기존 세션을 resume 해 이전 대화 맥락을 이어간다. 결과는
`--output-format json` 으로 받아 구조화된 값으로 돌려준다.

서브프로세스 env 에서 ANTHROPIC_API_KEY 를 제거해서 MAX 구독 세션만 사용한다.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Optional

from bot.config import Config


@dataclass
class ClaudeResult:
    session_id: str
    text: str
    is_error: bool
    raw: dict


async def run_claude(
    prompt: str,
    *,
    cwd: str,
    session_id: Optional[str] = None,
    resume: bool = False,
    timeout: int = 900,
) -> ClaudeResult:
    """Claude CLI 를 한 번 실행한다.

    Args:
        prompt: 사용자 요청 + 규칙.
        cwd: 작업 디렉터리. 각 프로젝트의 루트.
        session_id: 새 세션을 만들 때는 미리 생성한 UUID 를 전달, resume 할 때는
            기존 세션 UUID 를 전달한다.
        resume: True 면 --resume, False 면 --session-id 로 신규 생성.
        timeout: 초. 기본 15분.
    """
    os.makedirs(cwd, exist_ok=True)

    # ANTHROPIC_API_KEY 가 있으면 CLI 가 구독 세션 대신 API 로 결제되므로 제거
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    args = [
        Config.CLAUDE_CLI_PATH,
        "-p", prompt,
        "--output-format", "json",
        # 자동화된 실행이므로 파일/명령 권한 프롬프트를 건너뛴다.
        "--permission-mode", "bypassPermissions",
    ]
    if session_id:
        args += (["--resume", session_id] if resume else ["--session-id", session_id])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ClaudeResult(
            session_id=session_id or "",
            text=f"Claude CLI 실행 시간 초과 ({timeout}s).",
            is_error=True,
            raw={},
        )

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()

    if proc.returncode != 0 and not stdout_text:
        return ClaudeResult(
            session_id=session_id or "",
            text=f"CLI exit {proc.returncode}: {stderr_text or '(stderr empty)'}",
            is_error=True,
            raw={},
        )

    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        # JSON 파싱 실패 시 원문을 반환해 디버깅에 활용
        return ClaudeResult(
            session_id=session_id or "",
            text=stdout_text or stderr_text,
            is_error=True,
            raw={"stdout": stdout_text, "stderr": stderr_text},
        )

    return ClaudeResult(
        session_id=payload.get("session_id", session_id or ""),
        text=payload.get("result", ""),
        is_error=bool(payload.get("is_error", False)) or proc.returncode != 0,
        raw=payload,
    )
