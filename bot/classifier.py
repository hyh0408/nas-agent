"""Haiku 기반 명령 분류기 - 토큰 최소 소비"""

import json
import anthropic
from bot.config import Config

SYSTEM_PROMPT = """\
You are a command classifier for a Synology NAS automation bot.
Classify the user's message into one of these categories.

Respond ONLY with valid JSON, no other text.

Categories:
- {"type": "simple", "action": "status"} — check container status
- {"type": "simple", "action": "logs", "target": "<container>"} — view logs
- {"type": "simple", "action": "deploy", "target": "<project>"} — deploy existing project
- {"type": "simple", "action": "stop", "target": "<container>"} — stop container
- {"type": "simple", "action": "restart", "target": "<container>"} — restart container
- {"type": "simple", "action": "list_projects"} — list available projects
- {"type": "complex", "description": "<what the user wants>"} — code generation, new app creation, debugging, or anything requiring AI reasoning
- {"type": "chat", "message": "<friendly response>"} — casual conversation, greetings

Examples:
User: "상태 보여줘" → {"type": "simple", "action": "status"}
User: "myapp 로그 보여줘" → {"type": "simple", "action": "logs", "target": "myapp"}
User: "FastAPI로 할일 앱 만들어줘" → {"type": "complex", "description": "FastAPI로 할일 관리 API 앱을 만들어서 Docker로 배포"}
User: "안녕" → {"type": "chat", "message": "안녕하세요! NAS 관리 봇입니다. 무엇을 도와드릴까요?"}
"""

client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)


async def classify(message: str) -> dict:
    """메시지를 분류하고 JSON 딕셔너리를 반환한다."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    text = response.content[0].text.strip()

    # JSON 블록이 ```로 감싸져 있을 수 있음
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    return json.loads(text)
