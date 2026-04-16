import pytest

from executor.projects import ProjectRegistry, ProjectError, validate_name


@pytest.fixture
def registry(tmp_path):
    return ProjectRegistry(str(tmp_path / "registry.db"))


async def test_create_and_get(registry):
    p = await registry.create("myapp", "a todo app")
    assert p.name == "myapp"
    assert p.session_id
    assert p.created_at == p.updated_at

    got = await registry.get("myapp")
    assert got == p


async def test_duplicate_create_raises(registry):
    await registry.create("myapp", "first")
    with pytest.raises(ProjectError):
        await registry.create("myapp", "second")


async def test_get_missing_returns_none(registry):
    assert await registry.get("nope") is None


async def test_list_orders_by_updated_at(registry):
    a = await registry.create("aaa", "first")
    b = await registry.create("bbb", "second")
    await registry.record_task(a.name, "update a", "done", True)

    projects = await registry.list()
    assert [p.name for p in projects] == ["aaa", "bbb"]


async def test_record_task_and_history(registry):
    p = await registry.create("myapp", "todo")
    await registry.record_task(p.name, "add login", "ok", True)
    await registry.record_task(p.name, "add logout", "also ok", False)

    history = await registry.history(p.name)
    assert len(history) == 2
    # 최신 먼저 (id DESC)
    assert history[0].task == "add logout"
    assert history[0].deployed is False
    assert history[1].deployed is True


async def test_delete_cascades_tasks(registry):
    p = await registry.create("myapp", "todo")
    await registry.record_task(p.name, "x", "y", True)
    assert await registry.delete(p.name) is True
    assert await registry.get(p.name) is None
    assert await registry.history(p.name) == []


async def test_delete_missing_returns_false(registry):
    assert await registry.delete("nope") is False


@pytest.mark.parametrize(
    "bad",
    ["A", "1bad", "bad!", "x", "a" * 32, "has space"],
)
def test_validate_name_rejects_bad(bad):
    with pytest.raises(ProjectError):
        validate_name(bad)


@pytest.mark.parametrize("good", ["ab", "myapp", "todo-api", "my_app", "x1"])
def test_validate_name_accepts_good(good):
    validate_name(good)  # no exception


async def test_set_repo_url_persists(registry):
    await registry.create("myapp", "x")
    await registry.set_repo_url("myapp", "https://github.com/me/myapp")
    p = await registry.get("myapp")
    assert p.repo_url == "https://github.com/me/myapp"


async def test_project_defaults_repo_url_to_none(registry):
    p = await registry.create("myapp", "x")
    assert p.repo_url is None
