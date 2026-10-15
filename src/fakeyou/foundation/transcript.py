import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.messages import BaseMessage

from fakeyou.foundation.timeparse import assume_utc, parse_iso_datetime


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    id: str
    timestamp: str
    source: str
    text: str
    finalized: bool
    participant_id: str
    speaker_evidence_id: str | None
    stream_id: str = ""
    capture_ms: float = 0.0
    display_claim: str | None = None
    visual_refs: tuple[str, ...] = ()
    voice_clusters: tuple[str, ...] = ()


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
    visual_refs: tuple[str, ...] = ()


DEFAULT_CAPTURE_CONTEXT = CaptureContext()


def transcript_event(
    update: TranscriptUpdate,
    participant_id: str,
    speaker_evidence_id: str | None,
    capture: CaptureContext = DEFAULT_CAPTURE_CONTEXT,
) -> TranscriptEvent:
    source = "meeting_audio"
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
        visual_refs=capture.visual_refs,
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


def visual_refs_of(message: BaseMessage) -> list[str]:
    event = (message.additional_kwargs or {}).get("meeting_event")
    if not isinstance(event, dict):
        return []
    refs = event.get("visual_refs")
    if not isinstance(refs, (list, tuple)):
        return []
    return [ref for ref in refs if isinstance(ref, str) and ref]
