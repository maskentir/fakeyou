import asyncio
import json
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from graphiti_core.nodes import EpisodeType, EpisodicNode
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import SecretStr

from fakeyou import owner_notes
from fakeyou.agent import create_meeting_agent
from fakeyou.catalog.errors import FakeYouError
from fakeyou.conversation import TranscriptEvent
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import ingest_meeting
from fakeyou.journal import Journal
from fakeyou.memory import (
    create_graphiti_memory,
    create_memory_tools,
    forget_episode,
    forget_meeting,
)
from fakeyou.owner_notes import read_note_states, remember_owner_note
from fakeyou.persona import PersonaProfile
from fakeyou.providers import create_chat_model, create_fallback_chat_model, memory_config
from fakeyou.record import MeetingRecord, load_meeting_record, meeting_record_path
from fakeyou.settings import CompatibleReply, MemorySettings, Settings, require_secret
from fakeyou.voice import create_speech_synthesizer

pytestmark = pytest.mark.asyncio(loop_scope="module")

LIVE_INGESTION_MEETING = (
    Path(__file__).resolve().parent / "fixtures" / "live-ingestion-meeting.json"
)


def _secret_or_skip(settings_value, name: str) -> str:
    try:
        return require_secret(settings_value, name)
    except FakeYouError as error:
        pytest.skip(str(error))


def _fail_provider(error: Exception, provider: str) -> None:
    status = getattr(error, "status_code", None)
    detail = f" ({status})" if isinstance(status, int) else ""
    pytest.fail(f"{provider} failed: {type(error).__name__}{detail}", pytrace=False)


@pytest.mark.live
async def test_configured_agent_returns_a_response_and_executes_tool() -> None:
    settings = Settings()
    try:
        model = create_chat_model(settings)
    except FakeYouError as error:
        pytest.skip(str(error))

    @tool(description="Return the synthetic agenda marker for a meeting test.")
    def read_agenda_marker() -> str:
        return "agenda-marker-731"

    try:
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
        _fail_provider(error, f"{settings.reply.model} agent")

    messages = result["messages"]
    assert any(
        isinstance(message, ToolMessage) and message.content == "agenda-marker-731"
        for message in messages
    )
    assert isinstance(messages[-1], AIMessage)
    assert "agenda-marker-731" in str(messages[-1].content)


@pytest.mark.live
async def test_configured_tts_streams_pcm_audio() -> None:
    settings = Settings()
    try:
        require_secret(settings.tts.api_key, "FAKEYOU_TTS__API_KEY")
    except FakeYouError as error:
        pytest.skip(str(error))
    synthesizer = create_speech_synthesizer(
        settings.tts,
        language=settings.speech.language,
    )
    try:
        try:
            async with asyncio.timeout(60):
                streamed = b"".join(
                    [chunk async for chunk in synthesizer.stream("Hello from the test meeting.")]
                )
        except Exception as error:
            _fail_provider(error, f"{settings.tts.provider} TTS")
    finally:
        await synthesizer.aclose()

    assert len(streamed) > 0
    assert len(streamed) % 2 == 0


def _neo4j_config_or_skip() -> MemorySettings:
    settings = Settings()
    parsed = urlparse(settings.memory.store.uri)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 7687), 2):
            pass
    except OSError:
        pytest.skip("Neo4j is not listening at the configured URI")
    try:
        return memory_config(settings)
    except FakeYouError as error:
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


def _episode_sources(payload: dict, episode_id: str) -> list[dict]:
    return [
        source
        for fact in payload["facts"]
        for source in fact["sources"]
        if source["episode_id"] == episode_id
    ]


@pytest.mark.live
async def test_recorded_meeting_ingest_resume_recall_correction_and_forget(tmp_path) -> None:
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
        vocabulary=(project, "Budi"),
    )
    record_path = meeting_record_path(tmp_path, meeting_id)
    meeting = json.loads(LIVE_INGESTION_MEETING.read_text(encoding="utf-8"))
    record = MeetingRecord(record_path)
    record.add_twin_speech(meeting["twin_speech"].format(project=project))
    for index, template in enumerate(meeting["participant_transcripts"]):
        record.add_transcript(
            TranscriptEvent(
                id=f"{run}-{index}",
                timestamp=datetime.now(UTC).isoformat(),
                source="meeting_audio",
                text=template.format(project=project),
                finalized=True,
                participant_id="unknown",
                speaker_evidence_id=None,
            )
        )
    record.close()
    lines: list[str] = []
    memory = None
    ingested = False
    try:
        async with asyncio.timeout(420):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            options = {
                "group_id": profile.memory_group,
                "meeting_id": meeting_id,
                "twin_name": profile.display_name,
                "vocabulary": profile.vocabulary,
                "journal": Journal(meeting_id, emit=lines.append),
            }
            options["faults"] = FaultReporter(journal=options["journal"])
            first = await ingest_meeting(memory, load_meeting_record(record_path), **options)
            ingested = True
            again = await ingest_meeting(memory, load_meeting_record(record_path), **options)
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            recalled = await _recall(memory, profile.memory_group, f"{project} customer demo day")
            note = await remember_owner_note(
                memory,
                group_id=profile.memory_group,
                text=meeting["owner_note"].format(project=project),
            )
            corrected = await _recall(memory, profile.memory_group, f"{project} customer demo day")
            removed = await forget_meeting(
                memory, group_id=profile.memory_group, meeting_id=meeting_id
            )
            await forget_episode(
                memory, group_id=profile.memory_group, episode_uuid=note.episode_uuid
            )
            leftover = await _group_node_count(memory, profile.memory_group)
    except Exception as error:
        _fail_provider(error, "Graphiti recorded meeting ingestion")
    finally:
        if memory is None and ingested:
            memory = await asyncio.to_thread(create_graphiti_memory, config)
        if memory is not None:
            try:
                async with asyncio.timeout(120):
                    await forget_meeting(
                        memory, group_id=profile.memory_group, meeting_id=meeting_id
                    )
            finally:
                await memory.close()

    episodes = [json.loads(line) for line in lines if '"memory_episode_' in line]
    summary = [(row["utterances"], row["facts"], round(row["duration_ms"])) for row in episodes]
    print(f"\nepisodes (utterances, facts, ms): {summary}\nrecalled:\n{recalled}")
    print(f"corrected:\n{corrected}")
    assert (first.windows, first.ingested, first.completed) == (1, 1, True)
    assert (again.resumed_from, again.ingested, again.completed) == (1, 0, True)
    assert not [line for line in lines if '"event":"fault_recorded"' in line]
    assert first.facts > 0, f"Graphiti extracted no facts: {episodes}"
    assert f"Google Meet {meeting_id} transcript" in recalled
    assert "owner note" in corrected
    assert removed == 1
    assert leftover == 0


@pytest.mark.live
async def test_model_fallback_answers_and_calls_tools_when_the_primary_is_down() -> None:
    settings = Settings()
    fallback_key = _secret_or_skip(settings.reply.api_key, "FAKEYOU_REPLY__API_KEY")
    broken = settings.model_copy(
        update={
            "reply": CompatibleReply(
                model="gpt-4.1-mini",
                base_url="http://127.0.0.1:9/v1",
                api_key=SecretStr("unused"),
                max_retries=0,
            ),
            "reply_fallback": settings.reply.model_copy(
                update={"api_key": SecretStr(fallback_key), "max_retries": 1}
            ),
        }
    )

    @tool(description="Return the synthetic agenda marker for a meeting test.")
    def read_agenda_marker() -> str:
        return "agenda-marker-904"

    agent = create_meeting_agent(
        create_chat_model(broken),
        tools=[read_agenda_marker],
        middleware=[ModelFallbackMiddleware(create_fallback_chat_model(broken))],
    )
    started = time.monotonic()
    try:
        async with asyncio.timeout(60):
            result = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Call read_agenda_marker and repeat its exact value.",
                        }
                    ]
                },
                config={"recursion_limit": 8},
            )
    except Exception as error:
        _fail_provider(error, "model fallback")
    elapsed = time.monotonic() - started

    print(f"\nfallback answered in {elapsed:.2f}s: {result['messages'][-1].text}")
    assert any(
        isinstance(message, ToolMessage) and message.content == "agenda-marker-904"
        for message in result["messages"]
    )
    assert "agenda-marker-904" in result["messages"][-1].text
    assert elapsed < 20


@pytest.mark.live
async def test_owner_note_correction_roundtrip_survives_reopen() -> None:
    config = _neo4j_config_or_skip()
    run = uuid4().hex[:10]
    group_id = f"test_owner_notes_live_{run}"
    target_id = uuid4()
    fix_id = uuid4()
    target_at = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    fix_at = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    memory = None
    try:
        async with asyncio.timeout(420):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            await owner_notes.ensure_note_indexes(memory)
            target = await owner_notes.remember_owner_note(
                memory,
                group_id=group_id,
                text=f"Rani announced the Orchid{run} customer demo moves to Thursday morning.",
                source_at=target_at,
                note_id=target_id,
            )
            assert target.facts
            fix = await owner_notes.remember_owner_note(
                memory,
                group_id=group_id,
                text=f"Rani announced that the Orchid{run} customer demo moves to Friday morning.",
                source_at=fix_at,
                note_id=fix_id,
                supersedes=target_id,
            )
            assert fix.supersedes == str(target_id)
            again = await owner_notes.remember_owner_note(
                memory,
                group_id=group_id,
                text=f"Rani announced that the Orchid{run} customer demo moves to Friday morning.",
                source_at=fix_at,
                note_id=fix_id,
                supersedes=target_id,
            )
            assert again == fix
            episode = await EpisodicNode.get_by_uuid(memory.driver, str(fix_id))
            await episode.save(memory.driver)
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            states = await owner_notes.read_note_states(
                memory, group_id=group_id, episode_uuids=[str(target_id), str(fix_id)]
            )
            assert states == {str(target_id): "superseded", str(fix_id): "active"}
            reopened = await owner_notes.remember_owner_note(
                memory,
                group_id=group_id,
                text=f"Rani announced that the Orchid{run} customer demo moves to Friday morning.",
                source_at=fix_at,
                note_id=fix_id,
                supersedes=target_id,
            )
            assert reopened == fix
    except Exception as error:
        _fail_provider(error, "Graphiti owner note roundtrip")
    finally:
        if memory is None:
            try:
                memory = await asyncio.to_thread(create_graphiti_memory, config)
            except Exception:
                memory = None
        if memory is not None:
            try:
                async with asyncio.timeout(120):
                    await memory.driver.execute_query(
                        "MATCH (n) WHERE n.group_id = $group_id DETACH DELETE n",
                        group_id=group_id,
                    )
            finally:
                await memory.close()


@pytest.mark.live
async def test_trust_roundtrip_correction_survives_client_reopen(tmp_path) -> None:
    config = _neo4j_config_or_skip()
    run = uuid4().hex[:10]
    group_id = f"trust_roundtrip_{run}"
    comparison_id = f"trust_roundtrip_cmp_{run}"
    project = f"Anggrek{run}"
    meeting_id = f"trust-roundtrip-{run}"
    old_source = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
    new_source = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
    note_id = uuid4()
    correction_id = uuid4()
    comparison_note_id = uuid4()
    owned = [str(comparison_note_id), str(note_id), str(correction_id)]
    touched = False
    memory = None
    try:
        async with asyncio.timeout(600):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            touched = True
            comparison_note = await remember_owner_note(
                memory,
                group_id=comparison_id,
                text="Rani announced the comparison team serves jasmine tea on Friday morning.",
                note_id=comparison_note_id,
            )
            assert comparison_note.facts
            comparison_snapshot = await _group_node_count(memory, comparison_id)
            first = await remember_owner_note(
                memory,
                group_id=group_id,
                text=f"Rani announced that the {project} customer demo moves to Friday morning.",
                source_at=old_source,
                note_id=note_id,
            )
            assert first.source_at == old_source
            assert first.recorded_at >= old_source
            assert first.facts
            assert first.episode_uuid == str(note_id)
            record_path = meeting_record_path(tmp_path, meeting_id)
            record = MeetingRecord(record_path)
            record.add_twin_speech(f"Opening synthetic check for {project}.")
            record.add_transcript(
                TranscriptEvent(
                    id=f"{run}-0",
                    timestamp=datetime.now(UTC).isoformat(),
                    source="meeting_audio",
                    text=f"Customer demo of project {project} is planned this week.",
                    finalized=True,
                    participant_id="unknown",
                    speaker_evidence_id=None,
                )
            )
            record.close()
            lines: list[str] = []
            journal = Journal(meeting_id, emit=lines.append)
            try:
                ingested = await ingest_meeting(
                    memory,
                    load_meeting_record(record_path),
                    group_id=group_id,
                    meeting_id=meeting_id,
                    twin_name="Trust Twin",
                    vocabulary=(project,),
                    journal=journal,
                    faults=FaultReporter(journal=journal),
                )
            finally:
                journal.close()
            assert (ingested.windows, ingested.ingested, ingested.completed) == (1, 1, True)
            before = json.loads(await _recall(memory, group_id, f"{project} customer demo day"))
            assert before["schema"] == "memory_evidence/v1"
            assert _episode_sources(before, first.episode_uuid)
            assert {
                source["source_at"] for source in _episode_sources(before, first.episode_uuid)
            } == {old_source.isoformat()}
            corrected = await remember_owner_note(
                memory,
                group_id=group_id,
                text=f"Rani announced that the {project} customer demo moves to Monday morning.",
                source_at=new_source,
                note_id=correction_id,
                supersedes=note_id,
            )
            assert corrected.supersedes == str(note_id)
            assert corrected.source_at == new_source
            assert corrected.episode_uuid == str(correction_id)
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            states = await read_note_states(
                memory,
                group_id=group_id,
                episode_uuids=[first.episode_uuid, corrected.episode_uuid],
            )
            assert states[first.episode_uuid] == "superseded"
            assert states[corrected.episode_uuid] == "active"
            after = json.loads(await _recall(memory, group_id, f"{project} customer demo day"))
            assert _episode_sources(after, first.episode_uuid) == []
            assert _episode_sources(after, corrected.episode_uuid)
            retried = await remember_owner_note(
                memory,
                group_id=group_id,
                text=f"Rani announced that the {project} customer demo moves to Monday morning.",
                note_id=correction_id,
                supersedes=note_id,
            )
            assert retried == corrected
            controls = await memory.driver.execute_query(
                "MATCH (n:FakeyouOwnerNote {group_id: $group_id}) RETURN count(n) AS count",
                group_id=group_id,
            )
            assert controls.records[0]["count"] == 2
            final = json.loads(await _recall(memory, group_id, f"{project} customer demo day"))
            assert _episode_sources(final, first.episode_uuid) == []
            assert _episode_sources(final, corrected.episode_uuid)
            assert await _group_node_count(memory, comparison_id) == comparison_snapshot
            assert project not in await _recall(memory, comparison_id, "jasmine tea")
    except Exception as error:
        _fail_provider(error, "Graphiti trust roundtrip")
    finally:
        if memory is None and touched:
            memory = await asyncio.to_thread(create_graphiti_memory, config)
        if memory is not None and touched:
            try:
                async with asyncio.timeout(180):
                    await forget_meeting(memory, group_id=group_id, meeting_id=meeting_id)
                    for episode_uuid in owned:
                        try:
                            await memory.remove_episode(episode_uuid)
                        except Exception:
                            pass
                    for target in (group_id, comparison_id):
                        await memory.driver.execute_query(
                            "MATCH (n:FakeyouOwnerNote {group_id: $group_id}) DETACH DELETE n",
                            group_id=target,
                        )
                        await memory.driver.execute_query(
                            "MATCH (n {group_id: $group_id}) DETACH DELETE n",
                            group_id=target,
                        )
            finally:
                await memory.close()
