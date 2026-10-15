import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult

from fakeyou.audio.playback import PlaybackEvent
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.persona import PersonaProfile, build_system_prompt
from fakeyou.persistence.brief import read_meeting_context
from fakeyou.persistence.record import load_meeting_record, meeting_record_path
from tests._audio_fakes import FakeReplyAudio
from tests._cli_support import (
    BRIEF_FACT,
    PERSONA_DATA,
    expected_context,
    join_persona_conversation,
    meeting_brief,
    meeting_context_file,
    write_brief,
)
from tests._conversation_fakes import ToolCallingFakeModel
from tests._persona_brief_support import journal_rows


async def test_join_briefs_stay_isolated_per_meeting_and_resume_in_one_checkpointer(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_fact = "The synthetic vendor budget stays fixed."
    second = {
        "meeting_id": "meeting-10",
        "approved_facts": [{"id": "vendor-budget", "text": second_fact}],
    }
    first_model = ToolCallingFakeModel(responses=[AIMessage(content="First.")])
    await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=first_model,
        brief_path=write_brief(tmp_path),
        transcripts=("Rani Twin, first meeting question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )
    second_model = ToolCallingFakeModel(responses=[AIMessage(content="Second.")])
    await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=second_model,
        meeting_id="meeting-10",
        brief_path=write_brief(tmp_path, "second-brief.json", **second),
        transcripts=("Rani Twin, second meeting question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    second_prompt = second_model.seen[0][0].content
    assert second_prompt == build_system_prompt(profile, brief=meeting_brief(**second))
    assert BRIEF_FACT not in second_prompt
    assert second_fact not in first_model.seen[0][0].content
    second_history = " ".join(str(message.content) for message in second_model.seen[0])
    assert "Rani Twin, first meeting question?" not in second_history
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(
        meeting_brief(), profile
    )
    assert read_meeting_context(meeting_context_file(settings, "meeting-10")) == (
        expected_context(meeting_brief(**second), profile)
    )

    retry_model = ToolCallingFakeModel(responses=[AIMessage(content="Retry.")])
    _created, audio, _outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=retry_model,
        brief_path=write_brief(tmp_path),
        transcripts=("Rani Twin, retry question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )

    assert audio.spoken == ["Retry."]
    retry_history = " ".join(str(message.content) for message in retry_model.seen[0])
    assert "Rani Twin, first meeting question?" in retry_history
    assert "Rani Twin, second meeting question?" not in retry_history
    assert retry_model.seen[0][0].content == build_system_prompt(profile, brief=meeting_brief())


async def test_join_brief_stop_before_pcm_cancels_the_model_turn(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    class HangingModel(ToolCallingFakeModel):
        @override
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            started.set()
            await asyncio.Event().wait()
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    _created, audio, outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=HangingModel(responses=[AIMessage(content="A late answer.")]),
        brief_path=write_brief(tmp_path),
        settle=started.wait,
        settings=settings,
        join=join,
    )

    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    rows = journal_rows(settings)
    assert [row["reason"] for row in rows if row["event"] == "agent_cancelled"] == ["close"]
    assert not [row for row in rows if row["event"] == "playback_finished"]
    entries = load_meeting_record(
        meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    )
    assert [entry.kind for entry in entries] == ["transcript"]
    assert read_meeting_context(meeting_context_file(settings)) is not None


async def test_join_brief_stop_during_playback_records_no_twin_speech(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BlockingAudio(FakeReplyAudio):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        @override
        async def speak(
            self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
        ) -> None:
            self.speaking = True
            event_callback = self.on_playback_event
            assert event_callback is not None
            event_callback(PlaybackEvent("playback_started", text))
            self.entered.set()
            await asyncio.Event().wait()

    blocking = BlockingAudio()
    _created, audio, outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="A long answer.")]),
        brief_path=write_brief(tmp_path),
        audio=blocking,
        settle=blocking.entered.wait,
        settings=settings,
        join=join,
    )

    assert audio is blocking
    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    playback = [row["event"] for row in journal_rows(settings) if row["source"] == "playback"]
    assert playback == ["playback_started"]
    entries = load_meeting_record(
        meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    )
    assert [entry.kind for entry in entries] == ["transcript"]
    assert read_meeting_context(meeting_context_file(settings)) is not None
