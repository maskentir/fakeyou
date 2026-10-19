import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import fakeyou.api.chat as api_chat
import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.middleware import HistoryTrimMiddleware, OrphanToolCallMiddleware
from fakeyou.memory.persona import memory_group_for
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
    monkeypatch.setattr(api_chat, "create_memory_tools", create_tools)
    monkeypatch.setattr(api_chat, "create_meeting_agent", create_agent)

    await api_chat.chat("hello", "thread", use_browser=False, use_memory=True, settings=settings)

    assert memory.factory_thread != main_thread
    expected_group = memory_group_for(settings.identity.owner_id, settings.identity.twin_id)
    assert events == ["construct", f"tools:{expected_group}", "agent", "invoke", "close"]


@pytest.mark.asyncio
async def test_chat_binds_only_the_recall_tool_and_writes_no_memory(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = make_graphiti()
    bound: list[str] = []

    def create_agent(*_args: Any, tools: list[Any], **_kwargs: Any) -> Any:
        bound.extend(tool.name for tool in tools)

        class FakeAgent:
            async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return {"messages": [SimpleNamespace(content="ok")]}

        return FakeAgent()

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)
    monkeypatch.setattr(api_chat, "create_meeting_agent", create_agent)

    await api_chat.chat("hello", "thread", use_browser=False, use_memory=True, settings=settings)

    assert bound == ["recall_meeting_context"]
    assert memory.added == []


@pytest.mark.asyncio
async def test_chat_surfaces_an_agent_failure_without_writing_memory(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = make_graphiti()

    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("agent exploded")

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)
    monkeypatch.setattr(api_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.CHAT_FAILED)) as caught:
        await api_chat.chat(
            "hello", "thread", use_browser=False, use_memory=True, settings=settings
        )

    assert caught.value.code is ErrorCode.CHAT_FAILED
    assert str(caught.value) == "chat failed: RuntimeError"
    assert memory.added == []


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
        await api_chat.chat("hello", thread, use_browser=False, use_memory=False, settings=settings)
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
    monkeypatch.setattr(api_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    with pytest.raises(ProviderFault):
        await api_chat.chat(
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
    monkeypatch.setattr(api_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_UNREACHABLE),
    ) as caught:
        await api_chat.chat(
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
    monkeypatch.setattr(api_chat, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    await api_chat.chat("hello", "thread", use_browser=False, use_memory=False, settings=settings)

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
    monkeypatch.setattr(api_chat, "create_meeting_agent", create_agent)

    await api_chat.chat("hello", "thread", use_browser=False, use_memory=False, settings=settings)

    kinds = [type(item) for item in captured["middleware"]]
    assert HistoryTrimMiddleware in kinds
    assert OrphanToolCallMiddleware in kinds


@pytest.mark.asyncio
async def test_chat_returns_the_agent_reply_for_the_caller_to_print(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeAgent:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"messages": [SimpleNamespace(content="reply")]}

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_chat, "create_meeting_agent", lambda *_a, **_k: FakeAgent())

    reply = await api_chat.chat(
        "hello", "thread", use_browser=False, use_memory=False, settings=settings
    )

    assert reply == "reply"
