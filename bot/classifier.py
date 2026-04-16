"""룰 기반 명령 분류기 - API 호출 없이 정규식으로 분류"""

import re


GREETING = re.compile(r"^\s*(안녕|하이|헬로|hi|hello|ㅎㅇ)\b", re.IGNORECASE)

SYSTEM_STATUS = re.compile(
    r"(cpu|메모리|memory|디스크|disk|리소스|resource|시스템\s*상태|nas\s*상태|\bsys\b)",
    re.IGNORECASE,
)

LIST_PROJECTS = re.compile(
    r"(프로젝트\s*목록|프로젝트들|project\s*list|list\s*project)",
    re.IGNORECASE,
)

CONTAINER_STATUS = re.compile(
    r"(컨테이너\s*상태|container\s*status|^\s*상태)",
    re.IGNORECASE,
)

# "로그" 는 블로그/카탈로그/다이얼로그 등 합성어에 자주 포함되므로 한글 접두사가
# 앞에 붙지 않은 경우에만 인정한다. 다른 키워드들은 접두사 충돌 사례가 드물다.
LOGS = re.compile(r"(?<![가-힣])로그|\blogs?\b", re.IGNORECASE)
DEPLOY = re.compile(r"배포|\bdeploy\b", re.IGNORECASE)
STOP = re.compile(r"(중지|정지|멈춰|종료)|\bstop\b", re.IGNORECASE)
RESTART = re.compile(r"(재시작|리스타트)|\brestart\b", re.IGNORECASE)

TARGET_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]+")


def _extract_target(text: str, keyword_pattern: re.Pattern) -> str:
    cleaned = keyword_pattern.sub(" ", text)
    tokens = TARGET_TOKEN.findall(cleaned)
    return tokens[0] if tokens else ""


async def classify(message: str) -> dict:
    text = message.strip()

    if GREETING.search(text):
        return {
            "type": "chat",
            "message": "안녕하세요! NAS 관리 봇입니다. 무엇을 도와드릴까요?",
        }

    if SYSTEM_STATUS.search(text):
        return {"type": "simple", "action": "system_status"}

    if LIST_PROJECTS.search(text):
        return {"type": "simple", "action": "list_projects"}

    for pattern, action in (
        (LOGS, "logs"),
        (DEPLOY, "deploy"),
        (STOP, "stop"),
        (RESTART, "restart"),
    ):
        if pattern.search(text):
            target = _extract_target(text, pattern)
            if target:
                return {"type": "simple", "action": action, "target": target}

    if CONTAINER_STATUS.search(text):
        return {"type": "simple", "action": "status"}

    return {"type": "complex", "description": text}
