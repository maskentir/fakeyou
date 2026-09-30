import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

from fakeyou.conversation import TranscriptEvent
from fakeyou.faults import FaultReporter
from fakeyou.journal import ProviderJournal
from fakeyou.memory import meeting_saga
from fakeyou.provider_faults import ProviderFault, classify_exception

UtteranceSource = Literal["participant", "twin"]

UNIDENTIFIED_PARTICIPANT = "Unidentified participant"
TWIN_SPEAKER = "Digital twin"
SOURCE_LABELS: dict[UtteranceSource, str] = {
    "participant": "participant transcript",
    "twin": "twin speech",
}


@dataclass(frozen=True, slots=True)
class Utterance:
    source: UtteranceSource
    speaker: str
    text: str
    spoken_at: datetime


def extraction_instructions(twin_name: str) -> str:
    return (
        f"Speaker '{TWIN_SPEAKER}' is the owner's digital twin speaking in a Google Meet meeting; "
        f"participants call it '{twin_name}', and that name means the same entity. "
        "It is never the owner. "
        f"Speaker '{UNIDENTIFIED_PARTICIPANT}' stands for any meeting participant whose identity "
        "is unknown; lines with that label may come from different people, so never merge it "
        "with a named person. Keep in each fact who said, proposed, or agreed to it."
    )


class MeetingIngestion:
    def __init__(
        self,
        memory: Graphiti,
        *,
        group_id: str,
        meeting_id: str,
        twin_name: str,
        faults: FaultReporter | None = None,
        journal: ProviderJournal | None = None,
        window_seconds: float = 60.0,
        max_utterances: int = 20,
        queue_size: int = 500,
        episode_timeout_seconds: float = 120.0,
        drain_timeout_seconds: float = 20.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._memory = memory
        self._group_id = group_id
        self._meeting_id = meeting_id
        self._instructions = extraction_instructions(twin_name)
        self._faults = faults
        self._journal = journal
        self._window_seconds = window_seconds
        self._max_utterances = max_utterances
        self._episode_timeout_seconds = episode_timeout_seconds
        self._drain_timeout_seconds = drain_timeout_seconds
        self._now = now
        self._clock = clock
        self._queue: asyncio.Queue[Utterance | None] = asyncio.Queue(maxsize=queue_size)
        self._seen: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._previous_episode_uuid: str | None = None
        self._episode_count = 0
        self._closed = False

    def add_transcript(self, event: TranscriptEvent) -> None:
        if not event.finalized or not event.text.strip() or event.id in self._seen:
            return
        self._seen.add(event.id)
        speaker = UNIDENTIFIED_PARTICIPANT if event.participant == "unknown" else event.participant
        self._enqueue(Utterance("participant", speaker, event.text, self._spoken_at(event)))

    def add_twin_speech(self, text: str) -> None:
        if text.strip():
            self._enqueue(Utterance("twin", TWIN_SPEAKER, text, self._now()))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            return
        try:
            async with asyncio.timeout(self._drain_timeout_seconds):
                await self._queue.put(None)
                await self._task
        except TimeoutError:
            self._report(
                ProviderFault(
                    "memory_ingest",
                    "timeout",
                    detail=f"{self._queue.qsize()} queued utterances were not stored",
                )
            )
        finally:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _spoken_at(self, event: TranscriptEvent) -> datetime:
        try:
            spoken_at = datetime.fromisoformat(event.timestamp)
        except ValueError:
            return self._now()
        return spoken_at if spoken_at.tzinfo is not None else spoken_at.replace(tzinfo=UTC)

    def _enqueue(self, utterance: Utterance) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(utterance)
        except asyncio.QueueFull:
            self._report(
                ProviderFault(
                    "memory_ingest_backlog", "unavailable", detail="utterance dropped; queue full"
                )
            )
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def _report(self, fault: ProviderFault) -> None:
        if self._faults is not None:
            self._faults.record(fault)

    async def _run(self) -> None:
        batch: list[Utterance] = []
        batch_started = 0.0
        while True:
            wait = max(0.0, batch_started + self._window_seconds - self._clock()) if batch else None
            try:
                async with asyncio.timeout(wait):
                    utterance = await self._queue.get()
            except TimeoutError:
                await self._store(batch)
                batch = []
                continue
            if utterance is None:
                await self._store(batch)
                return
            if batch and utterance.source != batch[0].source:
                await self._store(batch)
                batch = []
            if not batch:
                batch_started = self._clock()
            batch.append(utterance)
            if (
                len(batch) >= self._max_utterances
                or self._clock() - batch_started >= self._window_seconds
            ):
                await self._store(batch)
                batch = []

    async def _store(self, batch: list[Utterance]) -> None:
        if not batch:
            return
        self._episode_count += 1
        source = batch[0].source
        started = self._clock()
        try:
            async with asyncio.timeout(self._episode_timeout_seconds):
                result = await self._memory.add_episode(
                    name=f"{self._meeting_id}:{self._episode_count:05d}",
                    episode_body="\n".join(
                        f"{utterance.speaker}: {' '.join(utterance.text.split())}"
                        for utterance in batch
                    ),
                    source_description=f"Google Meet {self._meeting_id} {SOURCE_LABELS[source]}",
                    reference_time=batch[0].spoken_at,
                    source=EpisodeType.message,
                    group_id=self._group_id,
                    saga=meeting_saga(self._meeting_id),
                    saga_previous_episode_uuid=self._previous_episode_uuid,
                    custom_extraction_instructions=self._instructions,
                )
        except Exception as error:
            self._report(classify_exception("memory_ingest", error))
            self._record_episode(batch, started, outcome="failed", facts=0)
            return
        self._previous_episode_uuid = result.episode.uuid
        self._record_episode(
            batch,
            started,
            outcome="extracted" if result.edges else "empty",
            facts=len(result.edges),
        )

    def _record_episode(
        self, batch: list[Utterance], started: float, *, outcome: str, facts: int
    ) -> None:
        if self._journal is not None:
            self._journal.record(
                "memory_episode",
                source="memory_ingest",
                speaker_kind=batch[0].source,
                utterances=len(batch),
                facts=facts,
                outcome=outcome,
                duration_ms=(self._clock() - started) * 1000,
            )
