"""Claude Code CLI 실행기 - 복잡한 작업을 CLI에 위임"""

import asyncio
import os
from bot.config import Config


async def run_claude(prompt: str, work_dir: str | None = None) -> str:
    """Claude Code CLI를 실행하고 결과를 반환한다.

    구독에 포함된 CLI를 사용하므로 API 토큰을 소비하지 않는다.
    """
    cwd = work_dir or Config.PROJECTS_DIR

    # 작업 디렉터리가 없으면 생성
    os.makedirs(cwd, exist_ok=True)

    # ANTHROPIC_API_KEY 가 있으면 CLI 가 구독 세션 대신 API 로 결제되므로 제거
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    proc = await asyncio.create_subprocess_exec(
        Config.CLAUDE_CLI_PATH,
        "-p", prompt,
        "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=600,  # 코드 생성은 최대 10분
        )
    except asyncio.TimeoutError:
        proc.kill()
        return "Claude Code 실행 시간 초과 (10분). 작업이 너무 복잡할 수 있습니다."

    output = stdout.decode().strip()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        return f"Claude Code 오류:\n{err}\n\n출력:\n{output}"

    # 출력이 너무 길면 텔레그램 메시지 제한(4096자)에 맞게 자르기
    if len(output) > 3500:
        output = output[:1500] + "\n\n... (중략) ...\n\n" + output[-1500:]

    return output
