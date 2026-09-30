from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from fakeyou.agent import DEFAULT_PERSONA, ENDING_RULE, create_meeting_agent


def test_default_persona_instructs_brief_spoken_replies() -> None:
    assert "two short sentences" in DEFAULT_PERSONA


def test_default_persona_instructs_casual_spoken_indonesian() -> None:
    assert "casual spoken Indonesian" in DEFAULT_PERSONA


def test_agent_keeps_conversations_isolated_by_thread_id() -> None:
    model = FakeMessagesListChatModel(
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


def test_agent_reopens_sqlite_checkpoint_for_same_thread(tmp_path) -> None:
    database = tmp_path / "checkpoints.sqlite"
    config = {"configurable": {"thread_id": "durable-thread"}}

    with SqliteSaver.from_conn_string(str(database)) as saver:
        first_agent = create_meeting_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="remembered")]),
            checkpointer=saver,
        )
        first_agent.invoke({"messages": [{"role": "user", "content": "keep this"}]}, config)

    with SqliteSaver.from_conn_string(str(database)) as saver:
        second_agent = create_meeting_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="reopened")]),
            checkpointer=saver,
        )
        result = second_agent.invoke(
            {"messages": [{"role": "user", "content": "continue"}]}, config
        )

    contents = [message.content for message in result["messages"]]
    assert contents == ["keep this", "remembered", "continue", "reopened"]


def test_default_persona_ends_with_the_ending_rule() -> None:
    assert DEFAULT_PERSONA.endswith(ENDING_RULE)
    assert "Never end with a question or an offer" in ENDING_RULE
