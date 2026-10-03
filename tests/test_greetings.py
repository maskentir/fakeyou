import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.agent import create_meeting_agent
from fakeyou.greetings import create_greeting_middleware, neutral_greeting
from tests.fakes import ToolCallingFakeModel

CASES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "greeting-cases.json").read_text(encoding="utf-8")
)


def tagged_event(text: str, message_id: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        id=message_id,
        additional_kwargs={
            "meeting_event": {
                "timestamp": "2026-10-03T00:00:00+00:00",
                "source": "meeting_audio",
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        },
    )


def recording_probe(calls: list[str]) -> Any:
    @tool(description="Record probe calls during greeting tests.")
    def probe_tool(query: str) -> str:
        calls.append(query)
        return "recorded"

    return probe_tool


@pytest.mark.parametrize("text", CASES["positives_id"])
def test_id_greeting_returns_fixed_reply(text: str) -> None:
    assert neutral_greeting(text, "id") == CASES["outputs"]["id"]


@pytest.mark.parametrize("text", CASES["positives_en"])
def test_en_greeting_returns_fixed_reply(text: str) -> None:
    assert neutral_greeting(text, "en") == CASES["outputs"]["en"]


@pytest.mark.parametrize("text", CASES["negatives"])
def test_mixed_input_returns_none(text: str) -> None:
    assert neutral_greeting(text, "id") is None


def test_en_mixed_input_returns_none() -> None:
    assert neutral_greeting("Hello, what time is the demo?", "en") is None
    assert neutral_greeting("Hi, approve the contract", "en") is None
    assert neutral_greeting("Hello, I am not Sam.", "en") is None


def test_unknown_language_returns_none() -> None:
    assert neutral_greeting(CASES["id_spot"], "fr") is None


async def test_greeting_short_circuits_before_model() -> None:
    model = ToolCallingFakeModel(responses=[])
    calls: list[str] = []
    agent = create_meeting_agent(
        model,
        tools=[recording_probe(calls)],
        middleware=[create_greeting_middleware("id")],
        checkpointer=InMemorySaver(),
    )
    result = await agent.ainvoke(
        {"messages": [tagged_event(CASES["id_spot"], "greeting-1")]},
        config={"configurable": {"thread_id": "greeting-short-circuit"}},
    )
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == CASES["outputs"]["id"]
    assert model.seen == []
    assert model.i == 0
    assert calls == []


async def test_english_greeting_short_circuits() -> None:
    model = ToolCallingFakeModel(responses=[])
    agent = create_meeting_agent(
        model,
        middleware=[create_greeting_middleware("en")],
        checkpointer=InMemorySaver(),
    )
    result = await agent.ainvoke(
        {"messages": [tagged_event(CASES["en_spot"], "greeting-en-1")]},
        config={"configurable": {"thread_id": "greeting-short-circuit-en"}},
    )
    assert result["messages"][-1].content == CASES["outputs"]["en"]
    assert model.seen == []


async def test_follow_up_reaches_model_with_greeting_context() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content=CASES["follow_answer"])])
    agent = create_meeting_agent(
        model,
        middleware=[create_greeting_middleware("id")],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "greeting-follow-up"}}
    first = await agent.ainvoke(
        {"messages": [tagged_event(CASES["id_spot"], "greeting-2")]}, config=config
    )
    assert first["messages"][-1].content == CASES["outputs"]["id"]
    second = await agent.ainvoke(
        {"messages": [tagged_event(CASES["negatives"][0], "question-1")]}, config=config
    )
    assert second["messages"][-1].content == CASES["follow_answer"]
    assert len(model.seen) >= 1
    seen_text = " ".join(
        message.content for message in model.seen[-1] if isinstance(message.content, str)
    )
    assert CASES["id_spot"] in seen_text
    assert CASES["outputs"]["id"] in seen_text
    assert CASES["negatives"][0] in seen_text


async def test_old_greeting_in_history_does_not_retrigger() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content=CASES["follow_answer"])])
    agent = create_meeting_agent(
        model,
        middleware=[create_greeting_middleware("id")],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "greeting-no-retrigger"}}
    await agent.ainvoke({"messages": [tagged_event(CASES["id_spot"], "greeting-3")]}, config=config)
    assert model.seen == []
    result = await agent.ainvoke(
        {"messages": [tagged_event(CASES["negatives"][6], "follow-1")]}, config=config
    )
    assert result["messages"][-1].content == CASES["follow_answer"]
    assert len(model.seen) >= 1


async def test_two_message_batch_is_not_short_circuited() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content=CASES["batch_answer"])])
    agent = create_meeting_agent(
        model,
        middleware=[create_greeting_middleware("id")],
        checkpointer=InMemorySaver(),
    )
    result = await agent.ainvoke(
        {
            "messages": [
                tagged_event(CASES["id_spot"], "batch-1"),
                tagged_event(CASES["negatives"][0], "batch-2"),
            ]
        },
        config={"configurable": {"thread_id": "greeting-batch"}},
    )
    assert result["messages"][-1].content == CASES["batch_answer"]
    assert len(model.seen) >= 1


async def test_untagged_greeting_reaches_model() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content=CASES["batch_answer"])])
    agent = create_meeting_agent(
        model,
        middleware=[create_greeting_middleware("id")],
        checkpointer=InMemorySaver(),
    )
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=CASES["id_spot"], id="untagged-1")]},
        config={"configurable": {"thread_id": "greeting-untagged"}},
    )
    assert result["messages"][-1].content == CASES["batch_answer"]
    assert len(model.seen) >= 1
