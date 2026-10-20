import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from fakeyou.audio.voice_activity import voice_speaker_name
from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import DebriefDraft, DebriefValidationError, EvidenceRef
from fakeyou.diarization.diarize_client import DiarizeError
from fakeyou.diarization.diarize_correction import load_corrected_record
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_exception
from fakeyou.persistence.brief import MAX_CONTEXT_BYTES, MeetingBrief, MeetingContext
from fakeyou.persistence.record import (
    ChatEntry,
    RecordEntry,
    ScreenEntry,
    TranscriptEntry,
    TwinSpeechEntry,
    record_header_of,
)

_RECORD_ENTRY = TypeAdapter[RecordEntry](RecordEntry)


@dataclass(frozen=True, slots=True)
class SourceEntry:
    id: str
    text: str
    kind: Literal["participant", "twin", "screen", "chat"]
    participant_id: str
    at: datetime
    display_claim: str | None = None

    def __post_init__(self) -> None:
        if self.at.utcoffset() is None:
            raise DebriefValidationError(ErrorCode.DEBRIEF_SOURCE_TIME_NOT_AWARE)


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    record_sha256: str
    sources: tuple[SourceEntry, ...]


def _corrected_speakers(
    path: Path, faults: FaultReporter | None, journal: Journal | None
) -> dict[str, str]:
    try:
        entries = load_corrected_record(path)
    except (DiarizeError, OSError, ValueError) as error:
        fault = classify_exception("meeting_record", error)
        if faults is not None:
            faults.record(fault)
        elif journal is not None:
            journal.record(
                "fault_recorded",
                source=fault.source,
                operation=fault.operation,
                kind=fault.kind,
                status=fault.status,
                detail=fault.detail,
            )
        return {}
    return {
        f"transcript:{entry.id}": uuid
        for entry in entries
        if isinstance(entry, TranscriptEntry) and (uuid := entry.corrected_speaker_uuid) is not None
    }


def _is_record_header(line: bytes, line_number: int) -> bool:
    try:
        return record_header_of(line) is not None
    except ValidationError:
        raise DebriefValidationError(
            ErrorCode.DEBRIEF_RECORD_INVALID_LINE, line=line_number
        ) from None


def _entry_lines(raw: bytes) -> Iterator[tuple[int, bytes]]:
    numbered = [
        (line_number, line)
        for line_number, line in enumerate(raw.splitlines(), start=1)
        if line.strip()
    ]
    if numbered and _is_record_header(numbered[0][1], numbered[0][0]):
        numbered = numbered[1:]
    return iter(numbered)


def _parse_record_entry(line: bytes, line_number: int) -> RecordEntry:
    try:
        return _RECORD_ENTRY.validate_json(line)
    except ValidationError:
        raise DebriefValidationError(
            ErrorCode.DEBRIEF_RECORD_INVALID_LINE, line=line_number
        ) from None


def _append_transcript(
    sources: list[SourceEntry],
    transcripts: dict[str, TranscriptEntry],
    entry: TranscriptEntry,
) -> None:
    if not entry.id.strip():
        raise DebriefValidationError(ErrorCode.DEBRIEF_TRANSCRIPT_ID_BLANK) from None
    previous = transcripts.get(entry.id)
    if previous is not None:
        if previous != entry:
            raise DebriefValidationError(ErrorCode.DEBRIEF_TRANSCRIPT_CONTRADICTORY) from None
        return
    transcripts[entry.id] = entry
    if entry.text.strip():
        sources.append(
            SourceEntry(
                id=f"transcript:{entry.id}",
                text=entry.text,
                kind="participant",
                participant_id=entry.participant_id,
                at=entry.at,
                display_claim=entry.display_claim,
            )
        )


def _append_twin(sources: list[SourceEntry], entry: TwinSpeechEntry, line_number: int) -> None:
    if not entry.text.strip():
        return
    sources.append(
        SourceEntry(
            id=f"twin_speech:{line_number}",
            text=entry.text,
            kind="twin",
            participant_id="twin",
            at=entry.at,
        )
    )


SCREEN_SOURCE_MAX_CHARS = 2000
SHARED_SCREEN = "shared_screen"


def screen_source_text(entry: ScreenEntry) -> str:
    parts = [entry.title, *entry.key_points, *entry.figures, *entry.visuals, entry.text]
    text = "\n".join(part for part in parts if part.strip())
    return text[:SCREEN_SOURCE_MAX_CHARS]


def _append_screen(sources: list[SourceEntry], screens: dict[str, int], entry: ScreenEntry) -> None:
    if not entry.ref.strip():
        raise DebriefValidationError(ErrorCode.DEBRIEF_SCREEN_REF_BLANK) from None
    source = SourceEntry(
        id=f"screen:{entry.ref}",
        text=screen_source_text(entry),
        kind="screen",
        participant_id=entry.presenter_id or SHARED_SCREEN,
        at=entry.shown_at,
        display_claim=entry.presenter,
    )
    position = screens.get(entry.ref)
    if position is None:
        screens[entry.ref] = len(sources)
        sources.append(source)
    else:
        sources[position] = source


def _append_chat(sources: list[SourceEntry], chats: dict[str, ChatEntry], entry: ChatEntry) -> None:
    previous = chats.get(entry.message_id)
    if previous is not None:
        if previous.text != entry.text or previous.sender_id != entry.sender_id:
            raise DebriefValidationError(ErrorCode.DEBRIEF_CHAT_CONTRADICTORY) from None
        return
    chats[entry.message_id] = entry
    sources.append(
        SourceEntry(
            id=f"chat:{entry.ref}",
            text=entry.text,
            kind="twin" if entry.own else "chat",
            participant_id="twin" if entry.own else entry.sender_id,
            at=entry.sent_at,
            display_claim=None if entry.own else entry.sender,
        )
    )


def load_record_snapshot(
    path: Path, *, faults: FaultReporter | None = None, journal: Journal | None = None
) -> RecordSnapshot:
    try:
        raw = path.read_bytes()
    except OSError:
        raise DebriefValidationError(ErrorCode.DEBRIEF_RECORD_UNREADABLE) from None
    sources: list[SourceEntry] = []
    transcripts: dict[str, TranscriptEntry] = {}
    screens: dict[str, int] = {}
    chats: dict[str, ChatEntry] = {}
    for line_number, line in _entry_lines(raw):
        entry = _parse_record_entry(line, line_number)
        if isinstance(entry, TranscriptEntry):
            _append_transcript(sources, transcripts, entry)
        elif isinstance(entry, TwinSpeechEntry):
            _append_twin(sources, entry, line_number)
        elif isinstance(entry, ScreenEntry):
            _append_screen(sources, screens, entry)
        elif isinstance(entry, ChatEntry):
            _append_chat(sources, chats, entry)
    if not sources:
        raise DebriefValidationError(ErrorCode.DEBRIEF_NO_SPEECH_SOURCES) from None
    corrected = _corrected_speakers(path, faults, journal)
    if corrected:
        sources = [
            replace(entry, participant_id=speaker)
            if (speaker := corrected.get(entry.id)) is not None
            else entry
            for entry in sources
        ]
    return RecordSnapshot(record_sha256=hashlib.sha256(raw).hexdigest(), sources=tuple(sources))


def load_context_snapshot(path: Path) -> tuple[MeetingContext | None, str | None]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONTEXT_BYTES + 1)
    except FileNotFoundError:
        return None, None
    except OSError:
        raise DebriefValidationError(ErrorCode.DEBRIEF_CONTEXT_UNREADABLE) from None
    if len(raw) > MAX_CONTEXT_BYTES:
        raise DebriefValidationError(
            ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE, limit=MAX_CONTEXT_BYTES
        ) from None
    try:
        context = MeetingContext.model_validate_json(raw)
    except ValidationError:
        raise DebriefValidationError(ErrorCode.DEBRIEF_CONTEXT_INVALID) from None
    return context, hashlib.sha256(raw).hexdigest()


def chunk_sources(
    sources: Sequence[SourceEntry],
    *,
    max_entries: int = 40,
    max_characters: int = 16000,
    overlap: int = 4,
) -> tuple[tuple[SourceEntry, ...], ...]:
    if not (1 <= max_entries <= 40 and 1 <= max_characters <= 16000 and 0 <= overlap <= 4):
        raise DebriefValidationError(ErrorCode.DEBRIEF_CHUNK_LIMITS_INVALID)
    if not sources:
        raise DebriefValidationError(ErrorCode.DEBRIEF_NO_SOURCES)
    if any(len(entry.text) > max_characters for entry in sources):
        raise DebriefValidationError(ErrorCode.DEBRIEF_SOURCE_EXCEEDS_CHUNK)
    chunks: list[tuple[SourceEntry, ...]] = []
    start = 0
    while start < len(sources):
        end = start
        characters = 0
        while (
            end < len(sources)
            and end - start < max_entries
            and characters + len(sources[end].text) <= max_characters
        ):
            characters += len(sources[end].text)
            end += 1
        chunks.append(tuple(sources[start:end]))
        if len(chunks) > 20:
            raise DebriefValidationError(ErrorCode.DEBRIEF_TOO_MANY_CHUNKS)
        if end == len(sources):
            break
        next_start = max(start + 1, end - overlap)
        overlap_characters = sum(len(entry.text) for entry in sources[next_start:end])
        while (
            end - next_start >= max_entries
            or overlap_characters + len(sources[end].text) > max_characters
        ):
            overlap_characters -= len(sources[next_start].text)
            next_start += 1
        start = next_start
    return tuple(chunks)


def validate_evidence(evidence: Sequence[EvidenceRef], sources: Mapping[str, SourceEntry]) -> None:
    if not evidence:
        raise DebriefValidationError(ErrorCode.DEBRIEF_EVIDENCE_MISSING)
    for reference in evidence:
        source = sources.get(reference.entry_id)
        if (
            source is None
            or source.id != reference.entry_id
            or not reference.quote.strip()
            or reference.quote not in source.text
        ):
            raise DebriefValidationError(ErrorCode.DEBRIEF_EVIDENCE_INVALID)


CLAIM_EDGE_PUNCTUATION = ".,!?;:\"'“”‘’()[]{}-–—… "


def _claim_in_quotes(claim: str, quotes: Sequence[str]) -> bool:
    words = claim.strip(CLAIM_EDGE_PUNCTUATION).split()
    if not words:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?!\w)"
    return any(re.search(pattern, quote, re.IGNORECASE) for quote in quotes)


def validate_draft(
    draft: DebriefDraft, *, sources: Mapping[str, SourceEntry], brief: MeetingBrief | None
) -> None:
    try:
        draft = DebriefDraft.model_validate(draft.model_dump())
    except ValidationError:
        raise DebriefValidationError(ErrorCode.DEBRIEF_DRAFT_INVALID) from None
    for item in draft.items:
        validate_evidence(item.evidence, sources)
        quotes = [reference.quote for reference in item.evidence]
        for claim in (item.assignee_claim, item.due_text):
            if claim is not None and not _claim_in_quotes(claim, quotes):
                raise DebriefValidationError(ErrorCode.DEBRIEF_CLAIM_NOT_IN_QUOTES)
    valid_ids = (
        {f"objective:{item.id}" for item in brief.objectives}
        | {f"question:{item.id}" for item in brief.questions}
        if brief is not None
        else set()
    )
    for finding in draft.findings:
        if finding.objective_id not in valid_ids:
            raise DebriefValidationError(ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF)
        if finding.evidence or finding.status == "addressed":
            validate_evidence(finding.evidence, sources)
        if finding.status == "addressed" and not any(
            sources[reference.entry_id].kind == "participant" for reference in finding.evidence
        ):
            raise DebriefValidationError(ErrorCode.DEBRIEF_ADDRESSED_NEEDS_PARTICIPANT)


def voice_speaker_labels(sources: Sequence[SourceEntry]) -> list[str]:
    labels: list[str] = []
    for entry in sources:
        label = voice_speaker_name(entry.participant_id, entry.display_claim)
        if label is not None and label not in labels:
            labels.append(label)
    return labels
