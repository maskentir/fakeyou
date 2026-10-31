from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict

from fakeyou.observability.report import read_journal
from fakeyou.persistence.record import (
    ChatEntry,
    RecordEntry,
    ScreenEntry,
    TranscriptEntry,
    TwinSpeechEntry,
    load_meeting_record,
)


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event: str
    at: AwareDatetime


class _SttTurn(_Row):
    interims: int = 0
    first_interim_to_final_ms: float | None = None


class _Final(_Row):
    transcript_id: str


class _Verdict(_Row):
    model_config = ConfigDict(extra="allow", frozen=True)

    transcript_id: str


class _Request(_Row):
    request_id: str
    operation: str | None = None
    transcript_id: str | None = None
    duration_ms: float | None = None


class _Sentence(_Row):
    transcript_id: str
    sentence_index: int
    chars: int


class _Synthesis(_Row):
    transcript_id: str | None = None
    sentence_index: int | None = None
    first_chunk_ms: float | None = None
    duration_ms: float | None = None
    bytes_out: int = 0


@dataclass(frozen=True, slots=True)
class VoiceEdge:
    offset: float
    active: bool


@dataclass(frozen=True, slots=True)
class SpokenFinal:
    offset: float
    entry: TranscriptEntry
    interims: int
    first_interim_to_final_ms: float | None


@dataclass(frozen=True, slots=True)
class Percept:
    offset: float
    entry: ChatEntry | ScreenEntry


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    delay_ms: float
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReplySentence:
    offset_ms: float
    index: int
    chars: int


@dataclass(frozen=True, slots=True)
class ReplyTiming:
    duration_ms: float | None
    sentences: tuple[ReplySentence, ...]
    spoken: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SynthesisTiming:
    first_chunk_ms: float
    duration_ms: float
    bytes_out: int


@dataclass(frozen=True, slots=True)
class ReplayFixture:
    meeting_id: str
    started_at: datetime
    edges: tuple[VoiceEdge, ...]
    finals: tuple[SpokenFinal, ...]
    percepts: tuple[Percept, ...]
    verdicts: Mapping[str, JudgeVerdict]
    replies: Mapping[str, ReplyTiming]
    syntheses: Mapping[tuple[str, int], SynthesisTiming]
    rows: tuple[dict[str, Any], ...] = field(repr=False)


def load_fixture(journal_path: Path, record_path: Path) -> ReplayFixture:
    return build_fixture(read_journal(journal_path), load_meeting_record(record_path))


def build_fixture(rows: Sequence[dict[str, Any]], entries: Sequence[RecordEntry]) -> ReplayFixture:
    timed = [_Row.model_validate(row) for row in rows]
    if not timed:
        raise ValueError("the journal has no rows to replay")
    started_at = min(row.at for row in timed)
    meeting_id = str(rows[0].get("meeting_id", ""))

    def offset(at: datetime) -> float:
        return (at - started_at).total_seconds()

    texts = {entry.id: entry for entry in entries if isinstance(entry, TranscriptEntry)}
    finals_at: dict[str, datetime] = {}
    edges: list[VoiceEdge] = []
    finals: list[SpokenFinal] = []
    stt: _SttTurn | None = None
    for row in rows:
        match row.get("event"):
            case "speech_started" | "speech_stopped" as event:
                at = _Row.model_validate(row).at
                edges.append(VoiceEdge(offset(at), event == "speech_started"))
            case "stt_turn_recorded":
                stt = _SttTurn.model_validate(row)
            case "transcript_finalized":
                final = _Final.model_validate(row)
                entry = texts.get(final.transcript_id)
                if entry is not None and final.transcript_id not in finals_at:
                    finals_at[final.transcript_id] = final.at
                    finals.append(
                        SpokenFinal(
                            offset(final.at),
                            entry,
                            stt.interims if stt is not None else 0,
                            stt.first_interim_to_final_ms if stt is not None else None,
                        )
                    )
                stt = None
            case _:
                pass
    percepts = [
        Percept(offset(entry.at), entry)
        for entry in entries
        if isinstance(entry, ChatEntry | ScreenEntry)
    ]
    return ReplayFixture(
        meeting_id=meeting_id,
        started_at=started_at,
        edges=tuple(edges),
        finals=tuple(finals),
        percepts=tuple(sorted(percepts, key=lambda percept: percept.offset)),
        verdicts=_verdicts(rows, finals_at),
        replies=_replies(rows, entries),
        syntheses=_syntheses(rows),
        rows=tuple(rows),
    )


def _verdicts(
    rows: Iterable[dict[str, Any]], finals_at: Mapping[str, datetime]
) -> dict[str, JudgeVerdict]:
    verdicts: dict[str, JudgeVerdict] = {}
    for row in rows:
        if row.get("event") != "policy_decided":
            continue
        verdict = _Verdict.model_validate(row)
        final_at = finals_at.get(verdict.transcript_id)
        if final_at is None or verdict.transcript_id in verdicts:
            continue
        delay = max(0.0, (verdict.at - final_at).total_seconds() * 1000)
        verdicts[verdict.transcript_id] = JudgeVerdict(delay, dict(verdict.model_extra or {}))
    return verdicts


def _replies(
    rows: Iterable[dict[str, Any]], entries: Iterable[RecordEntry]
) -> dict[str, ReplyTiming]:
    started: dict[str, datetime] = {}
    durations: dict[str, float] = {}
    sentences: defaultdict[str, list[ReplySentence]] = defaultdict(list)
    for row in rows:
        event = row.get("event")
        if event in ("request_started", "request_finished") and row.get("source") == "llm":
            request = _Request.model_validate(row)
            if request.operation != "chat" or request.transcript_id is None:
                continue
            if event == "request_started":
                started.setdefault(request.transcript_id, request.at)
            elif request.duration_ms is not None:
                durations.setdefault(request.transcript_id, request.duration_ms)
        elif event == "answer_sentence_emitted":
            sentence = _Sentence.model_validate(row)
            began = started.get(sentence.transcript_id)
            if began is not None:
                elapsed = (sentence.at - began).total_seconds() * 1000
                sentences[sentence.transcript_id].append(
                    ReplySentence(elapsed, sentence.sentence_index, sentence.chars)
                )
    spoken: defaultdict[str, list[str]] = defaultdict(list)
    for entry in entries:
        if isinstance(entry, TwinSpeechEntry) and entry.causation_id is not None:
            spoken[entry.causation_id].append(entry.text)
    return {
        transcript_id: ReplyTiming(
            durations.get(transcript_id),
            tuple(sentences.get(transcript_id, ())),
            tuple(spoken.get(transcript_id, ())),
        )
        for transcript_id in started
    }


def _syntheses(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, int], SynthesisTiming]:
    timings: dict[tuple[str, int], SynthesisTiming] = {}
    for row in rows:
        if row.get("event") != "synthesis_finished":
            continue
        synthesis = _Synthesis.model_validate(row)
        if (
            synthesis.transcript_id is None
            or synthesis.sentence_index is None
            or synthesis.first_chunk_ms is None
            or synthesis.duration_ms is None
        ):
            continue
        timings[(synthesis.transcript_id, synthesis.sentence_index)] = SynthesisTiming(
            synthesis.first_chunk_ms, synthesis.duration_ms, synthesis.bytes_out
        )
    return timings
