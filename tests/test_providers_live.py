import asyncio
import json
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
from fakeyou.cli import memory_config
from fakeyou.conversation import TranscriptEvent
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import MeetingIngestion
from fakeyou.journal import ProviderJournal
from fakeyou.memory import (
    RECALL_EMPTY_RESULT,
    Neo4jMemoryConfig,
    create_graphiti_memory,
    create_memory_tools,
    forget_episode,
    forget_meeting,
    remember_owner_note,
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
    parsed = urlparse(settings.neo4j_uri)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 7687), 2):
            pass
    except OSError:
        pytest.skip("Neo4j is not listening at the configured URI")
    try:
        return memory_config(settings)
    except ValueError as error:
        pytest.skip(str(error))


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
    employers = {"Heronfield Analytics": f"owner_a_{run}", "Larkmoor Logistics": f"owner_b_{run}"}
    profiles = {
        employer: PersonaProfile(
            owner_id=owner_id,
            twin_id=f"twin_{run}",
            display_name="Synthetic Twin",
            role="Synthetic live isolation check",
            allowed_tools=frozenset({"recall_meeting_context"}),
        )
        for employer, owner_id in employers.items()
    }
    episode_uuids: list[str] = []
    extracted: dict[str, list[str]] = {}
    recalled: dict[str, str] = {}
    leftovers: list[int] = []
    memory = None
    try:
        async with asyncio.timeout(300):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            for employer, profile in profiles.items():
                result = await memory.add_episode(
                    name=f"persona-isolation-{run}",
                    episode_body=f"Synthetic attendee {attendee} works at {employer}.",
                    source=EpisodeType.text,
                    source_description="synthetic live persona isolation check",
                    reference_time=datetime.now(UTC),
                    group_id=profile.memory_group,
                )
                episode_uuids.append(result.episode.uuid)
                extracted[employer] = [edge.fact for edge in result.edges]
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            for employer, profile in profiles.items():
                recalled[employer] = await _recall(
                    memory, profile.memory_group, f"Where does {attendee} work?"
                )
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
                    for profile in profiles.values():
                        leftovers.append(await _group_node_count(memory, profile.memory_group))
            finally:
                await memory.close()

    assert all(extracted.values()), f"Graphiti extraction produced no fact edge: {extracted}"
    first, second = employers
    assert first in recalled[first] and second.split()[0] not in recalled[first]
    assert second in recalled[second] and first.split()[0] not in recalled[second]
    assert leftovers == [0, 0]


async def _group_node_count(memory, group_id: str) -> int:
    result = await memory.driver.execute_query(
        "MATCH (n {group_id: $group_id}) RETURN count(n) AS count", group_id=group_id
    )
    return result.records[0]["count"]


async def _recall(memory, group_id: str, query: str) -> str:
    tools = create_memory_tools(memory, group_id=group_id)
    recall = next(tool for tool in tools if tool.name == "recall_meeting_context")
    return await recall.ainvoke({"query": query})


@pytest.mark.live
async def test_meeting_ingestion_recall_correction_and_forget_roundtrip() -> None:
    config = _neo4j_config_or_skip()
    run = uuid4().hex[:10]
    project = f"Orchid{run}"
    meeting_id = f"live-ingest-{run}"
    profile = PersonaProfile(
        owner_id=f"owner_{run}",
        twin_id=f"twin_{run}",
        display_name="Rani Twin",
        role="Synthetic live ingestion check",
        allowed_tools=frozenset({"recall_meeting_context"}),
        ingest_transcripts=True,
    )
    lines: list[str] = []
    memory = None
    note_uuid: str | None = None
    ingested = False
    try:
        async with asyncio.timeout(420):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            ingestion = MeetingIngestion(
                memory,
                group_id=profile.memory_group,
                meeting_id=meeting_id,
                twin_name=profile.display_name,
                faults=FaultReporter(meeting_id=meeting_id, log=lines.append),
                journal=ProviderJournal(meeting_id, emit=lines.append),
                drain_timeout_seconds=240,
            )
            ingestion.add_twin_speech(
                f"Gimana kalau demo customer {project} digeser ke hari Kamis?"
            )
            started = datetime.now(UTC)
            for index, text in enumerate(
                (
                    f"Oke, setuju, demo customer {project} kita pindah ke hari Kamis.",
                    f"Nanti aku kabari tim sales soal demo {project}.",
                )
            ):
                ingestion.add_transcript(
                    TranscriptEvent(
                        id=f"{run}-{index}",
                        timestamp=started.isoformat(),
                        source="meeting_audio",
                        text=text,
                        finalized=True,
                    )
                )
            await ingestion.close()
            ingested = True
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            recalled = await _recall(memory, profile.memory_group, f"{project} customer demo day")
            note = await remember_owner_note(
                memory,
                group_id=profile.memory_group,
                text=f"Update dari Rani: demo customer {project} dipindah ke hari Jumat.",
            )
            note_uuid = note.episode.uuid
            corrected = await _recall(memory, profile.memory_group, f"{project} customer demo day")
            removed = await forget_meeting(
                memory, group_id=profile.memory_group, meeting_id=meeting_id
            )
            await forget_episode(memory, group_id=profile.memory_group, episode_uuid=note_uuid)
            note_uuid = None
            leftover = await _group_node_count(memory, profile.memory_group)
    except Exception as error:
        _fail_provider(error, "Graphiti meeting ingestion")
    finally:
        if memory is None and (ingested or note_uuid is not None):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
        if memory is not None:
            try:
                async with asyncio.timeout(120):
                    if note_uuid is not None:
                        await memory.remove_episode(note_uuid)
                    if ingested:
                        await forget_meeting(
                            memory, group_id=profile.memory_group, meeting_id=meeting_id
                        )
            finally:
                await memory.close()

    episodes = [json.loads(line) for line in lines if '"memory_episode"' in line]
    print(
        "\ningestion episodes:",
        [
            (row["speaker_kind"], row["outcome"], row["facts"], round(row["duration_ms"]))
            for row in episodes
        ],
    )
    print("recalled:", recalled)
    print("corrected:", corrected)
    assert [row["speaker_kind"] for row in episodes] == ["twin", "participant"]
    assert not [line for line in lines if '"event":"fault"' in line]
    assert any(row["facts"] for row in episodes), f"Graphiti extracted no facts: {episodes}"
    assert recalled != RECALL_EMPTY_RESULT
    assert f"Google Meet {meeting_id}" in recalled
    assert "owner note" in corrected
    assert removed == len(episodes)
    assert leftover == 0
