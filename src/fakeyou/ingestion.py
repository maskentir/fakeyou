import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

from fakeyou.faults import FaultReporter
from fakeyou.journal import Journal
from fakeyou.memory import meeting_episodes, meeting_saga
from fakeyou.provider_faults import classify_exception
from fakeyou.record import RecordEntry, TranscriptEntry, TwinSpeechEntry
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


def _speaker(participant_id: str) -> str:
    if participant_id == "unknown":
        return UNIDENTIFIED_PARTICIPANT
    voice = voice_speaker_name(participant_id)
    return participant_id if voice is None else voice + VOICE_SPEAKER_SUFFIX


def meeting_windows(
    entries: Sequence[RecordEntry],
    *,
    window_utterances: int = DEFAULTS.memory.ingestion.window_utterances,
    window_seconds: float = DEFAULTS.memory.ingestion.window_seconds,
) -> list[list[Utterance]]:
    windows: list[list[Utterance]] = []
    for entry in entries:
        if isinstance(entry, TranscriptEntry):
            speaker = _speaker(entry.participant_id)
            spoken = _spoken_moment(entry.spoken_at)
        elif isinstance(entry, TwinSpeechEntry):
            speaker = TWIN_SPEAKER
            spoken = None
        else:
            continue
        utterance = Utterance(speaker, " ".join(entry.text.split()), entry.at, spoken)
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
    last = await meeting_episodes(memory, group_id=group_id, meeting_id=meeting_id, last_n=1)
    resumed_from = _next_window(last[-1].name, meeting_id) if last else 0
    previous_uuid = last[-1].uuid if last else None
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
    return IngestResult(len(windows), resumed_from, ingested, facts, completed)


def _next_window(name: str, meeting_id: str) -> int:
    match = re.fullmatch(rf"{re.escape(meeting_id)}:w(\d+)", name)
    if match is None:
        raise ValueError(
            f"invalid episode name for meeting {meeting_id}; expected {meeting_id}:w<index>; "
            "run memory-forget --meeting with --keep-record before ingesting again"
        )
    return int(match.group(1)) + 1
