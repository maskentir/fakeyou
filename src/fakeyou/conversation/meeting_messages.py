from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, trim_messages

from fakeyou.foundation.speakers import UNKNOWN_PARTICIPANT, speaker_name
from fakeyou.foundation.transcript import EventSource, TranscriptEvent
from fakeyou.foundation.triggers import MEETING_EVENT, TriggerKind
from fakeyou.persistence.record import ChatEntry, RecordEntry, TranscriptEntry, TwinSpeechEntry

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


def spoken_by(line: TranscriptEvent | TranscriptEntry) -> str:
    if line.participant_id == UNKNOWN_PARTICIPANT and line.voice_clusters:
        return f"voice {line.voice_clusters[0]}"
    return speaker_name(line.participant_id, line.display_claim)


def request_messages(events: Sequence[TranscriptEvent]) -> list[HumanMessage]:
    messages: list[HumanMessage] = []
    for event in events:
        spoken = _Spoken(
            event.source,
            event.timestamp,
            event.participant_id,
            event.speaker_evidence_id,
            event.stream_id,
            event.display_claim,
        ).meeting_event()
        if event.requests_chat:
            spoken["requests_chat"] = True
        if event.meet_action != "none":
            spoken["meet_action"] = event.meet_action
        if event.evidence_needs:
            spoken["evidence_needs"] = list(event.evidence_needs)
        if event.reaction_emoji is not None:
            spoken["reaction_emoji"] = event.reaction_emoji
        if event.turn_planned:
            spoken["turn_planned"] = True
        if event.attention is not None:
            spoken["attention_kind"] = event.attention.kind
            spoken["attention_source_ref"] = event.attention.suggested_source_ref
        messages.append(
            HumanMessage(
                content=event.text,
                id=event.id,
                additional_kwargs={MEETING_EVENT: spoken},
            )
        )
    return messages


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
        elif isinstance(entry, ChatEntry) and entry.own and entry.text.strip():
            messages.append(AIMessage(content=f"[chat] {entry.text.strip()}"))
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
    return trim_messages(
        _spoken(entries, exclude),
        max_tokens=max_tokens,
        token_counter="approximate",
        strategy="last",
        start_on="human",
    )


def recent_room_lines(entries: Sequence[RecordEntry], limit: int = 6) -> tuple[str, ...]:
    lines: list[str] = []
    for entry in entries:
        if isinstance(entry, TwinSpeechEntry) and entry.text.strip():
            lines.append(f"twin: {entry.text.strip()}")
        elif isinstance(entry, TranscriptEntry) and entry.text.strip():
            lines.append(entry.text.strip())
    return tuple(lines[-limit:])


def last_twin_text(entries: Sequence[RecordEntry]) -> str | None:
    spoken = [
        entry.text.strip()
        for entry in entries
        if isinstance(entry, TwinSpeechEntry) and entry.text.strip() and not entry.interrupted
    ]
    if not spoken:
        return None
    if len(spoken) >= 2 and spoken[-1] == spoken[-2]:
        return None
    return spoken[-1]
