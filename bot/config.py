import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
    ALLOWED_USER_IDS: list[int] = [
        int(uid.strip())
        for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    ]
    CLAUDE_CLI_PATH: str = os.environ.get("CLAUDE_CLI_PATH", "/usr/bin/claude")
    PROJECTS_DIR: str = os.environ.get("PROJECTS_DIR", "/app/projects")
    DATA_DIR: str = os.environ.get("DATA_DIR", "/app/data")
    NAS_HOST: str = os.environ.get("NAS_HOST", "nas.local")
    HEALTH_PORT: int = int(os.environ.get("HEALTH_PORT", "9100"))

    # GitHub 연동 (선택). GITHUB_TOKEN 이 비어 있으면 전 기능 비활성.
    GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
    GITHUB_OWNER: str = os.environ.get("GITHUB_OWNER", "")  # 비면 토큰 소유자의 user repo
    GITHUB_PRIVATE: bool = os.environ.get("GITHUB_PRIVATE", "true").lower() != "false"
    GIT_USER_NAME: str = os.environ.get("GIT_USER_NAME", "NAS Agent")
    GIT_USER_EMAIL: str = os.environ.get("GIT_USER_EMAIL", "nas-agent@local")

    @classmethod
    def registry_db_path(cls) -> str:
        return os.path.join(cls.DATA_DIR, "registry.db")
