from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from pydantic import SecretStr

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from tests._sanitize_support import SYNTHETIC_MARKERS, assert_no_synthetic_markers
from tests.test_api_sessions import ApiHarness


class AdminHarness(ApiHarness):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.memory_init_calls = 0
        self.chat_calls: list[dict[str, Any]] = []

    def wire(self, **deps: Any) -> None:
        self.app.state.deps = replace(self.app.state.deps, **deps)


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[AdminHarness]:
    alpha, *_ = SYNTHETIC_MARKERS
    settings.transport.redis.url = SecretStr(f"redis://user:{alpha}@cache.synthetic:6379/0")
    harness = AdminHarness(settings)

    async def count_memory_init() -> None:
        harness.memory_init_calls += 1

    async def record_chat_turn(**kwargs: Any) -> str:
        harness.chat_calls.append(kwargs)
        return f"{kwargs['message']}: done"

    harness.wire(memory_init=count_memory_init, chat_turn=record_chat_turn)
    try:
        yield harness
    finally:
        await harness.aclose()


async def test_memory_init_requires_admin_and_runs_the_initializer(api: AdminHarness) -> None:
    assert (await api.post("/v1/admin/memory-init", principal="owner")).status_code == 403
    assert (await api.post("/v1/admin/memory-init", principal=None)).status_code == 401

    response = await api.post("/v1/admin/memory-init", principal="admin")

    assert response.status_code == 200
    assert response.json() == {"status": "initialized"}
    assert api.memory_init_calls == 1


async def test_chat_turn_runs_the_cli_handler_behind_the_admin_token(api: AdminHarness) -> None:
    response = await api.post(
        "/v1/admin/chat/turns",
        principal="admin",
        json={"thread_id": "debug-1", "message": "hello"},
    )

    assert response.status_code == 200
    assert response.json() == {"reply": "hello: done"}
    assert api.chat_calls == [
        {"thread_id": "debug-1", "message": "hello", "use_browser": False, "use_memory": False}
    ]


async def test_chat_turn_forwards_the_memory_flag_from_the_body(api: AdminHarness) -> None:
    await api.post(
        "/v1/admin/chat/turns",
        principal="admin",
        json={"thread_id": "debug-1", "message": "hello", "memory": True},
    )

    assert api.chat_calls[0]["use_memory"] is True


async def test_chat_turn_rejects_browser_mode_as_an_unknown_field(api: AdminHarness) -> None:
    response = await api.post(
        "/v1/admin/chat/turns",
        principal="admin",
        json={"thread_id": "debug-1", "message": "hello", "browser": True},
    )

    assert response.status_code == 400
    assert api.chat_calls == []


async def test_chat_turn_is_not_reachable_by_the_owner(api: AdminHarness) -> None:
    response = await api.post(
        "/v1/admin/chat/turns",
        principal="owner",
        json={"thread_id": "debug-1", "message": "hello"},
    )

    assert response.status_code == 403
    assert api.chat_calls == []


async def test_admin_check_is_admin_only(api: AdminHarness) -> None:
    assert (await api.get("/v1/admin/check", principal="owner")).status_code == 403
    assert (await api.get("/v1/admin/check", principal=None)).status_code == 401


async def test_admin_check_never_returns_a_raw_endpoint_secret(api: AdminHarness) -> None:
    response = await api.get("/v1/admin/check", principal="admin")

    assert response.status_code == 200
    assert_no_synthetic_markers(response.json())
    assert SYNTHETIC_MARKERS[0] not in response.text


async def test_openapi_has_no_forbidden_routes(api: AdminHarness) -> None:
    paths = set((await api.get("/openapi.json")).json()["paths"])
    forbidden = ("voiceprint", "/db", "purge", "calibrate", "diarize", "internal")

    assert not [path for path in paths if any(word in path for word in forbidden)]


async def test_memory_init_failure_is_not_reported_as_initialized(
    settings: Settings,
) -> None:
    harness = AdminHarness(settings)

    async def failing_init() -> None:
        raise FakeYouError(ErrorCode.MEMORY_UNAVAILABLE, reason="index setup failed")

    harness.wire(memory_init=failing_init)
    try:
        response = await harness.post("/v1/admin/memory-init", principal="admin")
    finally:
        await harness.aclose()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "memory.unavailable"


def test_the_chat_handler_belongs_to_the_api_not_the_cli() -> None:
    import fakeyou.api.chat as api_chat
    import fakeyou.cli.chat as cli_chat

    assert callable(api_chat.chat)
    assert not hasattr(cli_chat, "chat")


def test_no_api_module_imports_the_cli_layer() -> None:
    import ast
    from pathlib import Path

    root = Path(api_module_file()).parent
    offenders = [
        path.name
        for path in root.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("fakeyou.cli")
    ]

    assert offenders == []


def api_module_file() -> str:
    import fakeyou.api.admin as admin_module

    return admin_module.__file__
