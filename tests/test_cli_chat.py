import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import fakeyou.cli.chat as cli_chat
import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.middleware import HistoryTrimMiddleware, OrphanToolCallMiddleware
from tests._error_support import message_pattern
from tests._memory_fakes import make_graphiti


@pytest.mark.asyncio
async def test_chat_waits_for_memory_ready_before_exposing_tools(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    main_thread = threading.get_ident()
    memory = make_graphiti()
    memory.events = events

    def create_memory(_config: Any, **_kwargs: Any) -> MagicMock:
        events.append("construct")
        memory.factory_thread = threading.get_ident()
        return memory

    def create_tools(_memory: Any, *, group_id: str, recall: Any) -> list[Any]:
        del recall
        events.append(f"tools:{group_id}")
        return []

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            events.append("invoke")
            return {"messages": [SimpleNamespace(content="ok")]}

    def create_agent(*_args: Any, **_kwargs: Any) -> FakeAgent:
        events.append("agent")
        return FakeAgent()

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", create_memory)
    monkeypatch.setattr(cli_chat, "create_memory_tools", create_tools)
    monkeypatch.setattr(cli_chat, "create_meeting_agent", create_agent)

    await cli_chat.chat("hello", "thread", use_browser=False, use_memory=True, settings=settings)

    assert memory.factory_thread != main_thread
    assert events == ["construct", "tools:test-group", "agent", "invoke", "close"]


@pytest.mark.asyncio
async def test_chat_turn_flush_persists_a_lone_short_fact(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = make_graphiti()
    captured: dict[str, Any] = {}

    def create_tools(_memory: Any, *, group_id: str, recall: Any) -> list[Any]:
        tools = memory_module.create_memory_tools(_memory, group_id=group_id, recall=recall)
        captured["tools"] = tools
        return tools

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            [remember, _recall] = captured["tools"]
            await remember.ainvoke({"fact": "Budi: The demo moved to Thursday."})
            return {"messages": [SimpleNamespace(content="ok")]}

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)
    monkeypatch.setattr(cli_chat, "create_memory_tools", create_tools)
    monkeypatch.setattr(cli_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    await cli_chat.chat("hello", "thread", use_browser=False, use_memory=True, settings=settings)

    assert len(memory.added) == 1
    assert "Budi: The demo moved to Thursday." in memory.added[0]["episode_body"]


@pytest.mark.asyncio
async def test_chat_flushes_pending_facts_when_the_agent_raises(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = make_graphiti()
    captured: dict[str, Any] = {}

    def create_tools(_memory: Any, *, group_id: str, recall: Any) -> list[Any]:
        tools = memory_module.create_memory_tools(_memory, group_id=group_id, recall=recall)
        captured["tools"] = tools
        return tools

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            [remember, _recall] = captured["tools"]
            await remember.ainvoke({"fact": "Budi: The demo moved to Thursday."})
            raise RuntimeError("agent exploded")

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)
    monkeypatch.setattr(cli_chat, "create_memory_tools", create_tools)
    monkeypatch.setattr(cli_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.CHAT_FAILED)) as caught:
        await cli_chat.chat(
            "hello", "thread", use_browser=False, use_memory=True, settings=settings
        )

    assert caught.value.code is ErrorCode.CHAT_FAILED
    assert str(caught.value) == "chat failed: RuntimeError"
    assert len(memory.added) == 1
    assert "Budi: The demo moved to Thursday." in memory.added[0]["episode_body"]


@pytest.mark.asyncio
async def test_chat_surfaces_flush_failure_after_a_successful_turn(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = make_graphiti()
    memory.errors = {0: RuntimeError("episode store unavailable")}
    captured: dict[str, Any] = {}

    def create_tools(_memory: Any, *, group_id: str, recall: Any) -> list[Any]:
        tools = memory_module.create_memory_tools(_memory, group_id=group_id, recall=recall)
        captured["tools"] = tools
        return tools

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            [remember, _recall] = captured["tools"]
            await remember.ainvoke({"fact": "Budi: The demo moved to Thursday."})
            return {"messages": [SimpleNamespace(content="ok")]}

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)
    monkeypatch.setattr(cli_chat, "create_memory_tools", create_tools)
    monkeypatch.setattr(cli_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.CHAT_FAILED)) as caught:
        await cli_chat.chat(
            "hello", "thread", use_browser=False, use_memory=True, settings=settings
        )

    assert caught.value.code is ErrorCode.CHAT_FAILED
    assert str(caught.value) == "chat failed: RuntimeError"


@pytest.mark.parametrize("thread", ["", "bad thread", "bad:thread", "x" * 129])
async def test_chat_rejects_invalid_thread(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, thread: str
) -> None:
    monkeypatch.setattr(
        providers,
        "init_chat_model",
        lambda *_args, **_kwargs: pytest.fail("thread must be validated first"),
    )
    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.CHAT_THREAD_INVALID),
    ) as caught:
        await cli_chat.chat("hello", thread, use_browser=False, use_memory=False, settings=settings)
    assert caught.value.code is ErrorCode.CHAT_THREAD_INVALID


@pytest.mark.asyncio
async def test_chat_reraises_provider_faults_for_the_top_level_mapper(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise ProviderFault("llm", "unavailable")

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    with pytest.raises(ProviderFault):
        await cli_chat.chat(
            "hello", "thread", use_browser=False, use_memory=False, settings=settings
        )


@pytest.mark.asyncio
async def test_chat_maps_agent_database_errors_to_unreachable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg import OperationalError

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise OperationalError("connection reset by peer")

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_UNREACHABLE),
    ) as caught:
        await cli_chat.chat(
            "hello", "thread", use_browser=False, use_memory=False, settings=settings
        )

    assert caught.value.code is ErrorCode.DATABASE_UNREACHABLE


@pytest.mark.asyncio
async def test_chat_threads_database_timeouts_to_the_checkpointer(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    postgres_checkpoints: dict[str, Any],
) -> None:
    from urllib.parse import parse_qsl, urlsplit

    settings.database.connect_timeout_seconds = 7.0
    settings.database.statement_timeout_seconds = 9.0
    settings.database.keepalive_idle_seconds = 11.0

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"messages": [SimpleNamespace(content="ok")]}

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    await cli_chat.chat("hello", "thread", use_browser=False, use_memory=False, settings=settings)

    (conninfo,) = postgres_checkpoints
    params = dict(parse_qsl(urlsplit(conninfo).query))
    assert params["connect_timeout"] == "7"
    assert "statement_timeout=9000" in params["options"]
    assert params["keepalives_idle"] == "11"


@pytest.mark.asyncio
async def test_chat_agent_trims_history_and_patches_orphaned_tool_calls(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"messages": [SimpleNamespace(content="ok")]}

    def create_agent(*_args: Any, **kwargs: Any) -> FakeAgent:
        captured["middleware"] = kwargs["middleware"]
        return FakeAgent()

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_chat, "create_meeting_agent", create_agent)

    await cli_chat.chat("hello", "thread", use_browser=False, use_memory=False, settings=settings)

    kinds = [type(item) for item in captured["middleware"]]
    assert HistoryTrimMiddleware in kinds
    assert OrphanToolCallMiddleware in kinds
