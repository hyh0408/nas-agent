"""NAS Agent Telegram Bot - 메인 진입점"""

from __future__ import annotations

import asyncio
import logging
import os
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
# 유저별 현재 작업 중인 프로젝트 (in-memory, 재시작 시 초기화)
_current_project: dict[int, str] = {}


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
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
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
        if sub_agents:
            parts.append("sub-agents")
        extras = f" (+{', '.join(parts)})" if parts else ""
        msg = f"🔨 '{project_name}' {action_label}{extras} 시작. 완료까지 몇 분 걸릴 수 있습니다."
        logger.info(f"[workflow] {msg}")
        await update.message.reply_text(msg)
        try:
            state = await workflow.ainvoke({
                "project_name": project_name,
                "task": task,
                "is_new": is_new,
                "description": description,
                "sub_agents": sub_agents,
                "projects_dir": Config.PROJECTS_DIR,
            })
        except Exception as e:
            logger.exception(f"[workflow] '{project_name}' 크래시")
            await update.message.reply_text(f"워크플로 오류: {e}")
            return

    # 성공 시 현재 프로젝트로 설정
    user_id = update.effective_user.id
    if not state.get("error"):
        _current_project[user_id] = project_name
        logger.info(f"[context] user={user_id} 현재 프로젝트 → '{project_name}'")

    result_text = format_workflow_result(state)
    logger.info(f"[workflow] '{project_name}' 완료 — status={state.get('status')} deployed={state.get('deployed')}")
    if state.get("error"):
        logger.error(f"[workflow] '{project_name}' 에러: {state['error']}")
    await update.message.reply_text(result_text)


# ── 명령 핸들러 ──────────────────────────────────────────────

@authorized
async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "NAS Agent Bot\n\n"
        "── 프로젝트 ─────────────\n"
        "/new <이름> [--agents] <설명>  - 새 프로젝트 (DB 자동)\n"
        "/work <이름> <작업>       - 이어서 개발\n"
        "/use <이름>               - 현재 프로젝트 전환\n"
        "/current                  - 현재 프로젝트 확인\n"
        "/info <이름>              - 프로젝트 상태\n"
        "/projects                 - 목록\n"
        "/rm <이름>                - 제거\n\n"
        "💡 /new 또는 /use 후에는 프로젝트 이름 없이 메시지만 보내도 됩니다.\n\n"
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
    logger.info(f"[cmd] /new args={context.args} from={update.effective_user.id}")
    if len(context.args) < 2:
        await update.message.reply_text(
            "사용법: /new <이름> [--agents] <설명>\n"
            "  --agents  plan→code→review→fix sub-agent 활성\n"
            "  DB 는 MariaDB 설정 시 자동 프로비저닝됩니다."
        )
        return
    name = context.args[0].lower()
    rest = list(context.args[1:])
    use_agents = False
    # 플래그 파싱
    while rest and rest[0] in {"--agents", "--db"}:
        flag = rest.pop(0)
        if flag == "--agents":
            use_agents = True
        # --db 는 하위 호환으로 받되 무시 (자동 프로비저닝)
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
        sub_agents=use_agents,
    )


@authorized
async def cmd_work(update: Update, context):
    logger.info(f"[cmd] /work args={context.args} from={update.effective_user.id}")
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
    logger.info(f"[cmd] /rm args={context.args} from={update.effective_user.id}")
    if not context.args:
        await update.message.reply_text("사용법: /rm <프로젝트이름>")
        return
    name = context.args[0].lower()

    project = await registry.get(name)
    if not project:
        await update.message.reply_text(f"없는 프로젝트: {name}")
        return

    results: list[str] = []

    # 1. MySQL DB 삭제
    if project.db_name and Config.MYSQL_ROOT_PASSWORD:
        try:
            await mysql_exec.drop(
                name,
                root_password=Config.MYSQL_ROOT_PASSWORD,
                host=Config.MYSQL_HOST,
                port=Config.MYSQL_PORT,
            )
            results.append(f"MySQL DB '{project.db_name}' 삭제됨")
            logger.info(f"[rm] '{name}' MySQL DB 삭제 완료")
        except MySQLError as e:
            results.append(f"MySQL 삭제 실패: {e}")
            logger.error(f"[rm] '{name}' MySQL 삭제 실패: {e}")

    # 2. GitHub repo 는 보존 (다음 /new 에서 재사용)
    if project.repo_url:
        results.append(f"GitHub repo 보존됨: {project.repo_url}")

    # 3. 로컬 프로젝트 파일 삭제
    import shutil
    project_dir = os.path.join(Config.PROJECTS_DIR, name)
    if os.path.isdir(project_dir):
        try:
            shutil.rmtree(project_dir)
            results.append("로컬 파일 삭제됨")
            logger.info(f"[rm] '{name}' 로컬 파일 삭제 완료: {project_dir}")
        except OSError as e:
            results.append(f"로컬 파일 삭제 실패: {e}")
            logger.error(f"[rm] '{name}' 로컬 파일 삭제 실패: {e}")

    # 4. 레지스트리 삭제
    await registry.delete(name)
    results.append("레지스트리 제거됨")

    result_msg = f"'{name}' 삭제 완료:\n" + "\n".join(f"  • {r}" for r in results)
    logger.info(f"[cmd] /rm '{name}' 완료")
    await update.message.reply_text(result_msg)


@authorized
async def cmd_use(update: Update, context):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("사용법: /use <프로젝트이름>")
        return
    name = context.args[0].lower()
    project = await registry.get(name)
    if not project:
        await update.message.reply_text(f"프로젝트를 찾을 수 없음: {name}")
        return
    _current_project[user_id] = name
    logger.info(f"[context] user={user_id} 현재 프로젝트 → '{name}'")
    agents = " (agents)" if project.sub_agents else ""
    await update.message.reply_text(
        f"🔀 현재 프로젝트: {name}{agents}\n"
        f"이제 메시지를 보내면 '{name}' 에 대한 작업으로 처리됩니다."
    )


@authorized
async def cmd_current(update: Update, context):
    user_id = update.effective_user.id
    name = _current_project.get(user_id)
    if not name:
        await update.message.reply_text("현재 선택된 프로젝트가 없습니다. /use <이름> 으로 지정하세요.")
        return
    project = await registry.get(name)
    if not project:
        del _current_project[user_id]
        await update.message.reply_text("이전에 선택한 프로젝트가 삭제되었습니다. /use <이름> 으로 다시 지정하세요.")
        return
    agents = " (agents)" if project.sub_agents else ""
    await update.message.reply_text(f"📌 현재 프로젝트: {name}{agents}")


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
        # 현재 프로젝트가 있으면 해당 프로젝트에 대한 작업으로 처리
        user_id = update.effective_user.id
        current = _current_project.get(user_id)
        if current:
            proj = await registry.get(current)
            if proj:
                logger.info(f"[context] 현재 프로젝트 '{current}' 로 라우팅: {text[:50]}")
                await _run_workflow_and_reply(
                    update,
                    project_name=current,
                    is_new=False,
                    task=classified["description"],
                    sub_agents=proj.sub_agents,
                )
                return
        await update.message.reply_text(
            "현재 선택된 프로젝트가 없습니다.\n"
            "• /new <이름> <설명> — 새 프로젝트\n"
            "• /use <이름> — 기존 프로젝트 선택\n"
            "• /projects — 목록 보기"
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
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("current", cmd_current))
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
