import asyncio
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

from fakeyou.diarize_client import DiarizeError
from fakeyou.diarize_correction import JSON_FILE_NAME, load_corrected_record
from fakeyou.faults import FaultReporter
from fakeyou.ingestion_status import IngestionStatus, read_status, status_path, write_status
from fakeyou.journal import Journal
from fakeyou.memory import meeting_episodes, meeting_saga
from fakeyou.provider_faults import classify_exception
from fakeyou.record import RecordEntry, TranscriptEntry, TwinSpeechEntry, load_meeting_record
from fakeyou.settings import DEFAULTS
from fakeyou.voice_activity import voice_speaker_name

UNIDENTIFIED_PARTICIPANT = "Speaker with unverified identity"
VOICE_SPEAKER_SUFFIX = " with unverified identity"
TWIN_SPEAKER = "Digital twin"


@dataclass(frozen=True, slots=True)
class Utterance:
    speaker: str
    text: str
    at: datetime
    spoken_at: datetime | None = None

    @property
    def source_at(self) -> datetime:
        return self.spoken_at if self.spoken_at is not None else self.at


def _spoken_moment(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


@dataclass(frozen=True, slots=True)
class IngestResult:
    windows: int
    resumed_from: int
    ingested: int
    facts: int
    completed: bool
    episode_uuids: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    changed_windows: int
    episodes: tuple[str, ...]
    completed: bool


def extraction_instructions(
    twin_name: str, vocabulary: Sequence[str], *, voice_labels: bool = False
) -> str:
    instructions = (
        f"Speaker '{TWIN_SPEAKER}' is the owner's digital twin speaking in a Google Meet meeting; "
        f"participants call it '{twin_name}', and that name means the same entity. "
        "It is never the owner. "
        f"Speaker '{UNIDENTIFIED_PARTICIPANT}' stands for any meeting participant whose identity "
        "is unknown; that speaker may be the owner or any named person, and lines with that "
        "label may come from different people, so never merge it "
        "with a named person. Every fact must keep its speaker as the subject, for example "
        f"'{TWIN_SPEAKER} said that ...' or '{UNIDENTIFIED_PARTICIPANT} decided that ...'. "
        "Never restate what a speaker said as a plain fact without the speaker. "
        "Do not extract a fact about someone asking, questioning, requesting, or offering; "
        "only the answer, statement, decision, or commitment is a fact. "
        "Write each fact in the language the speakers used."
    )
    if voice_labels:
        instructions += (
            f" Speakers such as 'Speaker A{VOICE_SPEAKER_SUFFIX}' are anonymous voice labels "
            "that hold only within this meeting; each may be the owner or any named person, so "
            "never merge one with a named person, with another label, or with a label from "
            "another meeting."
        )
    if vocabulary:
        instructions += (
            " Known names and terms, spelled as the owner spells them; speech recognition may "
            f"mishear them, so use these spellings: {', '.join(vocabulary)}. When a transcript "
            "word sounds similar to one of these terms, treat it as that term and use its spelling."
        )
    return instructions


def episode_name(meeting_id: str, index: int) -> str:
    return f"{meeting_id}:w{index:05d}"


def correction_episode_name(meeting_id: str, index: int) -> str:
    return f"{meeting_id}:w{index:05d}:corrected"


def _speaker(participant_id: str) -> str:
    if participant_id == "unknown":
        return UNIDENTIFIED_PARTICIPANT
    voice = voice_speaker_name(participant_id)
    return participant_id if voice is None else voice + VOICE_SPEAKER_SUFFIX


def _entry_speaker(entry: TranscriptEntry, labels: Mapping[str, str]) -> str:
    corrected = entry.corrected_speaker_uuid
    if corrected is not None and corrected in labels:
        return labels[corrected]
    return _speaker(entry.participant_id)


def _utterance(entry: RecordEntry, labels: Mapping[str, str] | None = None) -> Utterance | None:
    if isinstance(entry, TranscriptEntry):
        if labels is None:
            speaker = _speaker(entry.participant_id)
        else:
            speaker = _entry_speaker(entry, labels)
        spoken: datetime | None = _spoken_moment(entry.spoken_at)
    elif isinstance(entry, TwinSpeechEntry):
        speaker = TWIN_SPEAKER
        spoken = None
    else:
        return None
    return Utterance(speaker, " ".join(entry.text.split()), entry.at, spoken)


def _pack_windows(
    utterances: Iterable[Utterance], *, window_utterances: int, window_seconds: float
) -> list[list[Utterance]]:
    windows: list[list[Utterance]] = []
    for utterance in utterances:
        current = windows[-1] if windows else None
        if (
            current is None
            or len(current) >= window_utterances
            or utterance.at - current[0].at >= timedelta(seconds=window_seconds)
        ):
            windows.append([utterance])
        else:
            current.append(utterance)
    return windows


def _entries_to_windows(
    entries: Sequence[RecordEntry],
    labels: Mapping[str, str] | None,
    *,
    window_utterances: int,
    window_seconds: float,
) -> list[list[Utterance]]:
    utterances: list[Utterance] = []
    for entry in entries:
        utterance = _utterance(entry, labels)
        if utterance is not None:
            utterances.append(utterance)
    return _pack_windows(
        utterances, window_utterances=window_utterances, window_seconds=window_seconds
    )


def meeting_windows(
    entries: Sequence[RecordEntry],
    *,
    window_utterances: int = DEFAULTS.memory.ingestion.window_utterances,
    window_seconds: float = DEFAULTS.memory.ingestion.window_seconds,
) -> list[list[Utterance]]:
    return _entries_to_windows(
        entries, None, window_utterances=window_utterances, window_seconds=window_seconds
    )


async def ingest_meeting(
    memory: Graphiti,
    entries: Sequence[RecordEntry],
    *,
    group_id: str,
    meeting_id: str,
    twin_name: str,
    vocabulary: Sequence[str] = (),
    journal: Journal | None = None,
    faults: FaultReporter | None = None,
    stop: asyncio.Event | None = None,
    episode_timeout_seconds: float = DEFAULTS.memory.ingestion.episode_timeout_seconds,
    window_utterances: int = DEFAULTS.memory.ingestion.window_utterances,
    window_seconds: float = DEFAULTS.memory.ingestion.window_seconds,
) -> IngestResult:
    windows = meeting_windows(
        entries, window_utterances=window_utterances, window_seconds=window_seconds
    )
    resumed_from, previous_uuid = await _resume_point(
        memory, group_id=group_id, meeting_id=meeting_id
    )
    instructions = extraction_instructions(
        twin_name,
        vocabulary,
        voice_labels=any(
            isinstance(entry, TranscriptEntry) and voice_speaker_name(entry.participant_id)
            for entry in entries
        ),
    )
    ingested = facts = 0
    completed = True
    uuids: dict[int, str] = {}
    for index in range(resumed_from, len(windows)):
        if stop is not None and stop.is_set():
            completed = False
            break
        window = windows[index]
        try:
            timer_journal = journal or Journal("", stdout=False)
            with timer_journal.timed(
                finished="memory_episode_ingested",
                failed="memory_episode_failed",
                source="memory_ingest",
                window=index,
                utterances=len(window),
                facts=0,
            ) as timing:
                async with asyncio.timeout(episode_timeout_seconds):
                    result = await memory.add_episode(
                        name=episode_name(meeting_id, index),
                        episode_body="\n".join(f"{u.speaker}: {u.text}" for u in window),
                        source_description=f"Google Meet {meeting_id} transcript",
                        reference_time=window[0].source_at,
                        source=EpisodeType.message,
                        group_id=group_id,
                        saga=meeting_saga(meeting_id),
                        saga_previous_episode_uuid=previous_uuid,
                        custom_extraction_instructions=instructions,
                    )
                timing.fields.update(facts=len(result.edges))
        except Exception as error:
            if faults is not None:
                faults.record(classify_exception("memory_ingest", error))
            completed = False
            break
        previous_uuid = result.episode.uuid
        uuids[index] = result.episode.uuid
        ingested += 1
        facts += len(result.edges)
    if journal is not None:
        journal.record(
            "memory_ingest_finished",
            source="memory_ingest",
            windows=len(windows),
            resumed_from=resumed_from,
            ingested=ingested,
            facts=facts,
            completed=completed,
        )
    return IngestResult(len(windows), resumed_from, ingested, facts, completed, uuids)


async def ingest_corrections(
    memory: Graphiti,
    record_path: Path,
    *,
    group_id: str,
    meeting_id: str,
    journal: Journal | None = None,
    faults: FaultReporter | None = None,
    window_utterances: int = DEFAULTS.memory.ingestion.window_utterances,
    window_seconds: float = DEFAULTS.memory.ingestion.window_seconds,
    episode_timeout_seconds: float = DEFAULTS.memory.ingestion.episode_timeout_seconds,
) -> CorrectionResult:
    sidecar = record_path.parent / JSON_FILE_NAME
    if not sidecar.exists():
        return CorrectionResult(0, (), True)
    live = load_meeting_record(record_path)
    corrected = load_corrected_record(record_path)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    corrected_at = payload.get("corrected_at")
    if not isinstance(corrected_at, str) or not corrected_at:
        raise DiarizeError("diarization sidecar is invalid")
    labels: dict[str, str] = {
        turn["corrected_uuid"]: turn["speaker"]
        for turn in payload.get("turns", [])
        if isinstance(turn, dict)
        and isinstance(turn.get("corrected_uuid"), str)
        and isinstance(turn.get("speaker"), str)
    }
    live_windows = _entries_to_windows(
        live, None, window_utterances=window_utterances, window_seconds=window_seconds
    )
    fixed_windows = _entries_to_windows(
        corrected, labels, window_utterances=window_utterances, window_seconds=window_seconds
    )
    changed = [
        index
        for index, (before, after) in enumerate(zip(live_windows, fixed_windows, strict=True))
        if [utterance.speaker for utterance in before] != [utterance.speaker for utterance in after]
    ]
    status_file = status_path(record_path)
    existing = read_status(status_file)
    if existing is not None and existing.corrected_at == corrected_at:
        if journal is not None:
            journal.record(
                "memory_correction_finished",
                source="memory_ingest",
                changed=len(changed),
                ingested=0,
                completed=True,
            )
        return CorrectionResult(len(changed), (), True)
    now = datetime.now(UTC)
    last = await meeting_episodes(memory, group_id=group_id, meeting_id=meeting_id, last_n=1)
    previous_uuid = last[-1].uuid if last else None
    episodes: list[str] = []
    completed = True
    for index in changed:
        window = fixed_windows[index]
        try:
            timer_journal = journal or Journal("", stdout=False)
            with timer_journal.timed(
                finished="memory_correction_ingested",
                failed="memory_correction_failed",
                source="memory_ingest",
                window=index,
                utterances=len(window),
                facts=0,
            ) as timing:
                async with asyncio.timeout(episode_timeout_seconds):
                    result = await memory.add_episode(
                        name=correction_episode_name(meeting_id, index),
                        episode_body="\n".join(f"{u.speaker}: {u.text}" for u in window),
                        source_description=f"Google Meet {meeting_id} transcript (corrected)",
                        reference_time=now,
                        source=EpisodeType.message,
                        group_id=group_id,
                        saga=meeting_saga(meeting_id),
                        saga_previous_episode_uuid=previous_uuid,
                    )
                timing.fields.update(facts=len(result.edges))
        except Exception as error:
            if faults is not None:
                faults.record(classify_exception("memory_ingest", error))
            completed = False
            break
        previous_uuid = result.episode.uuid
        episodes.append(result.episode.uuid)
    if completed:
        base = existing
        if base is None:
            base = IngestionStatus(meeting_id=meeting_id, group_id=group_id, status="not_requested")
        try:
            write_status(
                status_file,
                base.model_copy(
                    update={
                        "corrected_at": corrected_at,
                        "correction_episodes": [*base.correction_episodes, *episodes],
                        "updated_at": now,
                    }
                ),
            )
        except OSError as error:
            if faults is not None:
                faults.record(classify_exception("memory_ingest", error))
            completed = False
    if journal is not None:
        journal.record(
            "memory_correction_finished",
            source="memory_ingest",
            changed=len(changed),
            ingested=len(episodes),
            completed=completed,
        )
    return CorrectionResult(len(changed), tuple(episodes), completed)


def _correction_index(name: str, meeting_id: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(meeting_id)}:w(\d+):corrected", name)
    return int(match.group(1)) if match is not None else None


async def _resume_point(
    memory: Graphiti, *, group_id: str, meeting_id: str
) -> tuple[int, str | None]:
    last = await meeting_episodes(memory, group_id=group_id, meeting_id=meeting_id, last_n=1)
    if not last:
        return 0, None
    head = last[-1]
    if _correction_index(head.name, meeting_id) is None:
        return _next_window(head.name, meeting_id), head.uuid
    batch = 10
    while True:
        episodes = await meeting_episodes(
            memory, group_id=group_id, meeting_id=meeting_id, last_n=batch
        )
        for episode in reversed(episodes):
            if _correction_index(episode.name, meeting_id) is None:
                return _next_window(episode.name, meeting_id), head.uuid
        if len(episodes) < batch:
            return 0, head.uuid
        batch *= 2


def _next_window(name: str, meeting_id: str) -> int:
    match = re.fullmatch(rf"{re.escape(meeting_id)}:w(\d+)", name)
    if match is None:
        raise ValueError(
            f"invalid episode name for meeting {meeting_id}; expected {meeting_id}:w<index>; "
            "run memory-forget --meeting with --keep-record before ingesting again"
        )
    return int(match.group(1)) + 1
