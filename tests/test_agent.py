import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import (
    DEFAULT_PERSONA,
    ENDING_RULE,
    create_meeting_agent,
)
from fakeyou.db.checkpoint import open_checkpointer
from tests._conversation_fakes import ToolCallingFakeModel


def test_default_persona_instructs_brief_spoken_replies() -> None:
    assert "two short sentences" in DEFAULT_PERSONA


def test_default_persona_instructs_casual_spoken_indonesian() -> None:
    assert "casual spoken Indonesian" in DEFAULT_PERSONA


def test_agent_keeps_conversations_isolated_by_thread_id() -> None:
    model = ToolCallingFakeModel(
        responses=[AIMessage(content="first"), AIMessage(content="second")]
    )
    agent = create_meeting_agent(model, checkpointer=InMemorySaver())

    first = agent.invoke(
        {"messages": [{"role": "user", "content": "alpha"}]},
        {"configurable": {"thread_id": "thread-a"}},
    )
    second = agent.invoke(
        {"messages": [{"role": "user", "content": "beta"}]},
        {"configurable": {"thread_id": "thread-b"}},
    )

    assert first["messages"][-1].content == "first"
    assert second["messages"][-1].content == "second"
    assert all(message.content != "alpha" for message in second["messages"])


@pytest.mark.asyncio
async def test_agent_reopens_postgres_checkpoint_for_same_thread(
    migrated_database_url: str,
) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": "durable-thread"}}
    async with open_checkpointer(migrated_database_url) as saver:
        first_agent = create_meeting_agent(
            ToolCallingFakeModel(responses=[AIMessage(content="remembered")]),
            checkpointer=saver,
        )
        await first_agent.ainvoke({"messages": [{"role": "user", "content": "keep this"}]}, config)

    async with open_checkpointer(migrated_database_url) as saver:
        second_agent = create_meeting_agent(
            ToolCallingFakeModel(responses=[AIMessage(content="reopened")]),
            checkpointer=saver,
        )
        result = await second_agent.ainvoke(
            {"messages": [{"role": "user", "content": "continue"}]}, config
        )

    contents = [message.content for message in result["messages"]]
    assert contents == ["keep this", "remembered", "continue", "reopened"]


def test_default_persona_ends_with_the_ending_rule() -> None:
    assert DEFAULT_PERSONA.endswith(ENDING_RULE)
    assert "Never end with a question or an offer" in ENDING_RULE
