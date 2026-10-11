import json
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

import fakeyou.cli.join as cli_join
import fakeyou.cli.memory as cli_memory
import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.assembly as assembly
import fakeyou.orchestration.providers as providers
from fakeyou.config.settings import (
    CompatibleReply,
    Settings,
)
from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import conversation_thread_id
from fakeyou.conversation.session import SessionResult
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url, require_url
from fakeyou.foundation.invariants import (
    AGENT_SINK_NAME,
    MEETING_MONITOR_SOURCE_NAME,
)
from fakeyou.memory.persona import PersonaProfile, persona_digest
from fakeyou.persistence.brief import (
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
)
from fakeyou.persistence.record import meeting_record_path
from tests.fakes import (
    FakeReplyAudio,
)


def _enable_audio_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PULSE_SOURCE", MEETING_MONITOR_SOURCE_NAME)
    monkeypatch.setenv("PULSE_SINK", AGENT_SINK_NAME)
    monkeypatch.setattr(cli_join, "find_spec", lambda name: SimpleNamespace())


async def _join_with_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    join: Callable[..., Coroutine[Any, Any, Any]],
    settings: Settings,
    session_run: Any,
    **overrides: Any,
) -> Any:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(providers, "init_chat_model", lambda *args, **kwargs: SimpleNamespace())

    @asynccontextmanager
    async def fake_saver(value: Any, url: Any = None) -> AsyncIterator[InMemorySaver]:
        yield InMemorySaver()

    monkeypatch.setattr(assembly, "open_checkpointer", fake_saver)
    captured = session_run
    arguments = {
        "twin_name": None,
        "owner_id": None,
        "twin_id": None,
        "meeting_id": "meeting-9",
        "lobby_timeout": None,
        "duration": None,
        "profile_dir": None,
        "settings": settings,
        "audio_enabled": True,
    }
    arguments.update(overrides)
    await join("https://meet.google.com/abc-defg-hij", **arguments)
    return captured


PERSONA_DATA = {
    "owner_id": "settings-owner",
    "twin_id": "settings-twin",
    "display_name": "Rani Twin",
    "role": "Product lead for the synthetic Orchid project",
    "verified_facts": ["The Orchid beta launch is planned for 14 October 2026"],
    "allowed_tools": ["recall_meeting_context"],
}

MEET_LINK = "https://meet.google.com/abc-defg-hij"

BASE_HOSTS = ("https://api.openai.com/v1", "wss://api.meta.ai/v1/asr/realtime")


def write_persona(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "persona.json"
    path.write_text(json.dumps({**PERSONA_DATA, **overrides}), encoding="utf-8")
    return path


def persona_join_arguments(tmp_path: Path, settings: Settings, **overrides: Any) -> dict[str, Any]:
    arguments = {
        "twin_name": None,
        "owner_id": None,
        "twin_id": None,
        "meeting_id": "meeting-9",
        "lobby_timeout": None,
        "duration": None,
        "profile_dir": None,
        "settings": settings,
        "audio_enabled": True,
        "conversation_enabled": True,
    }
    arguments.update(overrides)
    return arguments


def forbid_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> NoReturn:
        pytest.fail("resources must not open before persona validation")

    monkeypatch.setattr(providers, "init_chat_model", fail)
    monkeypatch.setattr(cli_join, "run_meeting_session", fail)
    monkeypatch.setattr(memory_module, "create_graphiti_memory", fail)


BRIEF_FACT = "The synthetic Orchid demo is booked for Friday."

BRIEF_DATA = {
    "schema_version": "meeting_brief/v1",
    "owner_id": "settings-owner",
    "twin_id": "settings-twin",
    "meeting_id": "meeting-9",
    "agenda": ["Synthetic Orchid kickoff agenda item"],
    "objectives": [{"id": "blockers", "text": "Find the synthetic kickoff blocker."}],
    "approved_facts": [{"id": "demo-date", "text": BRIEF_FACT}],
    "questions": [{"id": "vendor-needs", "text": "Which synthetic vendor input is missing?"}],
    "permissions": ["explain_facts", "ask_brief_questions"],
    "knowledge_scope": "brief_only",
}

BRIEF_TEXTS = (
    "Synthetic Orchid kickoff agenda item",
    "Find the synthetic kickoff blocker.",
    BRIEF_FACT,
    "Which synthetic vendor input is missing?",
)

PRIVATE_MARKER = "PRIVATE-BRIEF-MARKER-5c1e"


def write_brief(tmp_path: Path, name: str = "brief.json", **overrides: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({**BRIEF_DATA, **overrides}), encoding="utf-8")
    return path


def meeting_brief(**overrides: object) -> MeetingBrief:
    return MeetingBrief.model_validate_json(json.dumps({**BRIEF_DATA, **overrides}))


def expected_context(brief: MeetingBrief, profile: PersonaProfile) -> MeetingContext:
    return MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256=persona_digest(profile),
    )


def meeting_context_file(settings: Settings, meeting_id: str = "meeting-9") -> Path:
    return context_path(meeting_record_path(settings.paths.record_dir, meeting_id))


async def seed_conversation_thread(settings: Settings, meeting_id: str) -> str:
    thread_id = conversation_thread_id("settings-owner", "settings-twin", meeting_id)
    async with open_checkpointer(require_url(configured_url(settings))) as saver:
        agent = create_meeting_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="ok")]), checkpointer=saver
        )
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "hello"}]},
            {"configurable": {"thread_id": thread_id}},
        )
    return thread_id


async def _join_persona_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    join: Callable[..., Coroutine[Any, Any, Any]],
    settings: Settings,
    model: FakeMessagesListChatModel,
    graphiti: MagicMock | None = None,
    transcripts: tuple[str, ...] = ("When does Orchid launch?",),
    settle: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    persona_overrides: dict[str, Any] | None = None,
    audio: FakeReplyAudio | None = None,
    on_start: Callable[[], Any] | None = None,
    native_checkpointer: bool = False,
    **overrides: Any,
) -> tuple[dict[str, Any], FakeReplyAudio, dict[str, Any]]:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: model)

    @asynccontextmanager
    async def memory_saver(_value: Any, url: Any = None) -> AsyncIterator[InMemorySaver]:
        yield InMemorySaver()

    if not native_checkpointer:
        monkeypatch.setattr(assembly, "open_checkpointer", memory_saver)
    if graphiti is not None:
        monkeypatch.setattr(
            memory_module, "create_graphiti_memory", lambda _config, **_kwargs: graphiti
        )
    else:
        monkeypatch.setattr(
            memory_module,
            "create_graphiti_memory",
            lambda _config, **_kwargs: pytest.fail("graphiti must stay closed without --memory"),
        )
    created: dict[str, Any] = {}

    def recording_agent(agent_model: Any, **kwargs: Any) -> Any:
        created.update(kwargs)
        return create_meeting_agent(agent_model, **kwargs)

    monkeypatch.setattr(assembly, "create_meeting_agent", recording_agent)
    audio = audio if audio is not None else FakeReplyAudio()
    outcome: dict[str, Any] = {}

    async def run(config: Any, wiring: Any = None, **kwargs: Any) -> SessionResult:
        if wiring is not None:
            merged = {field.name: getattr(wiring, field.name) for field in fields(wiring)}
            merged.update(kwargs)
            kwargs = merged
        if on_start is not None:
            on_start()
        loop = kwargs["conversation"]
        outcome["config"] = config
        outcome["conversation"] = loop
        audio.on_playback_event = kwargs["open_audio"].keywords["callbacks"].on_playback_event
        loop.bind(audio)
        for index, text in enumerate(transcripts):
            frame = SimpleNamespace(
                id=f"t{index}",
                text=text,
                finalized=True,
                user_id="",
                timestamp=f"2026-09-30T00:00:0{index}+00:00",
            )
            await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
        if settle is None:
            await loop.wait_idle()
        else:
            await settle()
        outcome["phase"] = loop.phase
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    monkeypatch.setattr(cli_join, "run_meeting_session", run)
    arguments = persona_join_arguments(
        tmp_path,
        settings,
        persona_path=write_persona(tmp_path, **(persona_overrides or {})),
        **overrides,
    )
    await join(MEET_LINK, **arguments)
    return created, audio, outcome


def fake_persona_memory(monkeypatch: pytest.MonkeyPatch, memory: object) -> list[str]:
    opened: list[str] = []

    @asynccontextmanager
    async def open_memory(_settings: Any) -> AsyncIterator[Any]:
        opened.append("open")
        yield memory
        opened.append("close")

    for module in (cli_join, cli_memory):
        monkeypatch.setattr(module, "open_persona_memory", open_memory)
    return opened


async def _thread_exists(settings: Settings, thread_id: str) -> bool:
    async with open_checkpointer(require_url(configured_url(settings))) as saver:
        return await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None


def write_record(settings: Settings, meeting_id: str) -> None:
    from fakeyou.foundation.transcript import TranscriptEvent
    from fakeyou.persistence.record import MeetingRecord

    record = MeetingRecord(meeting_record_path(settings.paths.record_dir, meeting_id))
    record.add_transcript(
        TranscriptEvent(
            id="t1",
            timestamp="2026-09-30T10:00:00+00:00",
            source="meeting_audio",
            text="The Orchid vendor kickoff is on Tuesday.",
            finalized=True,
            participant_id="unknown",
            speaker_evidence_id=None,
        )
    )
    record.add_twin_speech("Got it, Tuesday.")
    record.close()


def with_muse_fallback(settings: Settings) -> Settings:
    settings.reply.max_retries = 0
    settings.reply_fallback = CompatibleReply(
        model="muse-spark-1.2",
        base_url="https://api.meta.ai/v1",
        api_key=SecretStr("meta-key"),
        reasoning_effort="minimal",
        max_retries=1,
    )
    return settings


def printed_join_summary(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    summary: dict[str, Any] = json.loads(out[out.rindex('{\n  "meeting_id"') :])
    return summary


def write_ingest_windows(settings: Settings, meeting_id: str, count: int) -> None:
    from datetime import timedelta

    from fakeyou.foundation.transcript import TranscriptEvent
    from fakeyou.persistence.record import MeetingRecord

    start = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    moments = iter(start + timedelta(seconds=61 * index) for index in range(count))
    record = MeetingRecord(
        meeting_record_path(settings.paths.record_dir, meeting_id), now=lambda: next(moments)
    )
    for index in range(count):
        moment = start + timedelta(seconds=61 * index)
        record.add_transcript(
            TranscriptEvent(
                id=f"window-{index}",
                timestamp=moment.isoformat(),
                source="meeting_audio",
                text=(
                    f"Decision {index} is final for the Thursday launch, with the rollout "
                    "checklist and the client demo schedule confirmed for all regions."
                ),
                finalized=True,
                participant_id="unknown",
                speaker_evidence_id=None,
            )
        )
    record.close()
