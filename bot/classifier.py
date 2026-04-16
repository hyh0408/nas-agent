"""룰 기반 명령 분류기.

텔레그램 메시지를 아래 카테고리로 분류한다.

    {"type": "simple",  "action": "status|system_status|logs|stop|restart|list_projects",
                         "target": "<container>"}
    {"type": "project", "mode": "new",      "name": "...", "description": "..."}
    {"type": "project", "mode": "continue", "name": "...", "task": "..."}
    {"type": "chat",    "message": "..."}
    {"type": "complex", "description": "..."}

자연어 연속 작업 감지를 위해 호출 측이 `known_projects` 를 주입한다.
"""

from __future__ import annotations

import re
from typing import Iterable


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

# "로그" 는 "블로그/카탈로그" 합성어와, "로그인" 동음이의어로 오탐이 잦아서
# 앞에 한글이 붙지 않고 뒤는 공백/구두점/특정 조사만 허용한다.
_KO_PARTICLE = r"를을이가은는도의만에"
LOGS = re.compile(
    rf"(?<![가-힣])로그(?=[\s,.!?{_KO_PARTICLE}]|$)|\blogs?\b",
    re.IGNORECASE,
)

# 컨테이너 조작 (프로젝트 워크플로와 별개). 배포는 자동화되므로 별도 simple 액션에서 제외.
STOP = re.compile(r"(중지|정지|멈춰|종료)|\bstop\b", re.IGNORECASE)
RESTART = re.compile(r"(재시작|리스타트)|\brestart\b", re.IGNORECASE)

# 새 프로젝트 생성 패턴
NEW_PROJECT = re.compile(
    r"^(?:프로젝트\s+)?([a-z][a-z0-9_-]{1,30})\s*"
    r"(?:를|을)?\s*(?:새로\s*)?만들어(?:줘)?\s*[:：]?\s*(.*)$",
    re.IGNORECASE,
)

TARGET_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]+")


def _extract_target(text: str, keyword_pattern: re.Pattern) -> str:
    cleaned = keyword_pattern.sub(" ", text)
    tokens = TARGET_TOKEN.findall(cleaned)
    return tokens[0] if tokens else ""


def classify(message: str, known_projects: Iterable[str] = ()) -> dict:
    text = message.strip()
    known = {p.lower() for p in known_projects}

    if GREETING.search(text):
        return {
            "type": "chat",
            "message": "안녕하세요! NAS 관리 봇입니다. 무엇을 도와드릴까요?",
        }

    if SYSTEM_STATUS.search(text):
        return {"type": "simple", "action": "system_status"}

    if LIST_PROJECTS.search(text):
        return {"type": "simple", "action": "list_projects"}

    # 새 프로젝트 생성: "myapp 만들어줘 : FastAPI 할일 API"
    if m := NEW_PROJECT.match(text):
        name = m.group(1).lower()
        description = m.group(2).strip() or text
        return {
            "type": "project",
            "mode": "new",
            "name": name,
            "description": description,
        }

    # 기존 프로젝트 이어서 작업: 메시지 어딘가에 등록된 프로젝트 이름이 있고,
    # 단순 액션 키워드에 걸리지 않으면 워크플로로 라우팅
    simple_action = _detect_simple_action(text)
    if not simple_action:
        for tok in TARGET_TOKEN.findall(text):
            if tok.lower() in known:
                name = tok.lower()
                task = re.sub(re.escape(tok), "", text, count=1).strip(" :,.")
                if task:
                    return {
                        "type": "project",
                        "mode": "continue",
                        "name": name,
                        "task": task,
                    }

    if simple_action:
        return simple_action

    if CONTAINER_STATUS.search(text):
        return {"type": "simple", "action": "status"}

    return {"type": "complex", "description": text}


def _detect_simple_action(text: str) -> dict | None:
    for pattern, action in (
        (LOGS, "logs"),
        (STOP, "stop"),
        (RESTART, "restart"),
    ):
        if pattern.search(text):
            target = _extract_target(text, pattern)
            if target:
                return {"type": "simple", "action": action, "target": target}
    return None


# 기존 코드와의 호환: async API 보존
async def classify_async(message: str, known_projects: Iterable[str] = ()) -> dict:
    return classify(message, known_projects)
