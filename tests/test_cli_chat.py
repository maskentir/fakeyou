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
from tests.fakes import (
    make_graphiti,
)


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

    with pytest.raises(RuntimeError, match="agent exploded"):
        await cli_chat.chat(
            "hello", "thread", use_browser=False, use_memory=True, settings=settings
        )

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

    with pytest.raises(RuntimeError, match="episode store unavailable"):
        await cli_chat.chat(
            "hello", "thread", use_browser=False, use_memory=True, settings=settings
        )


@pytest.mark.parametrize("thread", ["", "bad thread", "bad:thread", "x" * 129])
async def test_chat_rejects_invalid_thread(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, thread: str
) -> None:
    monkeypatch.setattr(
        providers,
        "init_chat_model",
        lambda *_args, **_kwargs: pytest.fail("thread must be validated first"),
    )
    with pytest.raises(FakeYouError) as caught:
        await cli_chat.chat("hello", thread, use_browser=False, use_memory=False, settings=settings)
    assert caught.value.code is ErrorCode.CHAT_THREAD_INVALID
