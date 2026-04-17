"""NAS Agent Telegram Bot - 메인 진입점"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

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
    list_projects as list_project_dirs,
    system_status,
)
from executor import mysql_exec
from executor.mysql_exec import MySQLError
from executor.projects import ProjectRegistry, ProjectError, validate_name
from executor.workflow import (
    build_workflow,
    format_workflow_result,
    GitHubConfig,
    MySQLConfig,
    SubAgentConfig,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("nas-agent")


# ── 전역 상태 ────────────────────────────────────────────────

# 모듈 import 시점에 디렉터리를 만들지 않도록 main() 에서 지연 초기화.
registry: ProjectRegistry | None = None
workflow = None
# 같은 프로젝트에 동시 작업 요청이 오면 CLI 세션이 충돌할 수 있으니 직렬화.
_project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _init_state() -> None:
    global registry, workflow
    registry = ProjectRegistry(Config.registry_db_path())
    github_cfg = GitHubConfig(
        token=Config.GITHUB_TOKEN,
        owner=Config.GITHUB_OWNER,
        private=Config.GITHUB_PRIVATE,
        user_name=Config.GIT_USER_NAME,
        user_email=Config.GIT_USER_EMAIL,
    )
    mysql_cfg = MySQLConfig(
        root_password=Config.MYSQL_ROOT_PASSWORD,
        container=Config.MYSQL_CONTAINER,
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
        shared_network=Config.SHARED_NETWORK,
    )
    sa_cfg = SubAgentConfig()
    workflow = build_workflow(registry, github_cfg, mysql_cfg, sa_cfg)
    logger.info(
        f"GitHub: {'ON' if github_cfg.enabled else 'OFF'} | "
        f"MySQL: {'ON' if mysql_cfg.enabled else 'OFF'} | "
        f"SubAgents: per-project (--agents)"
    )


# ── 보안 ────────────────────────────────────────────────────

def authorized(func):
    async def wrapper(update: Update, context):
        user_id = update.effective_user.id
        if Config.ALLOWED_USER_IDS and user_id not in Config.ALLOWED_USER_IDS:
            await update.message.reply_text("접근 권한이 없습니다.")
            logger.warning(f"Unauthorized access attempt: {user_id}")
            return
        return await func(update, context)
    return wrapper


# ── 공통 유틸 ────────────────────────────────────────────────

async def _run_workflow_and_reply(
    update: Update,
    *,
    project_name: str,
    is_new: bool,
    task: str = "",
    description: str = "",
    db_required: bool = False,
    sub_agents: bool = False,
):
    lock = _project_locks[project_name]
    if lock.locked():
        await update.message.reply_text(
            f"'{project_name}' 는 이미 작업 중입니다. 완료 후 다시 시도해 주세요."
        )
        return

    async with lock:
        action_label = "생성" if is_new else "작업"
        parts = []
        if is_new and db_required:
            parts.append("MySQL DB")
        if sub_agents:
            parts.append("sub-agents")
        extras = f" (+{', '.join(parts)})" if parts else ""
        await update.message.reply_text(
            f"🔨 '{project_name}' {action_label}{extras} 시작. 완료까지 몇 분 걸릴 수 있습니다."
        )
        try:
            state = await workflow.ainvoke({
                "project_name": project_name,
                "task": task,
                "is_new": is_new,
                "description": description,
                "db_required": db_required,
                "sub_agents": sub_agents,
                "projects_dir": Config.PROJECTS_DIR,
            })
        except Exception as e:
            logger.exception("workflow crashed")
            await update.message.reply_text(f"워크플로 오류: {e}")
            return

    await update.message.reply_text(format_workflow_result(state))


# ── 명령 핸들러 ──────────────────────────────────────────────

@authorized
async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "NAS Agent Bot\n\n"
        "── 프로젝트 ─────────────\n"
        "/new <이름> [--db] [--agents] <설명>  - 새 프로젝트 (--db: MySQL, --agents: 리뷰)\n"
        "/work <이름> <작업>       - 기존 프로젝트 이어서 개발 + 재배포\n"
        "/info <이름>              - 프로젝트 상태·히스토리\n"
        "/projects                 - 프로젝트 목록\n"
        "/rm <이름> [--drop-db]    - 레지스트리 제거 (--drop-db: DB 도 삭제)\n\n"
        "── 컨테이너 ─────────────\n"
        "/sys                    - NAS 리소스 상태\n"
        "/status                 - 컨테이너 상태\n"
        "/logs <컨테이너>         - 로그\n"
        "/stop <컨테이너>         - 중지\n"
        "/restart <컨테이너>      - 재시작\n\n"
        "자연어도 가능. 예: \"myapp 에 로그인 기능 추가해줘\""
    )


@authorized
async def cmd_sys(update: Update, context):
    result = await system_status()
    await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")


@authorized
async def cmd_status(update: Update, context):
    result = await container_status()
    await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")


@authorized
async def cmd_logs(update: Update, context):
    if not context.args:
        await update.message.reply_text("사용법: /logs <컨테이너이름>")
        return
    result = await container_logs(context.args[0])
    await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")


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
async def cmd_new(update: Update, context):
    if len(context.args) < 2:
        await update.message.reply_text(
            "사용법: /new <이름> [--db] [--agents] <설명>\n"
            "  --db      MySQL database 자동 생성\n"
            "  --agents  plan→code→review→fix sub-agent 활성"
        )
        return
    name = context.args[0].lower()
    rest = list(context.args[1:])
    db_required = False
    use_agents = False
    # 플래그 파싱 (순서 무관)
    flags = {"--db", "--agents"}
    while rest and rest[0] in flags:
        flag = rest.pop(0)
        if flag == "--db":
            db_required = True
        elif flag == "--agents":
            use_agents = True
    if not rest:
        await update.message.reply_text("설명이 비어 있습니다.")
        return
    description = " ".join(rest)
    try:
        validate_name(name)
    except ProjectError as e:
        await update.message.reply_text(str(e))
        return
    await _run_workflow_and_reply(
        update,
        project_name=name,
        is_new=True,
        description=description,
        db_required=db_required,
        sub_agents=use_agents,
    )


@authorized
async def cmd_work(update: Update, context):
    if len(context.args) < 2:
        await update.message.reply_text("사용법: /work <이름> <작업 내용>")
        return
    name = context.args[0].lower()
    task = " ".join(context.args[1:])
    # 프로젝트에 저장된 sub_agents 설정 미리 로드 (workflow load 에서도 하지만
    # _run_workflow_and_reply 메시지에 표시하기 위해)
    project = await registry.get(name)
    use_agents = project.sub_agents if project else False
    await _run_workflow_and_reply(
        update, project_name=name, is_new=False, task=task, sub_agents=use_agents
    )


@authorized
async def cmd_info(update: Update, context):
    if not context.args:
        await update.message.reply_text("사용법: /info <프로젝트이름>")
        return
    name = context.args[0].lower()
    project = await registry.get(name)
    if not project:
        await update.message.reply_text(f"프로젝트를 찾을 수 없음: {name}")
        return
    history = await registry.history(name, limit=5)
    lines = [
        f"📦 {project.name}",
        f"설명: {project.description}",
        f"생성: {project.created_at}",
        f"최근: {project.updated_at}",
        f"세션: {project.session_id[:8]}…",
    ]
    if project.repo_url:
        lines.append(f"repo: {project.repo_url}")
    if project.db_name:
        lines.append(f"DB: {project.db_name} (user {project.db_user})")
    if project.sub_agents:
        lines.append("agents: plan→code→review→fix")
    lines.extend(["", "최근 작업:"])
    if not history:
        lines.append("(아직 없음)")
    else:
        for t in history:
            tag = "✅" if t.deployed else "•"
            lines.append(f"{tag} [{t.created_at[:16]}] {t.task[:60]}")
    await update.message.reply_text("\n".join(lines))


@authorized
async def cmd_projects(update: Update, context):
    projects = await registry.list()
    if not projects:
        await update.message.reply_text("등록된 프로젝트가 없습니다. /new 로 시작하세요.")
        return
    lines = ["📚 프로젝트 목록"]
    for p in projects:
        lines.append(f"• {p.name} — {p.description[:60]}")
    await update.message.reply_text("\n".join(lines))


@authorized
async def cmd_rm(update: Update, context):
    if not context.args:
        await update.message.reply_text("사용법: /rm <프로젝트이름> [--drop-db]")
        return
    name = context.args[0].lower()
    drop_db = "--drop-db" in context.args[1:]

    project = await registry.get(name)
    if not project:
        await update.message.reply_text(f"없는 프로젝트: {name}")
        return

    db_msg = ""
    if drop_db and project.db_name and Config.MYSQL_ROOT_PASSWORD:
        try:
            await mysql_exec.drop(
                name,
                root_password=Config.MYSQL_ROOT_PASSWORD,
                container=Config.MYSQL_CONTAINER,
            )
            db_msg = f"\nMySQL DB '{project.db_name}' 삭제됨"
        except MySQLError as e:
            db_msg = f"\nMySQL 삭제 실패: {e}"

    await registry.delete(name)
    await update.message.reply_text(
        f"'{name}' 제거됨\n(프로젝트 파일은 그대로 남아 있습니다)" + db_msg
    )


# ── 자연어 메시지 핸들러 ─────────────────────────────────────

@authorized
async def handle_message(update: Update, context):
    text = update.message.text
    logger.info(f"Message from {update.effective_user.id}: {text}")

    projects = await registry.list()
    known = {p.name for p in projects}
    classified = classify(text, known_projects=known)
    logger.info(f"Classified: {classified}")

    msg_type = classified["type"]

    if msg_type == "chat":
        await update.message.reply_text(classified.get("message", "네, 말씀하세요!"))
        return

    if msg_type == "project":
        if classified["mode"] == "new":
            await _run_workflow_and_reply(
                update,
                project_name=classified["name"],
                is_new=True,
                description=classified["description"],
                db_required=bool(classified.get("db_required")),
            )
        else:
            proj = await registry.get(classified["name"])
            await _run_workflow_and_reply(
                update,
                project_name=classified["name"],
                is_new=False,
                task=classified["task"],
                sub_agents=proj.sub_agents if proj else False,
            )
        return

    if msg_type == "simple":
        await _handle_simple(update, classified)
        return

    if msg_type == "complex":
        # 프로젝트 컨텍스트 없는 단발성 요청은 안내만 하고 CLI 를 돌리지 않는다.
        # (예전에는 /app/projects 루트에서 돌렸지만 세션 관리가 안 됨)
        await update.message.reply_text(
            "프로젝트 단위로 작업하려면 /new 또는 /work 를 사용해 주세요.\n"
            "예: `/new myapi FastAPI 로 할일 API 만들어줘`"
        )
        return

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
    elif action == "stop":
        result = await container_stop(target)
        await update.message.reply_text(f"'{target}' 중지: {result}")
    elif action == "restart":
        result = await container_restart(target)
        await update.message.reply_text(f"'{target}' 재시작: {result}")
    elif action == "list_projects":
        await cmd_projects(update, None)
    else:
        await update.message.reply_text(f"알 수 없는 액션: {action}")


# ── Health check ─────────────────────────────────────────────

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
    _init_state()
    app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))

    # project
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("rm", cmd_rm))

    # container
    app.add_handler(CommandHandler("sys", cmd_sys))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("restart", cmd_restart))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_health_server())

    logger.info("NAS Agent Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
