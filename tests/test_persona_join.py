import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.nodes import EpisodicNode
from langchain.agents.middleware import ToolErrorMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import SecretStr

import fakeyou.cli.main as cli_main
import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.assembly as assembly
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import DEFAULTS, Settings
from fakeyou.conversation.middleware import (
    ChatTurnToolsMiddleware,
    SpeakerAttributionMiddleware,
)
from fakeyou.memory.persona import (
    OperatorIdentity,
    PersonaProfile,
    build_system_prompt,
    memory_group_for,
    require_persona_group,
)
from fakeyou.perception.context import PERCEPTION_GUIDANCE, PerceptionContextMiddleware
from fakeyou.persistence.record import meeting_record_path
from tests._cli_support import (
    MEET_LINK,
    PERSONA_DATA,
    AudioJoin,
    enable_audio_platform,
    fake_persona_memory,
    forbid_resources,
    join_persona_conversation,
    persona_join_arguments,
    printed_join_summary,
    write_persona,
    write_record,
)
from tests._conversation_fakes import ToolCallingFakeModel, recall_call
from tests._error_support import message_pattern
from tests._ingest_support import ingest_recorded_meeting
from tests._memory_fakes import make_graphiti
from tests._runtime_fakes import SessionRun


def recall_then_reply(reply: str) -> ToolCallingFakeModel:
    return ToolCallingFakeModel(
        responses=[
            recall_call({"query": "Orchid launch"}, id="call-1"),
            AIMessage(content=reply),
        ]
    )


async def test_join_persona_requires_converse(
    join: Callable[..., Any], settings: Settings, tmp_path: Path
) -> None:
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE)
    ) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                audio_enabled=False,
                conversation_enabled=False,
                persona_path=write_persona(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE


async def test_join_memory_requires_persona(
    join: Callable[..., Any], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_resources(monkeypatch)
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA)
    ) as caught:
        await join(MEET_LINK, **persona_join_arguments(settings, memory=True))
    assert caught.value.code is ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA


@pytest.mark.parametrize(
    "overrides", [{"owner_id": "other-owner"}, {"twin_id": "other-twin"}, {"unknown": "field"}]
)
async def test_join_rejects_invalid_persona_before_resources(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises((FakeYouError, ValueError), match="owner_id and twin_id|Extra inputs"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, **overrides),
            ),
        )
    assert not settings.paths.journal_dir.exists()


@pytest.mark.parametrize("overrides", [{"owner_id": "other-owner"}, {"twin_id": "other-twin"}])
async def test_join_reports_a_foreign_persona_identity_with_its_code(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(FakeYouError, match="owner_id and twin_id") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, **overrides),
            ),
        )
    assert caught.value.code is ErrorCode.PERSONA_IDENTITY_MISMATCH
    assert not settings.paths.journal_dir.exists()


async def test_join_memory_rejects_persona_without_recall_permission(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST),
    ) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, allowed_tools=[]),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST


async def test_join_rejects_unreadable_persona(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEMORY_PERSONA_UNREADABLE)
    ) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(settings, persona_path=tmp_path / "missing.json"),
        )
    assert caught.value.code is ErrorCode.MEMORY_PERSONA_UNREADABLE


@pytest.mark.parametrize("twin_name", [None, "Override"])
async def test_join_persona_display_name_yields_to_explicit_twin_name(
    audio_join: AudioJoin,
    session_run: SessionRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    twin_name: str | None,
) -> None:
    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = session_run
    await audio_join(
        conversation_enabled=True,
        twin_name=twin_name,
        persona_path=write_persona(tmp_path),
    )

    assert captured["config"].twin_name == (twin_name or PERSONA_DATA["display_name"])
    assert (captured["config"].owner_id, captured["config"].twin_id) == (
        "settings-owner",
        "settings-twin",
    )


async def test_join_persona_without_memory_uses_profile_prompt_without_tools(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, audio, _outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert created["persona"] == (
        f"{build_system_prompt(profile.model_copy(update={'allowed_tools': frozenset()}))}"
        f"\n\n{PERCEPTION_GUIDANCE}"
    )
    assert "recall_meeting_context" not in created["persona"]
    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        ChatTurnToolsMiddleware,
        PerceptionContextMiddleware,
        SpeakerAttributionMiddleware,
    ]
    assert audio.spoken == ["Hello."]


async def test_join_persona_memory_exposes_only_profile_scoped_recall(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    fact = "Orchid beta launch is planned for 14 October 2026"
    graphiti = make_graphiti(facts=(fact,), episodes={fact: ["meet-1"]})
    model = recall_then_reply("Orchid launches on 14 October.")
    episode = SimpleNamespace(
        uuid="meet-1",
        group_id=profile.memory_group,
        source_description="Google Meet meeting-9 transcript",
        valid_at=datetime(2026, 9, 30, 9, tzinfo=UTC),
        created_at=datetime(2026, 9, 30, 9, tzinfo=UTC),
    )

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        assert driver is graphiti.driver
        return [episode for uuid in uuids if uuid == "meet-1"]

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)

    created, audio, outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]
    assert [type(item) for item in created["middleware"]] == [
        ToolErrorMiddleware,
        ChatTurnToolsMiddleware,
        PerceptionContextMiddleware,
        SpeakerAttributionMiddleware,
    ]
    assert graphiti.searched == [
        {
            "query": "Orchid launch",
            "group_ids": [profile.memory_group],
            "num_results": DEFAULTS.memory.recall.search_limit,
            "search_filter": memory_module.CURRENT_FACTS_FILTER,
        },
        {
            "query": "Orchid launch",
            "group_ids": [profile.memory_group],
            "num_results": DEFAULTS.memory.recall.search_limit,
        },
    ]
    assert profile.memory_group == memory_group_for(profile.owner_id, profile.twin_id)
    assert (
        require_persona_group(
            graphiti.searched[0]["group_ids"][0],
            owner_id=profile.owner_id,
            twin_id=profile.twin_id,
        )
        == profile.memory_group
    )
    tool_results = [
        m
        for m in model.seen[-1]
        if isinstance(m, ToolMessage) and m.name == "recall_meeting_context"
    ]
    [content] = [m.content for m in tool_results]
    assert isinstance(content, str)
    payload = json.loads(content)
    assert payload["schema"] == "memory_evidence/v1"
    assert [item["text"] for item in payload["facts"]] == [fact]
    assert payload["facts"][0]["sources"][0]["episode_id"] == "meet-1"
    assert graphiti.added == []
    assert audio.spoken == ["Orchid launches on 14 October."]
    assert outcome["phase"] == "listening"
    assert graphiti.events == ["verify", "close"]


async def test_join_recall_failure_is_returned_as_unknown_without_failing_conversation(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti(search_error=RuntimeError("neo4j went away"))
    model = recall_then_reply("I don't know yet, I'll check with Rani.")

    _created, audio, outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    tool_results = [
        m
        for m in model.seen[-1]
        if isinstance(m, ToolMessage) and m.name == "recall_meeting_context"
    ]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert audio.spoken == ["I don't know yet, I'll check with Rani."]
    assert outcome["phase"] == "listening"
    outcome["conversation"].raise_if_failed()
    faults = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('{"event":"fault_recorded"')
    ]
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("memory", "unknown")]
    assert "neo4j went away" not in json.dumps(faults)


async def test_join_stop_during_recall_cancels_without_late_reply(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti(search_gate=asyncio.Event())
    model = recall_then_reply("A late answer.")

    _created, audio, outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settle=graphiti.search_started.wait,
        settings=settings,
        join=join,
    )

    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == []
    assert outcome["conversation"].lifecycle.phase == "closed"
    assert graphiti.events[-1] == "close"


async def test_join_persona_reply_keeps_two_sentence_cap(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ToolCallingFakeModel(
        responses=[AIMessage(content="Sentence one. Sentence two. Sentence three.")]
    )

    _created, audio, _outcome = await join_persona_conversation(
        tmp_path, monkeypatch, model=model, settings=settings, join=join
    )

    assert audio.spoken == ["Sentence one.", "Sentence two."]


def test_join_persona_flag_reaches_join(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_join(options: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        captured["options"] = options

    monkeypatch.setattr(cli_main, "run_session", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", MEET_LINK, "--audio", "--converse", "--persona", "p.json", "--memory"],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["options"].persona_path == Path("p.json")
    assert captured["options"].memory is True
    assert captured["options"].brief_path is None


async def test_join_ingest_only_persona_defers_memory_without_recall_tool(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    graphiti = make_graphiti()

    created, _audio, _outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        graphiti=graphiti,
        memory=True,
        persona_overrides={"ingest_transcripts": True, "allowed_tools": []},
        settings=settings,
        join=join,
    )

    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        ChatTurnToolsMiddleware,
        PerceptionContextMiddleware,
        SpeakerAttributionMiddleware,
    ]
    assert graphiti.added == []
    assert "recall" not in created["persona"].casefold()
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "pending"

    fake_persona_memory(monkeypatch, graphiti)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(
            write_persona(tmp_path, ingest_transcripts=True, allowed_tools=[]), None, None
        ),
        twin_name=None,
        settings=settings,
    )

    assert len(graphiti.added) == 1


async def test_join_hung_recall_times_out_as_unknown_and_keeps_listening(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti(search_gate=asyncio.Event())
    model = recall_then_reply("I don't know yet, I'll check with Rani.")
    settings.memory.recall.timeout_seconds = 0.01

    _created, audio, outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    tool_results = [
        m
        for m in model.seen[-1]
        if isinstance(m, ToolMessage) and m.name == "recall_meeting_context"
    ]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == ["I don't know yet, I'll check with Rani."]
    assert outcome["phase"] == "listening"
    faults = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('{"event":"fault_recorded"')
    ]
    assert [(row["source"], row["kind"]) for row in faults] == [("memory", "timeout")]


async def test_memory_ingest_command_ingests_the_record_into_the_persona_group(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)

    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(write_persona(tmp_path, vocabulary=["Orchid"]), None, None),
        twin_name=None,
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    [episode] = graphiti.added
    assert episode["group_id"] == profile.memory_group
    assert "Orchid" in episode["custom_extraction_instructions"]
    assert "'Rani Twin'" in episode["custom_extraction_instructions"]
    summary = json.loads(capsys.readouterr().out)
    assert (summary["meeting_id"], summary["ingested"], summary["completed"]) == (
        "meeting-9",
        1,
        True,
    )
    journal = (settings.paths.journal_dir / "meeting-9.jsonl").read_text()
    assert '"event":"memory_ingest_finished"' in journal


async def test_join_passes_persona_vocabulary_as_speech_keywords(
    audio_join: AudioJoin,
    session_run: SessionRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = session_run
    await audio_join(
        conversation_enabled=True,
        persona_path=write_persona(tmp_path, vocabulary=["Orchid", "Budi"]),
    )

    from fakeyou.memory.persona import number_keywords

    assert captured["open_audio"].keywords["policy"].speech.stt_keywords == (
        "Rani Twin",
        "Orchid",
        "Budi",
        *number_keywords("id"),
    )


async def test_join_without_persona_has_no_speech_keywords(
    audio_join: AudioJoin,
    session_run: SessionRun,
) -> None:
    captured = session_run
    await audio_join()

    assert captured["open_audio"].keywords["policy"].speech.stt_keywords is None


async def test_join_rejects_persona_language_mismatch_before_resources(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_audio_platform(monkeypatch)
    settings.stt.api_key = SecretStr("muse-key")
    forbid_resources(monkeypatch)
    persona = write_persona(tmp_path, language="en")
    with pytest.raises(FakeYouError, match="persona language") as caught:
        await join(MEET_LINK, audio_enabled=True, conversation_enabled=True, persona_path=persona)
    assert caught.value.code is ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH
    assert not settings.paths.journal_dir.exists()


@pytest.mark.usefixtures("session_run")
async def test_join_without_persona_writes_no_status(
    join: Callable[..., Any],
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fakeyou.persistence.ingestion_status import status_path

    await join(MEET_LINK)

    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-1"))
    assert not status_path(record_path).exists()
    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "not_requested"
    assert printed["record_error"] is None
    assert printed["resume_command"] is None
