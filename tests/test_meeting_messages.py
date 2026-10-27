from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from fakeyou.conversation.meeting_messages import NoHistory, history_messages, request_messages
from fakeyou.foundation.triggers import current_trigger, is_history
from fakeyou.persistence.record import (
    ChatEntry,
    MeetingRecord,
    RecordEntry,
    SignalEntry,
    TranscriptEntry,
    TwinSpeechEntry,
)
from tests._conversation_support import event

AT = datetime(2026, 10, 24, tzinfo=UTC)
RECORD_ORDER = count(1)


def said(entry_id: str, text: str) -> TranscriptEntry:
    return TranscriptEntry(
        at=AT,
        seq=next(RECORD_ORDER),
        id=entry_id,
        spoken_at=AT.isoformat(),
        source="meeting_audio",
        text=text,
        participant_id="unknown",
        speaker_evidence_id=None,
    )


def replied(text: str, *, interrupted: bool = False) -> TwinSpeechEntry:
    return TwinSpeechEntry(at=AT, seq=next(RECORD_ORDER), text=text, interrupted=interrupted)


def shape(messages: list[BaseMessage]) -> list[tuple[str, str]]:
    return [(type(message).__name__, message.text) for message in messages]


def test_history_merges_twin_sentences_and_marks_where_it_was_cut_off() -> None:
    entries: list[RecordEntry] = [
        said("a", "Can you hear me?"),
        replied("Yes."),
        replied("I can hear you", interrupted=True),
        said("b", "Good."),
    ]

    assert shape(history_messages(entries, exclude=(), max_tokens=1000)) == [
        ("HumanMessage", "Can you hear me?"),
        ("AIMessage", "Yes. I can hear you —"),
        ("HumanMessage", "Good."),
    ]


def test_history_leaves_out_the_turn_being_answered_and_never_counts_as_its_trigger() -> None:
    entries: list[RecordEntry] = [said("a", "Earlier."), said("b", "Now?")]

    messages = history_messages([*entries, said("c", "Then.")], exclude={"b"}, max_tokens=1000)
    asked = [*messages, *request_messages([event("b", "Now?")])]

    assert shape(messages) == [("HumanMessage", "Earlier."), ("HumanMessage", "Then.")]
    assert all(is_history(message) for message in messages)
    assert current_trigger(asked) == (3, asked[2:])


def test_history_keeps_the_newest_lines_and_opens_on_a_person() -> None:
    entries: list[RecordEntry] = [
        said("a", "old " * 200),
        replied("old answer " * 50),
        said("b", "recent question"),
        replied("recent answer"),
    ]

    messages = history_messages(entries, exclude=(), max_tokens=20)

    assert shape(messages) == [("HumanMessage", "recent question"), ("AIMessage", "recent answer")]
    assert isinstance(messages[0], HumanMessage)


def test_history_ignores_entries_that_are_not_spoken() -> None:
    signal = SignalEntry(
        at=AT,
        seq=next(RECORD_ORDER),
        signal="joined",
        participant_id="p1",
        participant="Ayu",
        observed_at=AT,
    )

    assert history_messages([signal, replied("Hi.")], exclude=(), max_tokens=1000) == []
    assert NoHistory().entries() == ()


def test_a_reopened_record_remembers_what_was_said(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    first = MeetingRecord(path)
    first.add_transcript(event("t1", "Are we on track?"))
    first.add_twin_speech("Yes, we are.")
    first.close()

    reopened = MeetingRecord(path)
    reopened.add_transcript(event("t1", "Are we on track?"))
    reopened.add_twin_speech("Still on track.", interrupted=True)
    reopened.close()

    assert shape(history_messages(reopened.entries(), exclude=(), max_tokens=1000)) == [
        ("HumanMessage", "Are we on track?"),
        ("AIMessage", "Yes, we are. Still on track. —"),
    ]
    assert isinstance(
        history_messages(reopened.entries(), exclude=(), max_tokens=1000)[1], AIMessage
    )


def test_history_shows_the_twins_own_chat_as_its_own_words() -> None:
    own = ChatEntry(
        at=AT,
        seq=next(RECORD_ORDER),
        ref="chat#9",
        message_id="twin-action-1",
        sender_id="twin",
        sender="Harun",
        text="The deck lands Friday.",
        sent_at=AT,
        own=True,
    )
    messages = history_messages([said("a", "When does it land?"), own], exclude=(), max_tokens=1000)

    assert shape(messages) == [
        ("HumanMessage", "When does it land?"),
        ("AIMessage", "[chat] The deck lands Friday."),
    ]
