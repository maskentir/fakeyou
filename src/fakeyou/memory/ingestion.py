import asyncio
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType, EpisodicNode

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS, IngestionSettings
from fakeyou.diarization.correction_labels import (
    correction_labels_valid,
    correction_speaker_name,
    read_correction_labels,
)
from fakeyou.diarization.diarize_client import DiarizeError
from fakeyou.diarization.diarize_correction import load_corrected_record
from fakeyou.foundation.speakers import (
    TWIN_SPEAKER,
    UNIDENTIFIED_PARTICIPANT,
    VOICE_SPEAKER_SUFFIX,
    speaker_name,
    voice_speaker_name,
)
from fakeyou.foundation.timeparse import is_aware, parse_iso_datetime
from fakeyou.memory.evidence import SCREEN_SOURCE_SUFFIX, TRANSCRIPT_SOURCE_SUFFIX
from fakeyou.memory.memory import meeting_episodes, meeting_saga
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import FaultReporter, capture_error
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault, classify_exception
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    read_status,
    status_path,
    write_status,
)
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.record import (
    RecordEntry,
    ScreenEntry,
    TranscriptEntry,
    TwinSpeechEntry,
    load_meeting_record,
)

_INVALIDATE_EDGE_QUERY = (
    "MATCH (n:Entity)-[e:RELATES_TO {uuid: $uuid}]->(m:Entity) "
    "WHERE e.group_id = $group_id SET e.invalid_at = $at, e.expired_at = $at"
)
_MERGE_FLOOR_UTTERANCES = 2
_MERGE_FLOOR_CHARS = 120


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
    moment = parse_iso_datetime(value)
    if moment is None or not is_aware(moment):
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
    screen_windows: int = 0
    screen_uuids: dict[int, str] = field(default_factory=dict)


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


def screen_episode_name(meeting_id: str, index: int) -> str:
    return f"{meeting_id}:s{index:05d}"


def screen_windows(entries: Sequence[RecordEntry]) -> list[ScreenEntry]:
    latest: dict[str, ScreenEntry] = {}
    for entry in entries:
        if isinstance(entry, ScreenEntry) and entry.ref.strip():
            latest[entry.ref] = entry
    return list(latest.values())


def screen_episode_body(entry: ScreenEntry) -> str:
    shown_by = f" shown by {entry.presenter}" if entry.presenter else ""
    lines = [f"Shared screen titled {entry.title!r}{shown_by} ({entry.content})."]
    lines += [f"Key point: {point}" for point in entry.key_points]
    lines += [f"Figure: {figure}" for figure in entry.figures]
    lines += [f"Visual: {visual}" for visual in entry.visuals]
    if entry.text.strip():
        lines.append(f"Screen text: {entry.text}")
    return "\n".join(lines)


def _entry_speaker(entry: TranscriptEntry, labels: Mapping[str, str]) -> str:
    corrected = entry.corrected_speaker_uuid
    if corrected is not None and corrected in labels:
        return labels[corrected]
    return speaker_name(entry.participant_id)


def _utterance(entry: RecordEntry, labels: Mapping[str, str] | None = None) -> Utterance | None:
    if isinstance(entry, TranscriptEntry):
        if labels is None:
            speaker = speaker_name(entry.participant_id)
        else:
            speaker = _entry_speaker(entry, labels)
        spoken: datetime | None = _spoken_moment(entry.spoken_at)
    elif isinstance(entry, TwinSpeechEntry):
        speaker = TWIN_SPEAKER
        spoken = None
    else:
        return None
    return Utterance(speaker, " ".join(entry.text.split()), entry.at, spoken)


def _below_floor(window: Sequence[Utterance]) -> bool:
    chars = sum(len(utterance.text) for utterance in window)
    return len(window) < _MERGE_FLOOR_UTTERANCES and chars < _MERGE_FLOOR_CHARS


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
    merged: list[list[Utterance]] = []
    for window in windows:
        if merged and _below_floor(window):
            merged[-1].extend(window)
        else:
            merged.append(window)
    while len(merged) > 1 and _below_floor(merged[0]):
        merged[1] = merged[0] + merged[1]
        del merged[0]
    return merged


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


@dataclass(frozen=True, slots=True)
class MeetingScope:
    group_id: str
    meeting_id: str


@dataclass(frozen=True, slots=True)
class IngestScope:
    group_id: str
    meeting_id: str
    twin_name: str
    vocabulary: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class IngestControl:
    ingestion: IngestionSettings = field(default_factory=lambda: DEFAULTS.memory.ingestion)
    stop: asyncio.Event | None = None


DEFAULT_INGEST_CONTROL = IngestControl()


@dataclass(slots=True)
class _SagaView:
    memory: Graphiti
    group_id: str
    meeting_id: str
    expected_max: int
    fetched: list[EpisodicNode] | None = None

    async def episodes(self) -> list[EpisodicNode]:
        if self.fetched is None:
            self.fetched = await _saga_episodes(
                self.memory,
                group_id=self.group_id,
                meeting_id=self.meeting_id,
                expected_max=self.expected_max,
            )
        return self.fetched


async def _resumed_screens(saga: _SagaView, screens: list[ScreenEntry]) -> set[int]:
    done: set[int] = set()
    if not screens:
        return done
    for episode in await saga.episodes():
        index = _screen_index(episode.name, saga.meeting_id)
        if index is not None:
            done.add(index)
    return done


@dataclass(frozen=True, slots=True)
class _EpisodeSpec:
    name: str
    body: str
    description: str
    reference_time: datetime
    window: int
    utterances: int
    finished: str
    failed: str


class _MeetingIngestor:
    def __init__(
        self,
        memory: Graphiti,
        scope: IngestScope,
        control: IngestControl,
        diagnostics: Diagnostics,
        instructions: str,
    ) -> None:
        self._memory = memory
        self._scope = scope
        self._control = control
        self._diagnostics = diagnostics
        self._instructions = instructions

    def stopped(self) -> bool:
        stop = self._control.stop
        return stop is not None and stop.is_set()

    async def _add_episode(
        self, spec: _EpisodeSpec, previous_uuid: str | None
    ) -> tuple[str, int] | None:
        async def attempt() -> tuple[str, int]:
            timer_journal = self._diagnostics.journal or Journal("", echo=False)
            with timer_journal.timed(
                finished=spec.finished,
                failed=spec.failed,
                source="memory_ingest",
                window=spec.window,
                utterances=spec.utterances,
                facts=0,
            ) as timing:
                async with asyncio.timeout(self._control.ingestion.episode_timeout_seconds):
                    result = await self._memory.add_episode(
                        name=spec.name,
                        episode_body=spec.body,
                        source_description=spec.description,
                        reference_time=spec.reference_time,
                        source=EpisodeType.message,
                        group_id=self._scope.group_id,
                        saga=meeting_saga(self._scope.meeting_id),
                        saga_previous_episode_uuid=previous_uuid,
                        custom_extraction_instructions=self._instructions,
                    )
                timing.fields.update(facts=len(result.edges))
            return result.episode.uuid, len(result.edges)

        outcome, error = await capture_error(attempt)
        if error is not None:
            if self._diagnostics.faults is not None:
                self._diagnostics.faults.record(classify_exception("memory_ingest", error))
            return None
        assert outcome is not None
        return outcome

    async def add_window(
        self, index: int, window: list[Utterance], previous_uuid: str | None
    ) -> tuple[str, int] | None:
        return await self._add_episode(
            _EpisodeSpec(
                episode_name(self._scope.meeting_id, index),
                "\n".join(f"{u.speaker}: {u.text}" for u in window),
                f"Google Meet {self._scope.meeting_id} {TRANSCRIPT_SOURCE_SUFFIX}",
                window[0].source_at,
                index,
                len(window),
                "memory_episode_ingested",
                "memory_episode_failed",
            ),
            previous_uuid,
        )

    async def add_screen(
        self, index: int, entry: ScreenEntry, previous_uuid: str | None
    ) -> tuple[str, int] | None:
        return await self._add_episode(
            _EpisodeSpec(
                screen_episode_name(self._scope.meeting_id, index),
                screen_episode_body(entry),
                f"Google Meet {self._scope.meeting_id} {SCREEN_SOURCE_SUFFIX}",
                entry.shown_at,
                index,
                1,
                "memory_screen_ingested",
                "memory_screen_failed",
            ),
            previous_uuid,
        )

    async def drive_windows(
        self, windows: list[list[Utterance]], resumed_from: int, previous_uuid: str | None
    ) -> tuple[bool, str | None, dict[int, str], int, int]:
        ingested = facts = 0
        uuids: dict[int, str] = {}
        for index in range(resumed_from, len(windows)):
            if self.stopped():
                return False, previous_uuid, uuids, ingested, facts
            added = await self.add_window(index, windows[index], previous_uuid)
            if added is None:
                return False, previous_uuid, uuids, ingested, facts
            previous_uuid, count = added
            uuids[index] = previous_uuid
            ingested += 1
            facts += count
        return True, previous_uuid, uuids, ingested, facts

    async def drive_screens(
        self, screens: list[ScreenEntry], screen_done: set[int], previous_uuid: str | None
    ) -> tuple[bool, str | None, dict[int, str], int, int]:
        ingested = facts = 0
        uuids: dict[int, str] = {}
        for index, entry in enumerate(screens):
            if index in screen_done:
                continue
            if self.stopped():
                return False, previous_uuid, uuids, ingested, facts
            if not entry.title.strip() and not entry.text.strip():
                continue
            added = await self.add_screen(index, entry, previous_uuid)
            if added is None:
                return False, previous_uuid, uuids, ingested, facts
            previous_uuid, count = added
            uuids[index] = previous_uuid
            ingested += 1
            facts += count
        return True, previous_uuid, uuids, ingested, facts


async def ingest_meeting(
    memory: Graphiti,
    entries: Sequence[RecordEntry],
    scope: IngestScope,
    control: IngestControl = DEFAULT_INGEST_CONTROL,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> IngestResult:
    windows = meeting_windows(
        entries,
        window_utterances=control.ingestion.window_utterances,
        window_seconds=control.ingestion.window_seconds,
    )
    screens = screen_windows(entries)
    saga = _SagaView(memory, scope.group_id, scope.meeting_id, 2 * len(windows) + len(screens))
    resumed_from, previous_uuid = await _resume_point(memory, saga)
    instructions = extraction_instructions(
        scope.twin_name,
        scope.vocabulary,
        voice_labels=any(
            isinstance(entry, TranscriptEntry) and voice_speaker_name(entry.participant_id)
            for entry in entries
        ),
    )
    ingestor = _MeetingIngestor(memory, scope, control, diagnostics, instructions)
    screen_done = await _resumed_screens(saga, screens)
    completed, previous_uuid, uuids, ingested, facts = await ingestor.drive_windows(
        windows, resumed_from, previous_uuid
    )
    screen_completed, _, screen_uuids, screen_ingested, screen_facts = await ingestor.drive_screens(
        screens, screen_done, previous_uuid
    )
    ingested += screen_ingested
    facts += screen_facts
    completed = completed and screen_completed
    if diagnostics.journal is not None:
        diagnostics.journal.record(
            "memory_ingest_finished",
            source="memory_ingest",
            windows=len(windows),
            resumed_from=resumed_from,
            ingested=ingested,
            facts=facts,
            completed=completed,
            screen_windows=len(screens),
            screen_ingested=screen_ingested,
        )
    return IngestResult(
        len(windows), resumed_from, ingested, facts, completed, uuids, len(screens), screen_uuids
    )


async def _invalidate_original_edges(
    memory: Graphiti, *, group_id: str, episode_uuid: str, at: datetime
) -> int:
    found = await memory.get_nodes_and_edges_by_episode([episode_uuid])
    invalidated = 0
    for edge in found.edges:
        if edge.invalid_at is not None or edge.expired_at is not None:
            continue
        await memory.driver.execute_query(
            _INVALIDATE_EDGE_QUERY, uuid=edge.uuid, group_id=group_id, at=at
        )
        invalidated += 1
    return invalidated


def _correction_payload(record_path: Path) -> tuple[dict[str, Any], str] | None:
    sidecar = MeetingDir.from_record_path(record_path).diarization
    if not sidecar.exists():
        return None
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    corrected_at = payload.get("corrected_at")
    if not isinstance(corrected_at, str) or not corrected_at:
        raise DiarizeError(ErrorCode.DIARIZE_SIDECAR_INVALID)
    return payload, corrected_at


def correction_stamp(record_path: Path) -> str | None:
    loaded = _correction_payload(record_path)
    return None if loaded is None else loaded[1]


def _read_correction_labels(record_path: Path, payload: dict[str, Any]) -> dict[str, str]:
    stored = read_correction_labels(record_path)
    labeled = correction_labels_valid(record_path)
    labels: dict[str, str] = {}
    for turn in payload.get("turns", []):
        if (
            isinstance(turn, dict)
            and isinstance(turn.get("corrected_uuid"), str)
            and isinstance(turn.get("speaker"), str)
        ):
            uuid = turn["corrected_uuid"]
            if labeled:
                labels[uuid] = correction_speaker_name(turn["speaker"], stored.get(uuid))
            else:
                labels[uuid] = turn["speaker"]
    return labels


async def _correction_window_map(
    saga: _SagaView,
    existing: IngestionStatus | None,
    changed: list[int],
    corrected_at: str,
) -> dict[int, str]:
    window_map: dict[int, str] = {}
    if existing is not None and existing.correction_run == corrected_at:
        window_map = dict(existing.correction_windows)
    if changed and (existing is None or existing.correction_run in (None, corrected_at)):
        adopted = await _orphaned_corrections(saga, changed=changed)
        prior: set[str] = set()
        if existing is not None:
            prior.update(existing.correction_episodes)
            prior.update(existing.correction_windows.values())
        for adopted_index, adopted_uuids in adopted.items():
            for adopted_uuid in adopted_uuids:
                if adopted_uuid not in prior:
                    window_map.setdefault(adopted_index, adopted_uuid)
                    break
    return window_map


@dataclass(slots=True)
class _CorrectionDrive:
    changed: list[int]
    fixed_windows: list[list[Utterance]]
    originals: dict[int, str]
    previous_uuid: str | None
    base: IngestionStatus
    window_map: dict[int, str]
    corrected_at: str
    now: datetime
    episodes: list[str] = field(default_factory=list)
    completed: bool = True


def _finalize_corrections(
    drive: _CorrectionDrive, status_file: Path, faults: FaultReporter | None
) -> None:
    try:
        drive.base = drive.base.model_copy(
            update={
                "corrected_at": drive.corrected_at,
                "correction_episodes": list(
                    dict.fromkeys([*drive.base.correction_episodes, *drive.window_map.values()])
                ),
                "updated_at": drive.now,
            }
        )
        write_status(status_file, drive.base)
    except OSError as error:
        if faults is not None:
            faults.record(classify_exception("memory_ingest", error))
        drive.completed = False


class _CorrectionIngestor:
    def __init__(
        self,
        memory: Graphiti,
        scope: MeetingScope,
        control: IngestControl,
        diagnostics: Diagnostics,
        record_path: Path,
    ) -> None:
        self._memory = memory
        self._scope = scope
        self._control = control
        self._diagnostics = diagnostics
        self._record_path = record_path

    async def _invalidate_window(self, index: int, original: str | None, now: datetime) -> int:
        if original is None:
            if self._diagnostics.faults is not None:
                self._diagnostics.faults.record(
                    classify_exception(
                        "memory_ingest",
                        ValueError(f"missing episode uuid for corrected window {index}"),
                    )
                )
            return 0
        return await _invalidate_original_edges(
            self._memory, group_id=self._scope.group_id, episode_uuid=original, at=now
        )

    def _orphan_fault(
        self, fault: ProviderFault, index: int, written_uuid: str | None
    ) -> ProviderFault:
        if written_uuid is None:
            return fault
        detail = (
            f"correction window {index} orphaned episode {written_uuid}; retry recovers the window"
        )
        if fault.detail is not None:
            detail = f"{detail} ({fault.detail})"
        return ProviderFault(
            fault.source,
            fault.kind,
            evidence=FaultEvidence(fault.status, detail),
            retryable=fault.retryable,
            operation=fault.operation,
        )

    async def add_one(self, drive: _CorrectionDrive, index: int) -> str | None:
        window = drive.fixed_windows[index]
        written_uuid: str | None = None

        async def attempt() -> str:
            nonlocal written_uuid
            timer_journal = self._diagnostics.journal or Journal("", echo=False)
            with timer_journal.timed(
                finished="memory_correction_ingested",
                failed="memory_correction_failed",
                source="memory_ingest",
                window=index,
                utterances=len(window),
                facts=0,
            ) as timing:
                async with asyncio.timeout(self._control.ingestion.episode_timeout_seconds):
                    result = await self._memory.add_episode(
                        name=correction_episode_name(self._scope.meeting_id, index),
                        episode_body="\n".join(f"{u.speaker}: {u.text}" for u in window),
                        source_description=(
                            f"Google Meet {self._scope.meeting_id} transcript (corrected)"
                        ),
                        reference_time=drive.now,
                        source=EpisodeType.message,
                        group_id=self._scope.group_id,
                        saga=meeting_saga(self._scope.meeting_id),
                        saga_previous_episode_uuid=drive.previous_uuid,
                    )
                written_uuid = result.episode.uuid
                invalidated = await self._invalidate_window(
                    index, drive.originals.get(index), drive.now
                )
                timing.fields.update(facts=len(result.edges), invalidated=invalidated)
                drive.window_map[index] = result.episode.uuid
                drive.base = drive.base.model_copy(
                    update={
                        "correction_windows": dict(drive.window_map),
                        "correction_run": drive.corrected_at,
                        "updated_at": drive.now,
                    }
                )
                write_status(status_path(self._record_path), drive.base)
            return result.episode.uuid

        outcome, error = await capture_error(attempt)
        if error is not None:
            if self._diagnostics.faults is not None:
                fault = classify_exception("memory_ingest", error)
                self._diagnostics.faults.record(self._orphan_fault(fault, index, written_uuid))
            return None
        assert outcome is not None
        return outcome

    async def drive(self, drive_state: _CorrectionDrive) -> None:
        for index in drive_state.changed:
            if index in drive_state.window_map:
                continue
            written = await self.add_one(drive_state, index)
            if written is None:
                drive_state.completed = False
                return
            drive_state.previous_uuid = written
            drive_state.episodes.append(written)


async def ingest_corrections(
    memory: Graphiti,
    record_path: Path,
    scope: MeetingScope,
    control: IngestControl = DEFAULT_INGEST_CONTROL,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> CorrectionResult:
    loaded = _correction_payload(record_path)
    if loaded is None:
        return CorrectionResult(changed_windows=0, episodes=(), completed=True)
    payload, corrected_at = loaded
    live = load_meeting_record(record_path)
    corrected = load_corrected_record(record_path)
    labels = _read_correction_labels(record_path, payload)
    live_windows = _entries_to_windows(
        live,
        None,
        window_utterances=control.ingestion.window_utterances,
        window_seconds=control.ingestion.window_seconds,
    )
    fixed_windows = _entries_to_windows(
        corrected,
        labels,
        window_utterances=control.ingestion.window_utterances,
        window_seconds=control.ingestion.window_seconds,
    )
    changed = [
        index
        for index, (before, after) in enumerate(zip(live_windows, fixed_windows, strict=True))
        if [utterance.speaker for utterance in before] != [utterance.speaker for utterance in after]
    ]
    status_file = status_path(record_path)
    existing = read_status(status_file, journal=diagnostics.journal)
    if existing is not None and existing.corrected_at == corrected_at:
        if diagnostics.journal is not None:
            diagnostics.journal.record(
                "memory_correction_finished",
                source="memory_ingest",
                changed=len(changed),
                ingested=0,
                completed=True,
            )
        return CorrectionResult(changed_windows=len(changed), episodes=(), completed=True)
    now = datetime.now(UTC)
    base = (
        existing
        if existing is not None
        else IngestionStatus(
            meeting_id=scope.meeting_id, group_id=scope.group_id, status="not_requested"
        )
    )
    saga = _SagaView(
        memory,
        scope.group_id,
        scope.meeting_id,
        2 * len(live_windows) + len(screen_windows(live)),
    )
    window_map = await _correction_window_map(saga, existing, changed, corrected_at)
    last = await meeting_episodes(
        memory, group_id=scope.group_id, meeting_id=scope.meeting_id, last_n=1
    )
    previous_uuid = last[-1].uuid if last else None
    originals = existing.episode_uuids if existing is not None else {}
    drive = _CorrectionDrive(
        changed, fixed_windows, originals, previous_uuid, base, window_map, corrected_at, now
    )
    ingestor = _CorrectionIngestor(memory, scope, control, diagnostics, record_path)
    await ingestor.drive(drive)
    if drive.completed:
        _finalize_corrections(drive, status_file, diagnostics.faults)
    if diagnostics.journal is not None:
        diagnostics.journal.record(
            "memory_correction_finished",
            source="memory_ingest",
            changed=len(changed),
            ingested=len(drive.episodes),
            completed=drive.completed,
        )
    return CorrectionResult(len(changed), tuple(drive.episodes), drive.completed)


def _correction_index(name: str, meeting_id: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(meeting_id)}:w(\d+):corrected", name)
    return int(match.group(1)) if match is not None else None


def _screen_index(name: str, meeting_id: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(meeting_id)}:s(\d+)", name)
    return int(match.group(1)) if match is not None else None


def _transcript_window(name: str, meeting_id: str) -> bool:
    return _correction_index(name, meeting_id) is None and _screen_index(name, meeting_id) is None


async def _saga_episodes(
    memory: Graphiti, *, group_id: str, meeting_id: str, expected_max: int
) -> list[EpisodicNode]:
    return await meeting_episodes(
        memory, group_id=group_id, meeting_id=meeting_id, last_n=max(expected_max, 1)
    )


async def _orphaned_corrections(saga: _SagaView, *, changed: Sequence[int]) -> dict[int, list[str]]:
    wanted = {correction_episode_name(saga.meeting_id, index): index for index in changed}
    found: dict[int, list[str]] = {}
    for episode in await saga.episodes():
        index = wanted.get(episode.name)
        if index is not None:
            found.setdefault(index, []).append(episode.uuid)
    return found


async def _resume_point(memory: Graphiti, saga: _SagaView) -> tuple[int, str | None]:
    meeting_id = saga.meeting_id
    last = await meeting_episodes(memory, group_id=saga.group_id, meeting_id=meeting_id, last_n=1)
    if not last:
        return 0, None
    head = last[-1]
    if _transcript_window(head.name, meeting_id):
        return _next_window(head.name, meeting_id), head.uuid
    for episode in reversed(await saga.episodes()):
        if _transcript_window(episode.name, meeting_id):
            return _next_window(episode.name, meeting_id), head.uuid
    return 0, head.uuid


class EpisodeNameError(FakeYouError, ValueError):
    pass


def _next_window(name: str, meeting_id: str) -> int:
    match = re.fullmatch(rf"{re.escape(meeting_id)}:w(\d+)", name)
    if match is None:
        raise EpisodeNameError(ErrorCode.INGESTION_EPISODE_NAME_INVALID, meeting_id=meeting_id)
    return int(match.group(1)) + 1
