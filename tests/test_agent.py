import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

import fakeyou.orchestration.assembly as assembly_module
import fakeyou.orchestration.providers as providers_module
from fakeyou.conversation.agent import DEFAULT_PERSONA, ENDING_RULE, create_meeting_agent
from fakeyou.conversation.session import SessionConfig
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.assembly import AgentWiring, LoopBehavior, open_conversation
from tests.fakes import ToolCallingFakeModel


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


@pytest.mark.asyncio
async def test_agent_reopens_postgres_checkpoint_for_same_thread(
    migrated_database_url: str,
) -> None:
    config = {"configurable": {"thread_id": "durable-thread"}}
    async with open_checkpointer(migrated_database_url) as saver:
        first_agent = create_meeting_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="remembered")]),
            checkpointer=saver,
        )
        await first_agent.ainvoke({"messages": [{"role": "user", "content": "keep this"}]}, config)

    async with open_checkpointer(migrated_database_url) as saver:
        second_agent = create_meeting_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="reopened")]),
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


def _tagged_greeting(text: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        id="mount-greeting",
        additional_kwargs={"meeting_event": {"source": "meeting_audio"}},
    )


async def test_production_agent_short_circuits_greeting_without_model(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content="from model")])
    monkeypatch.setattr(providers_module, "init_chat_model", lambda *_args, **_kwargs: model)
    built: list = []

    def recording_agent(agent_model, **kwargs):
        agent = create_meeting_agent(agent_model, **kwargs)
        built.append(agent)
        return agent

    monkeypatch.setattr(assembly_module, "create_meeting_agent", recording_agent)
    config = SessionConfig(
        owner_id="owner-1",
        twin_id="twin-1",
        meeting_id="mount-1",
        link="https://meet.google.com/aaa-bbbb-ccc",
        twin_name="Mount Twin",
    )
    profile = PersonaProfile.model_validate(
        {
            "owner_id": "owner-1",
            "twin_id": "twin-1",
            "display_name": "Mount Twin",
            "role": "Test role",
            "language": "en",
        }
    )
    async with open_conversation(
        settings,
        config,
        agent=AgentWiring(profile=profile),
        behavior=LoopBehavior(on_transition=lambda *args: None),
    ):
        [agent] = built
        result = await agent.ainvoke(
            {"messages": [_tagged_greeting("Hi, Sam here.")]},
            {"configurable": {"thread_id": "mount-greet"}},
        )
        assert result["messages"][-1].content == "Hello."
        assert model.seen == []
        mixed = await agent.ainvoke(
            {"messages": [_tagged_greeting("Hello, what time is the demo?")]},
            {"configurable": {"thread_id": "mount-mixed"}},
        )
        assert mixed["messages"][-1].content == "from model"
        assert len(model.seen) >= 1
