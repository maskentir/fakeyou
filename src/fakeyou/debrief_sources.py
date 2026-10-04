import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from fakeyou.brief import MAX_CONTEXT_BYTES, MeetingBrief, MeetingContext
from fakeyou.debrief_schema import DebriefDraft, DebriefValidationError, EvidenceRef
from fakeyou.record import RecordEntry, TranscriptEntry, TwinSpeechEntry

_RECORD_ENTRY = TypeAdapter[RecordEntry](RecordEntry)


@dataclass(frozen=True, slots=True)
class SourceEntry:
    id: str
    text: str
    kind: Literal["participant", "twin"]
    participant_id: str
    at: datetime

    def __post_init__(self) -> None:
        if self.at.utcoffset() is None:
            raise DebriefValidationError("source timestamp must be timezone aware")


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    record_sha256: str
    sources: tuple[SourceEntry, ...]


def load_record_snapshot(path: Path) -> RecordSnapshot:
    try:
        raw = path.read_bytes()
    except OSError:
        raise DebriefValidationError("unable to read meeting record") from None
    sources: list[SourceEntry] = []
    transcripts: dict[str, TranscriptEntry] = {}
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = _RECORD_ENTRY.validate_json(line)
        except ValidationError:
            raise DebriefValidationError(f"invalid meeting record at line {line_number}") from None
        if isinstance(entry, TranscriptEntry):
            if not entry.id.strip():
                raise DebriefValidationError("transcript source id must not be blank") from None
            previous = transcripts.get(entry.id)
            if previous is not None:
                if previous != entry:
                    raise DebriefValidationError(
                        "contradictory duplicate transcript source"
                    ) from None
                continue
            transcripts[entry.id] = entry
            if entry.text.strip():
                sources.append(
                    SourceEntry(
                        id=f"transcript:{entry.id}",
                        text=entry.text,
                        kind="participant",
                        participant_id=entry.participant_id,
                        at=entry.at,
                    )
                )
        elif isinstance(entry, TwinSpeechEntry) and entry.text.strip():
            sources.append(
                SourceEntry(
                    id=f"twin_speech:{line_number}",
                    text=entry.text,
                    kind="twin",
                    participant_id="twin",
                    at=entry.at,
                )
            )
    if not sources:
        raise DebriefValidationError("meeting record contains no usable speech sources") from None
    return RecordSnapshot(record_sha256=hashlib.sha256(raw).hexdigest(), sources=tuple(sources))


def load_context_snapshot(path: Path) -> tuple[MeetingContext | None, str | None]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONTEXT_BYTES + 1)
    except FileNotFoundError:
        return None, None
    except OSError:
        raise DebriefValidationError("unable to read meeting context") from None
    if len(raw) > MAX_CONTEXT_BYTES:
        raise DebriefValidationError(
            f"meeting context must be at most {MAX_CONTEXT_BYTES} bytes"
        ) from None
    try:
        context = MeetingContext.model_validate_json(raw)
    except ValidationError:
        raise DebriefValidationError("invalid meeting context") from None
    return context, hashlib.sha256(raw).hexdigest()


def chunk_sources(
    sources: Sequence[SourceEntry],
    *,
    max_entries: int = 40,
    max_characters: int = 16000,
    overlap: int = 4,
) -> tuple[tuple[SourceEntry, ...], ...]:
    if not (1 <= max_entries <= 40 and 1 <= max_characters <= 16000 and 0 <= overlap <= 4):
        raise DebriefValidationError("invalid debrief chunk limits")
    if not sources:
        raise DebriefValidationError("no debrief sources to chunk")
    if any(len(entry.text) > max_characters for entry in sources):
        raise DebriefValidationError("speech source exceeds the chunk character limit")
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
            raise DebriefValidationError("meeting record exceeds the 20 chunk limit")
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
        raise DebriefValidationError("missing evidence")
    for reference in evidence:
        source = sources.get(reference.entry_id)
        if (
            source is None
            or source.id != reference.entry_id
            or not reference.quote.strip()
            or reference.quote not in source.text
        ):
            raise DebriefValidationError("invalid evidence reference")


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
        raise DebriefValidationError("invalid debrief draft") from None
    for item in draft.items:
        validate_evidence(item.evidence, sources)
        quotes = [reference.quote for reference in item.evidence]
        for claim in (item.assignee_claim, item.due_text):
            if claim is not None and not _claim_in_quotes(claim, quotes):
                raise DebriefValidationError("item claim is not in its evidence quotes")
    valid_ids = (
        {f"objective:{item.id}" for item in brief.objectives}
        | {f"question:{item.id}" for item in brief.questions}
        if brief is not None
        else set()
    )
    for finding in draft.findings:
        if finding.objective_id not in valid_ids:
            raise DebriefValidationError("finding does not belong to the meeting brief")
        if finding.evidence or finding.status == "addressed":
            validate_evidence(finding.evidence, sources)
        if finding.status == "addressed" and not any(
            sources[reference.entry_id].kind == "participant" for reference in finding.evidence
        ):
            raise DebriefValidationError("addressed finding needs participant evidence")
