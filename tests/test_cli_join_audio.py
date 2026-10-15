import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

import fakeyou.orchestration.assembly as assembly
from fakeyou.audio.playback import PlaybackEvent
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.agent import DEFAULT_PERSONA
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LangGraphResponder,
    conversation_thread_id,
)
from fakeyou.conversation.middleware import HistoryTrimMiddleware, OrphanToolCallMiddleware
from fakeyou.conversation.session import SessionResult
from fakeyou.foundation.greetings import create_greeting_middleware
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.persistence.record import (
    TranscriptEntry,
    TwinSpeechEntry,
    load_meeting_record,
    meeting_record_path,
)
from tests._cli_support import AudioJoin
from tests._error_support import message_pattern
from tests._runtime_fakes import SessionRun


@pytest.mark.asyncio
async def test_join_audio_builds_conversation_with_meeting_thread(
    audio_join: AudioJoin,
    session_run: SessionRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, Any] = {}

    def fake_agent(
        model: Any,
        *,
        tools: tuple[Any, ...] = (),
        checkpointer: Any = None,
        **kwargs: Any,
    ) -> SimpleNamespace:
        del model
        created["tools"] = list(tools)
        created["checkpointer"] = checkpointer
        created.update(kwargs)
        return SimpleNamespace(name="agent")

    monkeypatch.setattr(assembly, "create_meeting_agent", fake_agent)
    captured = session_run
    await audio_join(
        conversation_enabled=True,
    )

    loop = captured["conversation"]
    assert isinstance(loop, ConversationLoop)
    assert loop.phase == "closed"
    assert created["tools"] == []
    assert created["checkpointer"] is not None
    assert created["persona"] == DEFAULT_PERSONA
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id")),
        HistoryTrimMiddleware,
        OrphanToolCallMiddleware,
    ]
    responder = loop.responder
    assert isinstance(responder, LangGraphResponder)
    assert responder.journal is not None


@pytest.mark.asyncio
async def test_join_audio_transcript_reaches_conversation(
    audio_join: AudioJoin,
    session_run: SessionRun,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_calls: list[Any] = []

    class FakeAgent:
        async def ainvoke(
            self, request: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            del request
            agent_calls.append(config)
            return {"messages": [AIMessage(content="Reply.")]}

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())
    captured = {}

    async def run(config: Any, **kwargs: Any) -> SessionResult:
        del config
        captured.update(kwargs)
        loop = kwargs["conversation"]
        loop.bind(SimpleNamespace(speak=lambda _text: asyncio.sleep(0)))
        frame = TranscriptUpdate(
            text="Settings Twin, hello.",
            final=True,
            timestamp="2026-09-28T00:00:00+00:00",
        )
        await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
        await loop.wait_idle()
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await audio_join(
        conversation_enabled=True,
    )

    assert [event.text for event in captured["conversation"].events] == ["Settings Twin, hello."]
    assert agent_calls[0]["configurable"]["thread_id"] == conversation_thread_id(
        "settings-owner", "settings-twin", "meeting-9"
    )
    rows = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{"')
    ]
    transitions = [row for row in rows if row["event"] == "conversation_transitioned"]
    assert len(transitions) == 3
    assert all(row["at"] for row in transitions)
    [final] = [row for row in rows if row["event"] == "transcript_finalized"]
    assert "text" not in final
    assert (final["participant_id"], final["meeting_id"]) == ("unknown", "meeting-9")
    assert final["speaker_evidence_id"] is None
    assert final["at"]
    entries = load_meeting_record(
        meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    )
    transcripts = cast(list[TranscriptEntry], entries)
    assert [(entry.kind, entry.text) for entry in transcripts] == [
        ("transcript", "Settings Twin, hello.")
    ]


async def test_audio_only_does_not_construct_responding_agent(
    audio_join: AudioJoin,
    session_run: SessionRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        assembly,
        "create_meeting_agent",
        lambda *_args, **_kwargs: pytest.fail("agent must be opt-in"),
    )
    captured = session_run
    await audio_join()
    assert captured["conversation"] is None


async def test_conversation_option_requires_audio(join: Callable[..., Any]) -> None:
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO)
    ) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            conversation_enabled=True,
        )
    assert caught.value.code is ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO


@pytest.mark.asyncio
async def test_join_wake_phrase_requires_converse(join: Callable[..., Any]) -> None:
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE)
    ) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            wake_phrase="hey fakeyou",
        )
    assert caught.value.code is ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE


@pytest.mark.asyncio
async def test_join_barge_in_requires_converse(join: Callable[..., Any]) -> None:
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE)
    ) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            barge_in=True,
        )
    assert caught.value.code is ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE


@pytest.mark.asyncio
async def test_join_wake_phrase_gates_agent_replies(
    audio_join: AudioJoin,
    session_run: SessionRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_calls = []

    class FakeAgent:
        async def ainvoke(
            self, request: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            del config
            agent_calls.append(request)
            return {"messages": [AIMessage(content="Reply.")]}

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    async def run(config: Any, **kwargs: Any) -> SessionResult:
        del config
        loop = kwargs["conversation"]
        loop.bind(SimpleNamespace(speak=lambda _text: asyncio.sleep(0)))
        for _ident, text in (("t1", "project status"), ("t2", "hey fakeyou hello")):
            frame = TranscriptUpdate(text=text, final=True, timestamp="2026-09-28T00:00:00+00:00")
            await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
        await loop.wait_idle()
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await audio_join(
        conversation_enabled=True,
        wake_phrase="hey fakeyou",
    )
    assert len(agent_calls) == 1


@pytest.mark.asyncio
async def test_join_barge_in_stops_speaking_session(
    audio_join: AudioJoin,
    session_run: SessionRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops = []

    class FakeAgent:
        async def ainvoke(
            self, request: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            del config
            del request
            return {"messages": [AIMessage(content="Reply.")]}

    async def stop_playback() -> None:
        stops.append(True)

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    async def run(config: Any, **kwargs: Any) -> SessionResult:
        del config
        loop = kwargs["conversation"]
        loop.bind(
            SimpleNamespace(
                speak=lambda _text: asyncio.sleep(0),
                is_speaking=True,
                stop_playback=stop_playback,
            )
        )
        frame = TranscriptUpdate(text="wait", final=True, timestamp="2026-09-28T00:00:00+00:00")
        await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
        await loop.wait_idle()
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await audio_join(
        conversation_enabled=True,
        barge_in=True,
    )
    assert stops == [True]


@pytest.mark.asyncio
async def test_join_playback_events_are_journaled_and_finished_speech_recorded(
    audio_join: AudioJoin,
    session_run: SessionRun,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run(config: Any, **kwargs: Any) -> SessionResult:
        del config
        handler = kwargs["open_audio"].keywords["callbacks"].on_playback_event
        handler(PlaybackEvent("playback_started", "hello", ""))
        handler(PlaybackEvent("playback_dropped", "cut off", "interrupted"))
        handler(PlaybackEvent("playback_finished", "hello?  ", ""))
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await audio_join()

    rows = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{"')
    ]
    playback = [
        (row["event"], row["ends_with_question"], row["detail"])
        for row in rows
        if row.get("source") == "playback"
    ]
    assert playback == [
        ("playback_started", False, None),
        ("playback_dropped", False, "interrupted"),
        ("playback_finished", True, None),
    ]
    assert all(
        row["meeting_id"] == "meeting-9" and row["at"]
        for row in rows
        if row.get("source") == "playback"
    )
    entries = load_meeting_record(
        meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    )
    speeches = cast(list[TwinSpeechEntry], entries)
    assert [(entry.kind, entry.text) for entry in speeches] == [("twin_speech", "hello?  ")]


async def test_join_routes_interim_transcripts_to_barge_in(
    audio_join: AudioJoin,
    session_run: SessionRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops: list[bool] = []

    async def stop_playback() -> None:
        stops.append(True)

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: object())

    async def run(config: Any, **kwargs: Any) -> SessionResult:
        del config
        loop = kwargs["conversation"]
        loop.bind(
            SimpleNamespace(
                speak=lambda _text: asyncio.sleep(0),
                is_speaking=True,
                stop_playback=stop_playback,
            )
        )
        interim = kwargs["open_audio"].keywords["callbacks"].on_interim
        await interim(TranscriptUpdate(text="wait", final=False))
        await interim(TranscriptUpdate(text="wait a moment", final=False))
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await audio_join(
        conversation_enabled=True,
        barge_in=True,
    )

    assert stops == [True]


async def test_join_rejects_policy_without_wake_phrase(
    join: Callable[..., Any], settings: Settings
) -> None:
    settings.conversation.participant_policy_enabled = True
    with pytest.raises(FakeYouError, match="wake-phrase") as caught:
        await join("https://meet.google.com/abc-defg-hij", meeting_id=None)
    assert caught.value.code is ErrorCode.MEETING_PARTICIPANT_POLICY_REQUIRES_ALIASES


@pytest.mark.asyncio
async def test_join_without_wake_phrase_answers_only_when_the_twin_is_named(
    audio_join: AudioJoin,
    session_run: SessionRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_calls = []

    class FakeAgent:
        async def ainvoke(
            self, request: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            del config
            agent_calls.append(request)
            return {"messages": [AIMessage(content="Reply.")]}

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: FakeAgent())

    async def run(config: Any, **kwargs: Any) -> SessionResult:
        del config
        loop = kwargs["conversation"]
        loop.bind(SimpleNamespace(speak=lambda _text: asyncio.sleep(0)))
        for _ident, text in (
            ("t1", "let's review the budget"),
            ("t2", "who owns the migration"),
            ("t3", "Settings Twin, what is blocking the release?"),
        ):
            frame = TranscriptUpdate(text=text, final=True, timestamp="2026-09-28T00:00:00+00:00")
            await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
        await loop.wait_idle()
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await audio_join(conversation_enabled=True)

    assert len(agent_calls) == 1
