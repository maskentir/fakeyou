from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from fakeyou.foundation.clock_mapping import MappedInstant
from fakeyou.foundation.content_digest import ContentSample, DigestRecord, DigestRole

IngestOutcome = Literal[
    "accepted", "repeated", "out_of_order", "inconsistent_time", "retired_stream", "retired_page"
]


@dataclass(frozen=True, slots=True)
class Ingested:
    outcome: IngestOutcome
    sample: ContentSample


SourceKey = tuple[DigestRole, str]
SamplerIdentity = tuple[int, str | None, str, int]


@dataclass(slots=True)
class _Source:
    incarnation: SamplerIdentity | None = None
    retired: set[SamplerIdentity] = field(default_factory=set)
    latest: ContentSample | None = None
    highest_sequence: int | None = None
    media_timestamp: float | None = None
    stopped: bool = False
    continuity: int = 0
    gaps: int = 0
    repeated: int = 0
    discarded: int = 0


HISTORY_SIZE = 512


@dataclass(slots=True)
class SampleLedger:
    revision: int = 0
    page_epoch: int = 0
    page_session: str | None = None
    _sources: dict[SourceKey, _Source] = field(default_factory=dict)
    _history: deque[ContentSample] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))
    _stream_subjects: dict[tuple[DigestRole, int, str | None, str], str] = field(
        default_factory=dict
    )
    _retired_streams: set[tuple[DigestRole, int, str | None, str, int]] = field(default_factory=set)

    def begin_page(self, epoch: int, session: str | None = None) -> None:
        if epoch < self.page_epoch or (
            epoch == self.page_epoch and self.page_session not in {None, session}
        ):
            raise ValueError("page identity must advance through a new epoch")
        if (epoch, session) == (self.page_epoch, self.page_session):
            return
        for role, subject in self._sources:
            self.stop(role, subject)
        self.page_epoch = epoch
        self.page_session = session

    def subject_of_stream(
        self, role: DigestRole, stream_id: str, *, page: tuple[int, str | None] | None = None
    ) -> str | None:
        epoch, session = (self.page_epoch, self.page_session) if page is None else page
        return self._stream_subjects.get((role, epoch, session, stream_id))

    def since(self, revision: int) -> tuple[ContentSample, ...]:
        return tuple(sample for sample in self._history if sample.revision > revision)

    def ingest(
        self, record: DigestRecord, *, captured_at: datetime, capture: MappedInstant | None
    ) -> Ingested:
        source = self._sources.setdefault((record.role, record.subject_id), _Source())
        if (record.page_epoch, record.page_session) != (self.page_epoch, self.page_session):
            source.discarded += 1
            return Ingested("retired_page", self._sample(source, record, captured_at, capture))
        if not self._admit_stream(source, record, captured_at):
            source.discarded += 1
            return Ingested("retired_stream", self._sample(source, record, captured_at, capture))
        previous = source.latest
        highest = source.highest_sequence
        if highest is not None and record.sequence <= highest:
            source.discarded += 1
            return Ingested("out_of_order", self._sample(source, record, captured_at, capture))
        if highest is not None and record.sequence > highest + 1:
            source.gaps += 1
            source.continuity += 1
        source.highest_sequence = record.sequence
        stamp = record.media_timestamp
        if stamp is not None and source.media_timestamp is not None:
            if stamp <= source.media_timestamp:
                source.repeated += 1
                return Ingested("repeated", self._sample(source, record, captured_at, capture))
        if stamp is not None:
            source.media_timestamp = stamp
        consistent = (
            previous is None
            or (
                (record.page_epoch, record.page_session)
                != (previous.record.page_epoch, previous.record.page_session)
            )
            or captured_at >= previous.captured_at
        )
        if previous is not None and previous.record.same_content_as(record) is not True:
            source.continuity += 1
        self.revision += 1
        source.stopped = False
        if record.stream_id:
            self._stream_subjects[
                record.role, record.page_epoch, record.page_session, record.stream_id
            ] = record.subject_id
        source.latest = self._sample(source, record, captured_at, capture, consistent=consistent)
        self._history.append(source.latest)
        return Ingested("accepted" if consistent else "inconsistent_time", source.latest)

    def _admit_stream(self, source: _Source, record: DigestRecord, captured_at: datetime) -> bool:
        incarnation = record.sampler_identity
        if (record.role, *incarnation) in self._retired_streams:
            return False
        previous = source.latest
        if incarnation in source.retired or (
            source.incarnation is not None
            and incarnation[:2] == source.incarnation[:2]
            and record.generation < source.incarnation[-1]
        ):
            return False
        if source.incarnation is not None and source.incarnation != incarnation:
            if (
                previous is not None
                and incarnation[:2] == source.incarnation[:2]
                and captured_at < previous.captured_at
            ):
                return False
            source.retired.add(source.incarnation)
            if source.incarnation[-1] > 0:
                self._retired_streams.add((record.role, *source.incarnation))
            source.continuity += 1
            source.highest_sequence = None
            source.media_timestamp = None
        source.incarnation = incarnation
        return True

    def _sample(
        self,
        source: _Source,
        record: DigestRecord,
        captured_at: datetime,
        capture: MappedInstant | None,
        *,
        consistent: bool = True,
    ) -> ContentSample:
        return ContentSample(
            record, captured_at, capture, self.revision, consistent, source.continuity
        )

    def stop(self, role: DigestRole, subject_id: str) -> None:
        source = self._sources.get((role, subject_id))
        if source is None or source.stopped:
            return
        self.revision += 1
        source.stopped = True
        source.continuity += 1
        if source.incarnation is not None and source.incarnation[-1] > 0:
            source.retired.add(source.incarnation)
            self._retired_streams.add((role, *source.incarnation))

    def latest(self, role: DigestRole, subject_id: str) -> ContentSample | None:
        source = self._sources.get((role, subject_id))
        return None if source is None else source.latest

    def stopped(self, role: DigestRole, subject_id: str) -> bool:
        source = self._sources.get((role, subject_id))
        return source is not None and source.stopped

    def next_sequence(self, role: DigestRole, subject_id: str) -> int:
        source = self._sources.get((role, subject_id))
        if source is None or source.highest_sequence is None:
            return 1
        return source.highest_sequence + 1

    def irregularities(self, role: DigestRole, subject_id: str) -> dict[str, int]:
        source = self._sources.get((role, subject_id)) or _Source()
        return {"gaps": source.gaps, "repeated": source.repeated, "discarded": source.discarded}
