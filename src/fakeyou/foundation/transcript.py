import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fakeyou.foundation.timeparse import assume_utc, parse_iso_datetime

EventSource = Literal["meeting_audio", "meeting_chat", "meeting_attention"]

AttentionKind = Literal[
    "screen", "unanswered_question", "owner_task", "decision_vs_brief", "fact_opportunity"
]


@dataclass(frozen=True, slots=True)
class AttentionContext:
    kind: AttentionKind
    trigger_text: str
    trigger_event_id: str | None = None
    suggested_source_ref: str | None = None
    suggested_source: str | None = None
    source_text: str | None = None


def attention_line(context: AttentionContext) -> str:
    if context.suggested_source_ref is None or context.source_text is None:
        return context.trigger_text
    return (
        f"{context.trigger_text}\n"
        f"[Nobody asked you anything. This looks like a {context.kind} moment. "
        f"A source you know may be relevant - {context.suggested_source} "
        f"{context.suggested_source_ref}: {context.source_text}]"
    )


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    id: str
    timestamp: str
    source: EventSource
    text: str
    finalized: bool
    participant_id: str
    speaker_evidence_id: str | None
    stream_id: str = ""
    capture_ms: float = 0.0
    display_claim: str | None = None
    voice_clusters: tuple[str, ...] = ()
    attention: AttentionContext | None = None
    requests_chat: bool = False
    meet_action: str = "none"
    evidence_needs: tuple[str, ...] = ()
    reaction_emoji: str | None = None
    turn_planned: bool = False


@dataclass(frozen=True, slots=True)
class TranscriptUpdate:
    text: str
    final: bool
    timestamp: str | None = None
    language: str | None = None
    voice_clusters: tuple[str, ...] = ()
    stream_id: str = ""
    capture_seq: int | None = None
    capture_ms: float | None = None


@dataclass(frozen=True, slots=True)
class CaptureContext:
    stream_id: str = ""
    capture_ms: float = 0.0
    display_claim: str | None = None


DEFAULT_CAPTURE_CONTEXT = CaptureContext()


def transcript_event(
    update: TranscriptUpdate,
    participant_id: str,
    speaker_evidence_id: str | None,
    capture: CaptureContext = DEFAULT_CAPTURE_CONTEXT,
) -> TranscriptEvent:
    source: EventSource = "meeting_audio"
    timestamp = update.timestamp or datetime.now(UTC).isoformat()
    ordinal = update.timestamp or f"{update.stream_id}:{update.capture_seq}"
    identity = json.dumps(
        [source, ordinal, update.text, capture.stream_id],
        ensure_ascii=False,
    )
    return TranscriptEvent(
        id=hashlib.sha256(identity.encode()).hexdigest(),
        timestamp=timestamp,
        source=source,
        text=update.text,
        finalized=update.final,
        participant_id=participant_id,
        speaker_evidence_id=speaker_evidence_id,
        stream_id=capture.stream_id,
        capture_ms=capture.capture_ms,
        display_claim=capture.display_claim,
        voice_clusters=update.voice_clusters,
    )


def _merge_stamp(timestamp: str) -> datetime:
    parsed = parse_iso_datetime(timestamp)
    if parsed is None:
        return datetime.min.replace(tzinfo=UTC)
    return assume_utc(parsed)


def transcript_stamp(event: TranscriptEvent) -> datetime:
    return _merge_stamp(event.timestamp)


def merge_transcripts(events: Sequence[TranscriptEvent]) -> list[TranscriptEvent]:
    return sorted(events, key=lambda event: (_merge_stamp(event.timestamp), event.id))
