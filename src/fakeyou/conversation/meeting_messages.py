from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from fakeyou.conversation.middleware import estimate_message_tokens
from fakeyou.foundation.speakers import UNKNOWN_PARTICIPANT, speaker_name
from fakeyou.foundation.transcript import EventSource, TranscriptEvent
from fakeyou.foundation.triggers import MEETING_EVENT, TriggerKind
from fakeyou.persistence.record import RecordEntry, TranscriptEntry, TwinSpeechEntry

KIND_OF_SOURCE: dict[EventSource, TriggerKind] = {
    "meeting_audio": "speech",
    "meeting_chat": "chat",
    "meeting_attention": "attention",
}
INTERRUPTED_SUFFIX = " —"


class MeetingHistory(Protocol):
    def entries(self) -> Sequence[RecordEntry]: ...


@dataclass(frozen=True, slots=True)
class NoHistory:
    def entries(self) -> tuple[RecordEntry, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class _Spoken:
    source: EventSource
    timestamp: str
    participant_id: str
    speaker_evidence_id: str | None
    stream_id: str
    display_claim: str | None

    def meeting_event(self) -> dict[str, Any]:
        return {
            "kind": KIND_OF_SOURCE[self.source],
            "timestamp": self.timestamp,
            "source": self.source,
            "participant_id": self.participant_id,
            "speaker_evidence_id": self.speaker_evidence_id,
            "stream_id": self.stream_id,
            "speaker": speaker_name(self.participant_id, self.display_claim),
            "speaker_status": (
                "observed" if self.participant_id != UNKNOWN_PARTICIPANT else UNKNOWN_PARTICIPANT
            ),
        }


def request_messages(events: Sequence[TranscriptEvent]) -> list[HumanMessage]:
    return [
        HumanMessage(
            content=event.text,
            id=event.id,
            additional_kwargs={
                MEETING_EVENT: _Spoken(
                    event.source,
                    event.timestamp,
                    event.participant_id,
                    event.speaker_evidence_id,
                    event.stream_id,
                    event.display_claim,
                ).meeting_event()
            },
        )
        for event in events
    ]


def _history_message(entry: TranscriptEntry) -> HumanMessage:
    source: EventSource = entry.source if entry.source in KIND_OF_SOURCE else "meeting_audio"
    event = _Spoken(
        source,
        entry.spoken_at,
        entry.participant_id,
        entry.speaker_evidence_id,
        entry.stream_id,
        entry.display_claim,
    ).meeting_event()
    return HumanMessage(
        content=entry.text.strip(),
        id=entry.id,
        additional_kwargs={MEETING_EVENT: {**event, "history": True}},
    )


def _twin_text(entry: TwinSpeechEntry) -> str:
    return entry.text.strip() + (INTERRUPTED_SUFFIX if entry.interrupted else "")


def _spoken(entries: Sequence[RecordEntry], exclude: Collection[str]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for entry in entries:
        if isinstance(entry, TranscriptEntry) and entry.id not in exclude and entry.text.strip():
            messages.append(_history_message(entry))
        elif isinstance(entry, TwinSpeechEntry) and entry.text.strip():
            last = messages[-1] if messages else None
            if isinstance(last, AIMessage):
                messages[-1] = AIMessage(content=f"{last.text} {_twin_text(entry)}")
            else:
                messages.append(AIMessage(content=_twin_text(entry)))
    return messages


def history_messages(
    entries: Sequence[RecordEntry], *, exclude: Collection[str], max_tokens: int
) -> list[BaseMessage]:
    kept: list[BaseMessage] = []
    total = 0
    for message in reversed(_spoken(entries, exclude)):
        total += estimate_message_tokens([message])
        if total > max_tokens:
            break
        kept.append(message)
    kept.reverse()
    first_human = next(
        (index for index, message in enumerate(kept) if isinstance(message, HumanMessage)),
        len(kept),
    )
    return kept[first_human:]
