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
    ],
)
def test_simple_commands(text, expected_type, expected_action):
    result = classify(text)
    assert result["type"] == expected_type
    if expected_action:
        assert result["action"] == expected_action


@pytest.mark.parametrize(
    "text,action,target",
    [
        ("myapp 로그 보여줘", "logs", "myapp"),
        ("logs nginx", "logs", "nginx"),
        ("nginx 중지", "stop", "nginx"),
        ("stop web_server", "stop", "web_server"),
        ("nginx 재시작", "restart", "nginx"),
        ("restart api", "restart", "api"),
    ],
)
def test_target_actions(text, action, target):
    result = classify(text)
    assert result["type"] == "simple"
    assert result["action"] == action
    assert result["target"] == target


def test_logs_pattern_does_not_match_login():
    """'로그인' 과 '블로그' 가 '로그' 로 오탐되면 안 됨."""
    assert classify("myapp 에 로그인 기능 추가", known_projects={"myapp"}) != {
        "type": "simple",
        "action": "logs",
        "target": "myapp",
    }
    assert classify("Postgres + FastAPI 로 블로그 만들어줘")["type"] != "simple"


@pytest.mark.parametrize(
    "text,name",
    [
        ("myapp 만들어줘: FastAPI 할일 API", "myapp"),
        ("todo-api 새로 만들어줘: 할일 관리 REST API", "todo-api"),
        ("프로젝트 blog 만들어줘: 블로그", "blog"),
    ],
)
def test_new_project_patterns(text, name):
    result = classify(text)
    assert result["type"] == "project"
    assert result["mode"] == "new"
    assert result["name"] == name
    assert result["description"]


def test_continue_project_when_name_is_known():
    result = classify("myapp 에 로그인 기능 추가해줘", known_projects={"myapp"})
    assert result["type"] == "project"
    assert result["mode"] == "continue"
    assert result["name"] == "myapp"
    assert "로그인" in result["task"]


def test_continue_skipped_when_project_unknown():
    result = classify("unknown-proj 에 기능 추가", known_projects=set())
    assert result["type"] == "complex"


def test_simple_action_beats_project_continue():
    """'myapp 로그 보여줘' 는 로그 조회 액션이지, 프로젝트 이어작업이 아니다."""
    result = classify("myapp 로그 보여줘", known_projects={"myapp"})
    assert result["type"] == "simple"
    assert result["action"] == "logs"


def test_complex_fallback():
    result = classify("뭔가 복잡한 일반 요청")
    assert result["type"] == "complex"
    assert result["description"] == "뭔가 복잡한 일반 요청"


def test_chat_returns_message_field():
    result = classify("안녕")
    assert result["type"] == "chat"
    assert result["message"]
