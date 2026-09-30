import asyncio
import socket
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from graphiti_core.nodes import EpisodeType
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from fakeyou.agent import create_meeting_agent
from fakeyou.memory import (
    RECALL_EMPTY_RESULT,
    Neo4jMemoryConfig,
    create_graphiti_memory,
    create_memory_tools,
)
from fakeyou.persona import PersonaProfile
from fakeyou.settings import Settings, require_secret
from fakeyou.voice import OpenAISpeechSynthesizer


def _secret_or_skip(settings_value, name: str) -> str:
    try:
        return require_secret(settings_value, name)
    except ValueError as error:
        pytest.skip(str(error))


def _fail_provider(error: Exception, provider: str) -> None:
    status = getattr(error, "status_code", None)
    detail = f" ({status})" if isinstance(status, int) else ""
    pytest.fail(f"{provider} failed: {type(error).__name__}{detail}", pytrace=False)


@pytest.mark.live
async def test_native_agent_returns_openai_response_and_executes_tool() -> None:
    settings = Settings()
    api_key = _secret_or_skip(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")

    @tool(description="Return the synthetic agenda marker for a meeting test.")
    def read_agenda_marker() -> str:
        return "agenda-marker-731"

    try:
        model = init_chat_model(settings.chat_model, api_key=api_key, timeout=30, max_retries=0)
        agent = create_meeting_agent(model, tools=[read_agenda_marker])
        async with asyncio.timeout(90):
            result = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Call read_agenda_marker and include its exact value "
                                "in your answer."
                            ),
                        }
                    ]
                },
                config={"recursion_limit": 8},
            )
    except Exception as error:
        _fail_provider(error, "OpenAI agent")

    messages = result["messages"]
    assert any(
        isinstance(message, ToolMessage) and message.content == "agenda-marker-731"
        for message in messages
    )
    assert isinstance(messages[-1], AIMessage)
    assert "agenda-marker-731" in str(messages[-1].content)


@pytest.mark.live
async def test_native_openai_tts_synthesizes_pcm_audio() -> None:
    settings = Settings()
    api_key = _secret_or_skip(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")
    synthesizer = OpenAISpeechSynthesizer(api_key=api_key)
    try:
        try:
            async with asyncio.timeout(60):
                audio = await synthesizer.synthesize("Hello from the test meeting.")
        except Exception as error:
            _fail_provider(error, "OpenAI TTS")
    finally:
        await synthesizer.aclose()

    assert len(audio) > 0
    assert len(audio) % 2 == 0


def _neo4j_config_or_skip() -> Neo4jMemoryConfig:
    settings = Settings()
    api_key = _secret_or_skip(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")
    parsed = urlparse(settings.neo4j_uri)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 7687), 2):
            pass
    except OSError:
        pytest.skip("Neo4j is not listening at the configured URI")
    return Neo4jMemoryConfig(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=_secret_or_skip(settings.neo4j_password, "FAKEYOU_NEO4J_PASSWORD"),
        api_key=api_key,
    )


@pytest.mark.live
async def test_graphiti_fact_survives_client_reopen() -> None:
    config = _neo4j_config_or_skip()

    group_id = f"live_provider_{uuid4().hex}"
    marker = f"orchid-{uuid4().hex[:12]}"
    fact = f"Synthetic attendee {marker} prefers jasmine tea."
    episode_uuid: str | None = None
    memory = None
    try:
        async with asyncio.timeout(240):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            result = await memory.add_episode(
                name=f"provider-check-{marker}",
                episode_body=fact,
                source=EpisodeType.text,
                source_description="synthetic live provider check",
                reference_time=datetime.now(UTC),
                group_id=group_id,
            )
            episode_uuid = result.episode.uuid
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            matches = await memory.search(f"{marker} jasmine tea", group_ids=[group_id])
            assert any(marker in edge.fact for edge in matches)
    except Exception as error:
        _fail_provider(error, "Graphiti add/reopen/search")
    finally:
        if episode_uuid is not None and memory is None:
            memory = await asyncio.to_thread(create_graphiti_memory, config)
        if memory is not None:
            try:
                if episode_uuid is not None:
                    async with asyncio.timeout(30):
                        await memory.remove_episode(episode_uuid)
            finally:
                await memory.close()


@pytest.mark.live
async def test_graphiti_persona_namespaces_stay_isolated_after_reopen() -> None:
    config = _neo4j_config_or_skip()
    run = uuid4().hex[:10]
    attendee = f"kestrel-{run}"
    drinks = {"jasmine tea": f"owner_a_{run}", "espresso coffee": f"owner_b_{run}"}
    profiles = {
        drink: PersonaProfile(
            owner_id=owner_id,
            twin_id=f"twin_{run}",
            display_name="Synthetic Twin",
            role="Synthetic live isolation check",
            allowed_tools=frozenset({"recall_meeting_context"}),
        )
        for drink, owner_id in drinks.items()
    }
    episode_uuids: list[str] = []
    extracted: dict[str, list[str]] = {}
    recalled: dict[str, str] = {}
    memory = None
    try:
        async with asyncio.timeout(300):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            for drink, profile in profiles.items():
                result = await memory.add_episode(
                    name=f"persona-isolation-{run}",
                    episode_body=f"Synthetic attendee {attendee} prefers {drink}.",
                    source=EpisodeType.text,
                    source_description="synthetic live persona isolation check",
                    reference_time=datetime.now(UTC),
                    group_id=profile.memory_group,
                )
                episode_uuids.append(result.episode.uuid)
                extracted[drink] = [edge.fact for edge in result.edges]
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            for drink, profile in profiles.items():
                tools = create_memory_tools(memory, group_id=profile.memory_group)
                recall = next(tool for tool in tools if tool.name == "recall_meeting_context")
                recalled[drink] = await recall.ainvoke({"query": f"What does {attendee} prefer?"})
    except Exception as error:
        _fail_provider(error, "Graphiti persona isolation")
    finally:
        if episode_uuids and memory is None:
            memory = await asyncio.to_thread(create_graphiti_memory, config)
        if memory is not None:
            try:
                async with asyncio.timeout(60):
                    for episode_uuid in episode_uuids:
                        await memory.remove_episode(episode_uuid)
            finally:
                await memory.close()

    assert all(extracted.values()), f"Graphiti extraction produced no fact edge: {extracted}"
    assert recalled["jasmine tea"] != RECALL_EMPTY_RESULT
    assert recalled["espresso coffee"] != RECALL_EMPTY_RESULT
    assert "jasmine" in recalled["jasmine tea"] and "espresso" not in recalled["jasmine tea"]
    assert (
        "espresso" in recalled["espresso coffee"] and "jasmine" not in recalled["espresso coffee"]
    )
