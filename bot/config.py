import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    ALLOWED_USER_IDS: list[int] = [
        int(uid.strip())
        for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    ]
    CLAUDE_CLI_PATH: str = os.environ.get("CLAUDE_CLI_PATH", "/root/.claude/local/claude")
    PROJECTS_DIR: str = os.environ.get("PROJECTS_DIR", "/app/projects")
    NAS_HOST: str = os.environ.get("NAS_HOST", "nas.local")
    HEALTH_PORT: int = int(os.environ.get("HEALTH_PORT", "9100"))
