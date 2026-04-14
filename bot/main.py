"""NAS Agent Telegram Bot - 메인 진입점"""

import asyncio
import logging
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import Config
from bot.classifier import classify
from executor.docker_exec import (
    container_status,
    container_logs,
    container_stop,
    container_restart,
    deploy_project,
    list_projects,
    system_status,
)
from executor.claude_exec import run_claude

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("nas-agent")


# ── 보안: 허용된 사용자만 접근 ──────────────────────────────

def authorized(func):
    async def wrapper(update: Update, context):
        user_id = update.effective_user.id
        if Config.ALLOWED_USER_IDS and user_id not in Config.ALLOWED_USER_IDS:
            await update.message.reply_text("접근 권한이 없습니다.")
            logger.warning(f"Unauthorized access attempt: {user_id}")
            return
        return await func(update, context)
    return wrapper


# ── 명령 핸들러 ──────────────────────────────────────────────

@authorized
async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "NAS Agent Bot 입니다.\n\n"
        "사용 가능한 명령:\n"
        "/sys - NAS 리소스 상태 (CPU/메모리/디스크)\n"
        "/status - 컨테이너 상태\n"
        "/logs <이름> - 컨테이너 로그\n"
        "/deploy <프로젝트> - 프로젝트 배포\n"
        "/stop <이름> - 컨테이너 중지\n"
        "/restart <이름> - 컨테이너 재시작\n"
        "/projects - 프로젝트 목록\n\n"
        "또는 자연어로 말씀하세요!\n"
        "예: \"FastAPI로 할일 앱 만들어서 배포해줘\""
    )


@authorized
async def cmd_status(update: Update, context):
    await update.message.reply_text("컨테이너 상태 조회 중...")
    result = await container_status()
    await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")


@authorized
async def cmd_logs(update: Update, context):
    if not context.args:
        await update.message.reply_text("사용법: /logs <컨테이너이름>")
        return
    name = context.args[0]
    result = await container_logs(name)
    await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")


@authorized
async def cmd_deploy(update: Update, context):
    if not context.args:
        await update.message.reply_text("사용법: /deploy <프로젝트이름>")
        return
    project = context.args[0]
    await update.message.reply_text(f"'{project}' 배포 시작...")
    result = await deploy_project(project, Config.PROJECTS_DIR)
    await update.message.reply_text(f"배포 결과:\n```\n{result}\n```", parse_mode="Markdown")


@authorized
async def cmd_stop(update: Update, context):
    if not context.args:
        await update.message.reply_text("사용법: /stop <컨테이너이름>")
        return
    result = await container_stop(context.args[0])
    await update.message.reply_text(result)


@authorized
async def cmd_restart(update: Update, context):
    if not context.args:
        await update.message.reply_text("사용법: /restart <컨테이너이름>")
        return
    result = await container_restart(context.args[0])
    await update.message.reply_text(result)


@authorized
async def cmd_sys(update: Update, context):
    result = await system_status()
    await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")


@authorized
async def cmd_projects(update: Update, context):
    result = await list_projects(Config.PROJECTS_DIR)
    await update.message.reply_text(f"프로젝트 목록:\n```\n{result}\n```", parse_mode="Markdown")


# ── 자연어 메시지 핸들러 (AI 분류) ───────────────────────────

@authorized
async def handle_message(update: Update, context):
    """자연어 메시지를 Haiku로 분류하고 적절한 액션을 실행한다."""
    text = update.message.text
    logger.info(f"Message from {update.effective_user.id}: {text}")

    try:
        classified = await classify(text)
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        await update.message.reply_text(f"명령 분석 실패: {e}")
        return

    msg_type = classified.get("type")
    logger.info(f"Classified as: {classified}")

    if msg_type == "chat":
        await update.message.reply_text(classified.get("message", "네, 말씀하세요!"))

    elif msg_type == "simple":
        await _handle_simple(update, classified)

    elif msg_type == "complex":
        await _handle_complex(update, classified)

    else:
        await update.message.reply_text("이해하지 못했습니다. 다시 말씀해 주세요.")


async def _handle_simple(update: Update, cmd: dict):
    action = cmd.get("action")
    target = cmd.get("target", "")

    if action == "status":
        result = await container_status()
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")

    elif action == "system_status":
        result = await system_status()
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")

    elif action == "logs":
        result = await container_logs(target)
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")

    elif action == "deploy":
        await update.message.reply_text(f"'{target}' 배포 시작...")
        result = await deploy_project(target, Config.PROJECTS_DIR)
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")

    elif action == "stop":
        result = await container_stop(target)
        await update.message.reply_text(f"'{target}' 중지: {result}")

    elif action == "restart":
        result = await container_restart(target)
        await update.message.reply_text(f"'{target}' 재시작: {result}")

    elif action == "list_projects":
        result = await list_projects(Config.PROJECTS_DIR)
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")

    else:
        await update.message.reply_text(f"알 수 없는 액션: {action}")


async def _handle_complex(update: Update, cmd: dict):
    description = cmd.get("description", "")
    await update.message.reply_text(
        f"Claude Code에 작업을 요청합니다...\n"
        f"내용: {description}\n\n"
        f"시간이 좀 걸릴 수 있습니다."
    )

    # Claude Code CLI 실행 - 구독에 포함, 추가 API 비용 없음
    prompt = (
        f"{description}\n\n"
        f"규칙:\n"
        f"- 프로젝트 디렉터리를 만들고 그 안에 모든 파일 생성\n"
        f"- Dockerfile과 docker-compose.yml 반드시 포함\n"
        f"- 완료 후 docker compose up -d --build 로 배포\n"
        f"- 한국어로 결과 요약"
    )

    result = await run_claude(prompt)
    await update.message.reply_text(f"작업 완료:\n\n{result}")


# ── Health check 서버 ────────────────────────────────────────

async def health_handler(request):
    return web.Response(text="ok")


async def run_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", Config.HEALTH_PORT)
    await site.start()
    logger.info(f"Health server on :{Config.HEALTH_PORT}")


# ── 메인 ─────────────────────────────────────────────────────

def main():
    app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    # 명령 핸들러
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("deploy", cmd_deploy))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("sys", cmd_sys))

    # 자연어 메시지 핸들러
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Health check 서버 시작
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_health_server())

    logger.info("NAS Agent Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
