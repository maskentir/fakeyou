import asyncio
from pathlib import Path
from typing import Any, cast

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptEvent, TranscriptUpdate
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.transcript import (
    DrainOptions,
    TranscriptDrain,
    TranscriptSinks,
    build_transcript_callback,
)
from fakeyou.persistence.record import MeetingRecord, TranscriptEntry, meeting_record_path

QUESTION = "Rani, when is the beta?"


class Conversation:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.finals: list[TranscriptEvent] = []
        self.interims: list[str] = []

    async def handle_interim(self, text: str) -> None:
        self.interims.append(text)

    async def ready_to_answer(self, text: str) -> bool:
        del text
        return self.ready

    async def receive_final(self, event: TranscriptEvent) -> None:
        self.finals.append(event)

    async def withdraw(self, *, reason: str) -> None:
        del reason


def drain(
    tmp_path: Path, conversation: Conversation, *, hold: float | None = 0.0
) -> tuple[TranscriptDrain, MeetingRecord, Journal]:
    journal = Journal("meeting-9", emit=lambda _line: None, echo=False)
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, cast(Any, conversation)),
        binder=SlotBinder(),
        voice_activity=None,
        options=DrainOptions(early_hold_seconds=hold),
    )
    return callback, record, journal


async def speak_then_pause(callback: TranscriptDrain, text: str) -> None:
    await callback.on_interim(TranscriptUpdate(text=text, final=False))
    await callback.on_vad(active=False)
    for _ in range(5):
        await asyncio.sleep(0)


def recorded(record: MeetingRecord) -> list[str]:
    return [entry.text for entry in record.entries() if isinstance(entry, TranscriptEntry)]


async def test_a_finished_question_is_answered_before_the_final(tmp_path: Path) -> None:
    conversation = Conversation()
    callback, record, _journal = drain(tmp_path, conversation)

    await speak_then_pause(callback, QUESTION)
    await callback(
        TranscriptUpdate(text=QUESTION, final=True, timestamp="2026-10-25T01:00:00+00:00")
    )

    assert [event.text for event in conversation.finals] == [QUESTION]
    assert recorded(record) == [QUESTION]
    await callback.close()
    record.close()


async def test_without_a_ready_judgement_the_final_starts_the_turn(tmp_path: Path) -> None:
    conversation = Conversation(ready=False)
    callback, record, _journal = drain(tmp_path, conversation)

    await speak_then_pause(callback, "Rani, so about the")
    await callback(
        TranscriptUpdate(
            text="Rani, so about the beta?", final=True, timestamp="2026-10-25T01:00:00+00:00"
        )
    )

    assert [event.text for event in conversation.finals] == ["Rani, so about the beta?"]
    await callback.close()
    record.close()


async def test_early_turns_stay_off_without_a_hold(tmp_path: Path) -> None:
    conversation = Conversation()
    callback, record, _journal = drain(tmp_path, conversation, hold=None)

    await speak_then_pause(callback, QUESTION)

    assert conversation.finals == []
    await callback.close()
    record.close()
