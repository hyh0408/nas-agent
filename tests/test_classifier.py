import pytest

from bot.classifier import classify


@pytest.mark.parametrize(
    "text,expected_type,expected_action",
    [
        ("상태 보여줘", "simple", "status"),
        ("컨테이너 상태", "simple", "status"),
        ("CPU 메모리 상태", "simple", "system_status"),
        ("NAS 리소스 확인", "simple", "system_status"),
        ("디스크 사용량", "simple", "system_status"),
        ("프로젝트 목록", "simple", "list_projects"),
        ("프로젝트들 보여줘", "simple", "list_projects"),
        ("안녕", "chat", None),
        ("hi", "chat", None),
        ("FastAPI로 할일 앱 만들어줘", "complex", None),
        ("nginx reverse proxy 설정 좀", "complex", None),
    ],
)
async def test_no_target_actions(text, expected_type, expected_action):
    result = await classify(text)
    assert result["type"] == expected_type
    if expected_action:
        assert result["action"] == expected_action


@pytest.mark.parametrize(
    "text,action,target",
    [
        ("myapp 로그 보여줘", "logs", "myapp"),
        ("logs nginx", "logs", "nginx"),
        ("myapp 배포해줘", "deploy", "myapp"),
        ("deploy todo-api", "deploy", "todo-api"),
        ("nginx 중지", "stop", "nginx"),
        ("stop web_server", "stop", "web_server"),
        ("nginx 재시작", "restart", "nginx"),
        ("restart api", "restart", "api"),
    ],
)
async def test_target_actions(text, action, target):
    result = await classify(text)
    assert result["type"] == "simple"
    assert result["action"] == action
    assert result["target"] == target


async def test_target_action_without_target_falls_through_to_complex():
    """'로그' 만 있고 컨테이너 이름이 없으면 complex 로 떨어져야 한다."""
    result = await classify("로그 좀 보고 싶은데")
    assert result["type"] == "complex"


async def test_complex_preserves_full_description():
    result = await classify("Postgres + FastAPI 로 블로그 만들어줘")
    assert result["type"] == "complex"
    assert result["description"] == "Postgres + FastAPI 로 블로그 만들어줘"


async def test_chat_returns_message_field():
    result = await classify("안녕")
    assert result["type"] == "chat"
    assert "message" in result
    assert result["message"]
