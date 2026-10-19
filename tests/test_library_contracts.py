import asyncio
import inspect
import json
import operator
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx2
import pytest
from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator, date_filter_query_constructor
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolErrorMiddleware,
)
from langchain.chat_models import init_chat_model
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately, trim_messages
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy, interrupt
from neo4j.exceptions import ConstraintError
from neo4j.time import DateTime as Neo4jDateTime
from pydantic import BaseModel

from tests._conversation_fakes import ToolCallingFakeModel


def test_graphiti_add_episode_accepts_the_arguments_ingestion_relies_on() -> None:
    parameters = inspect.signature(Graphiti.add_episode).parameters

    for name in ("saga", "saga_previous_episode_uuid", "custom_extraction_instructions", "source"):
        assert name in parameters


def test_graphiti_retrieves_episodes_by_saga() -> None:
    assert "saga" in inspect.signature(Graphiti.retrieve_episodes).parameters


def test_graphiti_node_not_found_is_not_a_value_error() -> None:
    assert not issubclass(NodeNotFoundError, ValueError)


def test_graphiti_is_null_filter_renders_without_a_parameter() -> None:
    query = date_filter_query_constructor(
        "e.invalid_at", "$invalid_at_0", ComparisonOperator.is_null
    )

    assert query == "(e.invalid_at IS NULL)"


def test_neo4j_driver_keeps_group_ids_in_one_database() -> None:
    driver = Neo4jDriver("bolt://127.0.0.1:9", "neo4j", "password")

    assert driver.clone(database="twin_group") is driver


def test_tool_error_middleware_filters_by_tool_name() -> None:
    middleware = ToolErrorMiddleware(lambda _error, _request: "unavailable", tools=["recall"])

    assert middleware.on_error is not None
    assert "tools" in inspect.signature(ToolErrorMiddleware.__init__).parameters


def test_init_chat_model_forwards_openai_compatible_provider_options() -> None:
    model = init_chat_model(
        "muse-spark-1.3",
        model_provider="openai",
        api_key="key",
        base_url="https://api.meta.ai/v1",
        reasoning_effort="minimal",
    )

    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == "https://api.meta.ai/v1"
    assert model.reasoning_effort == "minimal"


def test_init_chat_model_forwards_google_reasoning_effort() -> None:
    model = init_chat_model(
        "gemini-3.8-flash",
        model_provider="google_genai",
        api_key="key",
        reasoning_effort="low",
    )

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.reasoning_effort == "low"


def test_init_chat_model_sends_extra_body_fields_in_the_request() -> None:
    sent: list[dict[str, Any]] = []

    def provider(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 0,
                "model": "MiniMax-M2.7-highspeed",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(provider)) as http_client:
        model = init_chat_model(
            "openai:MiniMax-M2.7-highspeed",
            api_key="key",
            base_url="https://api.minimax.io/v1",
            extra_body={"reasoning_split": True},
            http_client=http_client,
        )
        assert isinstance(model, ChatOpenAI)
        model.invoke("hello")

    [body] = sent
    assert body["reasoning_split"] is True
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_init_chat_model_builds_claude_with_the_reply_timeout_and_retries() -> None:
    from langchain_anthropic import ChatAnthropic

    model = init_chat_model(
        "anthropic:claude-haiku-4-5-20251001", api_key="key", timeout=20, max_retries=0
    )

    assert isinstance(model, ChatAnthropic)
    assert (model.model, model.default_request_timeout, model.max_retries) == (
        "claude-haiku-4-5-20251001",
        20,
        0,
    )
    assert model.anthropic_api_key.get_secret_value() == "key"


def test_env_settings_source_exposes_mutable_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_settings.sources import EnvSettingsSource

    from fakeyou.config.settings import Settings

    monkeypatch.setenv("FAKEYOU_REPLY__MODEL", "contract-model")
    source = EnvSettingsSource(Settings, env_prefix="FAKEYOU_", env_nested_delimiter="__")

    assert source.env_vars["fakeyou_reply__model"] == "contract-model"
    source.env_vars = {}
    assert source.env_vars == {}


async def test_checkpointer_reports_whether_a_thread_has_a_checkpoint() -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from fakeyou.db.checkpoint import open_checkpointer
    from tests._fakes_common import DATABASE_URL

    async with open_checkpointer(DATABASE_URL) as saver:
        assert await saver.aget_tuple({"configurable": {"thread_id": "meeting:a"}}) is None
        agent = create_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="ok")]), checkpointer=saver
        )
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "hello"}]},
            {"configurable": {"thread_id": "meeting:a"}},
        )

        assert await saver.aget_tuple({"configurable": {"thread_id": "meeting:a"}}) is not None
        assert await saver.aget_tuple({"configurable": {"thread_id": "meeting:b"}}) is None


def test_owner_note_memory_contracts_cover_correction_path() -> None:
    assert callable(EpisodicNode.save)
    assert callable(EpisodicNode.get_by_uuid)
    assert callable(EpisodicNode.get_by_uuids)
    assert callable(EntityEdge.get_by_uuids)
    assert callable(Neo4jDriver.transaction)
    assert callable(Neo4jDriver.execute_query)
    assert issubclass(ConstraintError, Exception)
    assert callable(Neo4jDateTime.from_native)
    assert callable(Neo4jDateTime.to_native)


class ParticipationDecision(BaseModel):
    decision: Literal["speak", "silent", "clarify"]
    confidence: float
    reason_code: str


async def test_create_agent_response_format_returns_typed_structured_output() -> None:
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ParticipationDecision",
                        "args": {
                            "decision": "speak",
                            "confidence": 0.9,
                            "reason_code": "addressed_by_name",
                        },
                        "id": "structured-1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    agent = create_agent(model, response_format=ParticipationDecision)

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Twin, what is the demo date?"}]}
    )

    structured = result["structured_response"]
    assert isinstance(structured, ParticipationDecision)
    assert structured == ParticipationDecision(
        decision="speak", confidence=0.9, reason_code="addressed_by_name"
    )


async def test_model_call_limit_middleware_stops_run_at_limit() -> None:
    @tool(description="Return a fixed marker for contract tests.")
    def read_marker() -> str:
        return "marker-1"

    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_marker", "args": {}, "id": "call-1", "type": "tool_call"}
                ],
            ),
            AIMessage(content="unreachable second answer"),
        ]
    )
    agent = create_agent(
        model,
        tools=[read_marker],
        middleware=[ModelCallLimitMiddleware(run_limit=1, exit_behavior="end")],
    )

    result = await agent.ainvoke({"messages": [{"role": "user", "content": "Read the marker."}]})

    messages = result["messages"]
    assert any(
        isinstance(message, ToolMessage) and message.content == "marker-1" for message in messages
    )
    assert all(message.content != "unreachable second answer" for message in messages)
    final = messages[-1]
    assert isinstance(final, AIMessage)
    assert final.content == "Model call limits exceeded: run limit (1/1)"


class ConfirmationState(TypedDict):
    topic: str
    answer: str


async def test_interrupt_pauses_graph_until_command_resume_on_same_thread() -> None:
    async def ask_human(state: ConfirmationState) -> dict[str, str]:
        answer: Any = interrupt(f"Confirm the date for {state['topic']}?")
        return {"answer": str(answer)}

    builder = StateGraph(ConfirmationState)
    builder.add_node("ask_human", ask_human)
    builder.add_edge(START, "ask_human")
    builder.add_edge("ask_human", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "interrupt-thread-1"}}

    first = cast(
        dict[str, Any],
        await graph.ainvoke({"topic": "the demo", "answer": ""}, config),
    )

    waiting = await graph.aget_state(config)
    assert first["__interrupt__"][0].value == "Confirm the date for the demo?"
    assert waiting.next == ("ask_human",)
    assert waiting.values["answer"] == ""

    resumed = await graph.ainvoke(Command(resume="Thursday"), config)

    assert resumed["topic"] == "the demo"
    assert resumed["answer"] == "Thursday"
    finished = await graph.aget_state(config)
    assert finished.next == ()


async def test_create_agent_streams_many_message_chunks_per_turn() -> None:
    expected = "Hello streamed world"
    agent = create_agent(FakeListChatModel(responses=[expected]))

    events: list[Any] = []
    async for event in agent.astream(
        {"messages": [{"role": "user", "content": "Say hello."}]}, stream_mode="messages"
    ):
        events.append(event)

    pairs = [cast(tuple[AIMessageChunk, dict[str, Any]], event) for event in events]
    chunks = [chunk for chunk, _metadata in pairs]
    assert len(chunks) > 1
    assert all(isinstance(chunk, AIMessageChunk) for chunk in chunks)
    assert all(isinstance(chunk.content, str) for chunk in chunks)
    texts = [cast(str, chunk.content) for chunk in chunks]
    assert "".join(texts) == expected
    assert texts[0] != expected
    assert texts[0] == expected[: len(texts[0])]
    partials = ["".join(texts[: index + 1]) for index in range(len(texts))]
    assert partials[-1] == expected
    assert all(partial != expected for partial in partials[:-1])
    assert all(expected.startswith(partial) for partial in partials)
    assert all(metadata["langgraph_node"] == "model" for _chunk, metadata in pairs)
    assert chunks[-1].chunk_position == "last"
    assert all(chunk.chunk_position != "last" for chunk in chunks[:-1])


class VisitState(TypedDict):
    marker: str
    visits: Annotated[list[str], operator.add]


async def test_in_memory_checkpointer_round_trips_state_per_thread() -> None:
    async def record_visit(state: VisitState) -> dict[str, list[str]]:
        return {"visits": [state["marker"]]}

    builder = StateGraph(VisitState)
    builder.add_node("record_visit", record_visit)
    builder.add_edge(START, "record_visit")
    builder.add_edge("record_visit", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config_a: RunnableConfig = {"configurable": {"thread_id": "round-trip-thread-a"}}
    config_b: RunnableConfig = {"configurable": {"thread_id": "round-trip-thread-b"}}

    first_a = await graph.ainvoke({"marker": "alpha-first", "visits": []}, config_a)
    second_a = await graph.ainvoke({"marker": "alpha-second", "visits": []}, config_a)
    before_b = await graph.aget_state(config_b)
    first_b = await graph.ainvoke({"marker": "bravo-first", "visits": []}, config_b)

    assert first_a == {"marker": "alpha-first", "visits": ["alpha-first"]}
    assert second_a == {
        "marker": "alpha-second",
        "visits": ["alpha-first", "alpha-second"],
    }
    assert before_b.values == {}
    assert first_b == {"marker": "bravo-first", "visits": ["bravo-first"]}
    state_a = await graph.aget_state(config_a)
    state_b = await graph.aget_state(config_b)
    assert state_a.values == {
        "marker": "alpha-second",
        "visits": ["alpha-first", "alpha-second"],
    }
    assert state_b.values == {"marker": "bravo-first", "visits": ["bravo-first"]}


async def test_postgres_checkpointer_round_trips_state_per_thread(
    migrated_database_url: str,
) -> None:
    from fakeyou.db.checkpoint import open_checkpointer

    async def record_visit(state: VisitState) -> dict[str, list[str]]:
        return {"visits": [state["marker"]]}

    builder = StateGraph(VisitState)
    builder.add_node("record_visit", record_visit)
    builder.add_edge(START, "record_visit")
    builder.add_edge("record_visit", END)
    config_a: RunnableConfig = {"configurable": {"thread_id": "postgres-round-trip-a"}}
    config_b: RunnableConfig = {"configurable": {"thread_id": "postgres-round-trip-b"}}

    async with open_checkpointer(migrated_database_url) as saver:
        graph = builder.compile(checkpointer=saver)
        first_a = await graph.ainvoke({"marker": "alpha-first", "visits": []}, config_a)
        second_a = await graph.ainvoke({"marker": "alpha-second", "visits": []}, config_a)
        before_b = await graph.aget_state(config_b)
        first_b = await graph.ainvoke({"marker": "bravo-first", "visits": []}, config_b)
        state_a = await graph.aget_state(config_a)
        state_b = await graph.aget_state(config_b)

    assert first_a == {"marker": "alpha-first", "visits": ["alpha-first"]}
    assert second_a == {
        "marker": "alpha-second",
        "visits": ["alpha-first", "alpha-second"],
    }
    assert before_b.values == {}
    assert first_b == {"marker": "bravo-first", "visits": ["bravo-first"]}
    assert state_a.values == {
        "marker": "alpha-second",
        "visits": ["alpha-first", "alpha-second"],
    }
    assert state_b.values == {"marker": "bravo-first", "visits": ["bravo-first"]}


def test_summarization_middleware_constructs_with_installed_signature() -> None:
    parameters = inspect.signature(SummarizationMiddleware.__init__).parameters

    for name in (
        "model",
        "trigger",
        "keep",
        "token_counter",
        "summary_prompt",
        "trim_tokens_to_summarize",
    ):
        assert name in parameters

    summary_model = ToolCallingFakeModel(responses=[AIMessage(content="summary text")])
    middleware = SummarizationMiddleware(
        summary_model, trigger=("messages", 4), keep=("messages", 2)
    )

    assert middleware.model is summary_model
    assert middleware.trigger == ("messages", 4)
    assert middleware.keep == ("messages", 2)
    assert middleware.trim_tokens_to_summarize == 4000
    assert middleware.token_counter([HumanMessage(content="latest")]) == 6

    default = SummarizationMiddleware(summary_model)

    assert default.trigger is None
    assert default.keep == ("messages", 20)

    with pytest.raises(ValueError, match="Model profile information is required"):
        SummarizationMiddleware(summary_model, trigger=("fraction", 0.5))


async def test_summarization_middleware_replaces_old_history_with_summary() -> None:
    summary_model = ToolCallingFakeModel(responses=[AIMessage(content="SUMMARY-TEXT")])
    main_model = ToolCallingFakeModel(responses=[AIMessage(content="final answer")])
    agent = create_agent(
        main_model,
        middleware=[
            SummarizationMiddleware(summary_model, trigger=("messages", 4), keep=("messages", 2))
        ],
    )
    history: list[AnyMessage | dict[str, Any]] = [
        HumanMessage(content="old question one"),
        AIMessage(content="old answer one"),
        HumanMessage(content="old question two"),
        AIMessage(content="old answer two"),
        HumanMessage(content="recent question"),
        AIMessage(content="recent answer"),
        HumanMessage(content="newest question"),
    ]

    result = await agent.ainvoke({"messages": history})

    messages = result["messages"]
    summary_content = "Here is a summary of the conversation to date:\n\nSUMMARY-TEXT"
    assert [message.content for message in messages] == [
        summary_content,
        "recent answer",
        "newest question",
        "final answer",
    ]
    assert isinstance(messages[0], HumanMessage)
    assert messages[0].additional_kwargs == {"lc_source": "summarization"}
    assert messages[1] is history[5]
    assert messages[2] is history[6]

    assert main_model.seen != []
    for call in main_model.seen:
        assert [message.content for message in call] == [
            summary_content,
            "recent answer",
            "newest question",
        ]

    assert summary_model.seen != []
    for call in summary_model.seen:
        prompt = "".join(str(message.content) for message in call)
        for summarized in (
            "old question one",
            "old answer one",
            "old question two",
            "old answer two",
            "recent question",
        ):
            assert summarized in prompt
        assert "recent answer" not in prompt
        assert "newest question" not in prompt


def test_official_trim_budget_scale_against_custom_trim() -> None:
    from fakeyou.conversation.middleware import estimate_message_tokens, trim_messages_to_budget

    def build(length: int, count: int) -> list[AnyMessage]:
        messages: list[AnyMessage] = [SystemMessage(content=("sys" + "x" * length)[:length])]
        for index in range(count):
            content = (f"m{index:04d}" + "x" * length)[:length]
            if index % 2 == 0:
                messages.append(HumanMessage(content=content))
            else:
                messages.append(AIMessage(content=content))
        return messages

    long_messages = build(400, 400)

    assert count_tokens_approximately([long_messages[1]]) == 104
    assert count_tokens_approximately([long_messages[2]]) == 106
    assert estimate_message_tokens([long_messages[1]]) == 102
    assert estimate_message_tokens([long_messages[2]]) == 102
    assert count_tokens_approximately(long_messages) == 42105
    assert estimate_message_tokens(long_messages) == 40902

    official_long = trim_messages(
        long_messages,
        max_tokens=32000,
        token_counter="approximate",
        strategy="last",
        include_system=True,
        start_on="human",
    )
    custom_long = trim_messages_to_budget(list(long_messages), max_tokens=32000)

    assert official_long == [long_messages[0], *long_messages[99:]]
    assert len(official_long) == 303
    assert custom_long == [long_messages[0], *long_messages[89:]]
    assert len(custom_long) == 313
    assert count_tokens_approximately(official_long) == 31815
    assert estimate_message_tokens(official_long) == 30906
    assert count_tokens_approximately(custom_long) == 32865
    assert estimate_message_tokens(custom_long) == 31926

    short_messages = build(40, 3000)

    assert count_tokens_approximately([short_messages[1]]) == 14
    assert count_tokens_approximately([short_messages[2]]) == 16
    assert estimate_message_tokens([short_messages[1]]) == 12
    assert estimate_message_tokens([short_messages[2]]) == 12
    assert count_tokens_approximately(short_messages) == 45015
    assert estimate_message_tokens(short_messages) == 36012

    official_short = trim_messages(
        short_messages,
        max_tokens=32000,
        token_counter="approximate",
        strategy="last",
        include_system=True,
        start_on="human",
    )
    custom_short = trim_messages_to_budget(list(short_messages), max_tokens=32000)

    assert official_short == [short_messages[0], *short_messages[869:]]
    assert len(official_short) == 2133
    assert custom_short == [short_messages[0], *short_messages[336:]]
    assert len(custom_short) == 2666
    assert count_tokens_approximately(official_short) == 31995
    assert estimate_message_tokens(official_short) == 25596
    assert count_tokens_approximately(custom_short) == 39991
    assert estimate_message_tokens(custom_short) == 31992


def test_official_trim_drops_latest_message_below_its_own_cost() -> None:
    from fakeyou.conversation.middleware import estimate_message_tokens, trim_messages_to_budget

    def official(
        messages: list[AnyMessage], budget: int, *, start_on_human: bool = True
    ) -> list[BaseMessage]:
        return trim_messages(
            messages,
            max_tokens=budget,
            token_counter="approximate",
            strategy="last",
            include_system=True,
            start_on="human" if start_on_human else None,
        )

    small_latest: list[AnyMessage] = [
        HumanMessage(content=("old1" + "x" * 400)[:400]),
        AIMessage(content=("old2" + "x" * 400)[:400]),
        HumanMessage(content="latest"),
    ]

    assert count_tokens_approximately([small_latest[-1]]) == 6
    assert official(small_latest, 5) == []
    assert official(small_latest, 6) == [small_latest[-1]]
    assert trim_messages_to_budget(list(small_latest), max_tokens=5) == [small_latest[-1]]

    big_latest: list[AnyMessage] = [
        HumanMessage(content="first"),
        AIMessage(content="second"),
        HumanMessage(content=("biglatest" + "x" * 400)[:400]),
    ]

    assert count_tokens_approximately([big_latest[-1]]) == 104
    assert official(big_latest, 103) == []
    assert official(big_latest, 104) == [big_latest[-1]]
    custom_big = trim_messages_to_budget(list(big_latest), max_tokens=10)
    assert custom_big == [big_latest[-1]]
    assert estimate_message_tokens(custom_big) == 102

    ai_latest: list[AnyMessage] = [
        HumanMessage(content=("oldq" + "x" * 400)[:400]),
        AIMessage(content=("olda" + "x" * 400)[:400]),
        HumanMessage(content=("newq" + "x" * 400)[:400]),
        AIMessage(content="fresh answer"),
    ]

    assert count_tokens_approximately([ai_latest[-1]]) == 9
    assert official(ai_latest, 20) == []
    assert official(ai_latest, 20, start_on_human=False) == [ai_latest[-1]]
    assert trim_messages_to_budget(list(ai_latest), max_tokens=20) == [ai_latest[-1]]
    assert official(ai_latest, 120) == [ai_latest[2], ai_latest[3]]


def test_official_trim_system_message_semantics_against_custom_trim() -> None:
    from fakeyou.conversation.middleware import estimate_message_tokens, trim_messages_to_budget

    def official(
        messages: list[AnyMessage], budget: int, *, include_system: bool
    ) -> list[BaseMessage]:
        return trim_messages(
            messages,
            max_tokens=budget,
            token_counter="approximate",
            strategy="last",
            include_system=include_system,
            start_on="human",
        )

    leading_system: list[AnyMessage] = [
        SystemMessage(content=("sys" + "x" * 400)[:400]),
        HumanMessage(content=("oldq" + "x" * 400)[:400]),
        AIMessage(content=("olda" + "x" * 400)[:400]),
        HumanMessage(content="latest"),
    ]

    assert count_tokens_approximately([leading_system[0]]) == 105
    assert estimate_message_tokens([leading_system[0]]) == 102
    assert official(leading_system, 110, include_system=True) == [leading_system[0]]
    assert official(leading_system, 111, include_system=True) == [
        leading_system[0],
        leading_system[3],
    ]
    assert count_tokens_approximately(official(leading_system, 111, include_system=True)) == 111
    assert official(leading_system, 110, include_system=False) == [leading_system[3]]
    assert trim_messages_to_budget(list(leading_system), max_tokens=110) == [
        leading_system[0],
        leading_system[3],
    ]

    oversized_system: list[AnyMessage] = [
        SystemMessage(content=("sys" + "x" * 4000)[:4000]),
        HumanMessage(content="latest"),
    ]

    assert count_tokens_approximately([oversized_system[0]]) == 1005
    kept_oversized = official(oversized_system, 100, include_system=True)
    assert kept_oversized == [oversized_system[0]]
    assert count_tokens_approximately(kept_oversized) == 1005
    assert trim_messages_to_budget(list(oversized_system), max_tokens=100) == oversized_system

    middle_system: list[AnyMessage] = [
        HumanMessage(content=("oldq" + "x" * 400)[:400]),
        SystemMessage(content="mid system"),
        AIMessage(content=("olda" + "x" * 400)[:400]),
        HumanMessage(content="latest"),
    ]

    assert official(middle_system, 130, include_system=True) == [middle_system[3]]
    assert trim_messages_to_budget(list(middle_system), max_tokens=130) == [
        middle_system[1],
        middle_system[2],
        middle_system[3],
    ]


def test_official_trim_tool_pair_at_window_boundary() -> None:
    from fakeyou.conversation.middleware import estimate_message_tokens, trim_messages_to_budget

    def official(
        messages: list[AnyMessage], budget: int, *, start_on_human: bool = True
    ) -> list[BaseMessage]:
        return trim_messages(
            messages,
            max_tokens=budget,
            token_counter="approximate",
            strategy="last",
            include_system=True,
            start_on="human" if start_on_human else None,
        )

    history: list[AnyMessage] = [
        HumanMessage(content=("oldq" + "x" * 400)[:400]),
        AIMessage(
            content="",
            tool_calls=[{"name": "read_marker", "args": {}, "id": "call-1", "type": "tool_call"}],
        ),
        ToolMessage(content=("toolres" + "x" * 400)[:400], tool_call_id="call-1"),
        HumanMessage(content="latest"),
    ]

    assert count_tokens_approximately([history[1]]) == 24
    assert estimate_message_tokens([history[1]]) == 2

    dangling = official(history, 120, start_on_human=False)
    assert dangling == [history[2], history[3]]
    assert isinstance(dangling[0], ToolMessage)
    assert official(history, 120) == [history[3]]
    assert official(history, 140, start_on_human=False) == [
        history[1],
        history[2],
        history[3],
    ]
    assert trim_messages_to_budget(list(history), max_tokens=120) == [
        history[1],
        history[2],
        history[3],
    ]


def test_file_secrets_nested_leaves_require_whole_field_json_file(tmp_path: Path) -> None:
    from pydantic import BaseModel, SecretStr
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class ProbeLeaf(BaseModel):
        api_key: SecretStr | None = None

    class ProbeSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_prefix="PROBE_",
            env_nested_delimiter="__",
            secrets_dir=str(tmp_path),
            env_file=None,
        )
        reply: ProbeLeaf = ProbeLeaf()

    for leaf_name in ("probe_reply__api_key", "PROBE_REPLY__API_KEY", "reply__api_key"):
        (tmp_path / leaf_name).write_text("file-secret", encoding="utf-8")
    assert ProbeSettings().reply.api_key is None

    (tmp_path / "probe_reply").write_text('{"api_key": "file-secret"}', encoding="utf-8")
    probed = ProbeSettings()
    assert probed.reply.api_key is not None
    assert probed.reply.api_key.get_secret_value() == "file-secret"


def test_file_secrets_typo_file_name_is_silently_ignored(tmp_path: Path) -> None:
    from pydantic import BaseModel, SecretStr
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class ProbeLeaf(BaseModel):
        api_key: SecretStr | None = None

    class ProbeSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_prefix="PROBE_",
            env_nested_delimiter="__",
            secrets_dir=str(tmp_path),
            env_file=None,
        )
        reply: ProbeLeaf = ProbeLeaf()

    (tmp_path / "probe_reply__api_ky").write_text("typo-secret", encoding="utf-8")
    probed = ProbeSettings()
    assert probed.reply.api_key is None


def test_graphiti_embedding_dimension_is_the_pinned_import_time_constant() -> None:
    script = "from graphiti_core.embedder.client import EMBEDDING_DIM\nprint(EMBEDDING_DIM)\n"
    environment = {key: value for key, value in os.environ.items() if key != "EMBEDDING_DIM"}
    environment["EMBEDDING_DIM"] = "384"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == "384"


def test_summarization_middleware_accepts_a_token_trigger() -> None:
    summary_model = ToolCallingFakeModel(responses=[AIMessage(content="summary text")])
    middleware = SummarizationMiddleware(
        summary_model, trigger=("tokens", 100), keep=("messages", 2)
    )

    assert middleware.trigger == ("tokens", 100)
    assert middleware.token_counter([HumanMessage(content="hello world")]) == 7


class RetryState(TypedDict):
    attempts: list[int]


class _TransientStepError(Exception):
    pass


def _retrying_graph(
    seen: list[int], failure: type[Exception]
) -> CompiledStateGraph[Any, Any, Any, Any]:
    async def flaky(state: RetryState, *, runtime: Runtime[None]) -> dict[str, list[int]]:
        info = runtime.execution_info
        assert info is not None
        seen.append(info.node_attempt)
        if info.node_attempt < 3:
            raise failure("transient")
        return {"attempts": [*state["attempts"], info.node_attempt]}

    builder = StateGraph(RetryState)
    builder.add_node(
        "flaky",
        flaky,
        retry_policy=RetryPolicy(
            initial_interval=2.0,
            backoff_factor=2.0,
            max_interval=3.0,
            max_attempts=3,
            jitter=True,
            retry_on=_TransientStepError,
        ),
    )
    builder.add_edge(START, "flaky")
    builder.add_edge("flaky", END)
    return builder.compile()


async def test_retry_policy_reruns_a_node_with_its_attempt_number_and_jittered_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []
    slept: list[float] = []
    original_sleep = asyncio.sleep

    async def record_sleep(delay: float, result: Any = None) -> Any:
        if delay > 0:
            slept.append(delay)
        return await original_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        "langgraph.pregel._retry.random", SimpleNamespace(uniform=lambda _low, high: high / 4)
    )

    result = await _retrying_graph(seen, _TransientStepError).ainvoke({"attempts": []})

    assert seen == [1, 2, 3]
    assert result["attempts"] == [3]
    assert slept == [2.25, 3.25]


async def test_retry_policy_does_not_retry_exceptions_outside_retry_on() -> None:
    seen: list[int] = []

    with pytest.raises(LookupError):
        await _retrying_graph(seen, LookupError).ainvoke({"attempts": []})

    assert seen == [1]
