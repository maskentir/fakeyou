from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from fakeyou.foundation.clock_mapping import MappedInstant
from fakeyou.foundation.content_digest import ContentSample, DigestRecord, DigestRole

IngestOutcome = Literal["accepted", "repeated", "out_of_order", "inconsistent_time"]


@dataclass(frozen=True, slots=True)
class Ingested:
    outcome: IngestOutcome
    sample: ContentSample


SourceKey = tuple[DigestRole, str]


@dataclass(slots=True)
class _Source:
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
    _sources: dict[SourceKey, _Source] = field(default_factory=dict)
    _history: deque[ContentSample] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))
    _stream_subjects: dict[tuple[DigestRole, str], str] = field(default_factory=dict)

    def subject_of_stream(self, role: DigestRole, stream_id: str) -> str | None:
        return self._stream_subjects.get((role, stream_id))

    def since(self, revision: int) -> tuple[ContentSample, ...]:
        return tuple(sample for sample in self._history if sample.revision > revision)

    def ingest(
        self, record: DigestRecord, *, captured_at: datetime, capture: MappedInstant | None
    ) -> Ingested:
        source = self._sources.setdefault((record.role, record.subject_id), _Source())
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
        previous = source.latest
        consistent = previous is None or captured_at >= previous.captured_at
        if previous is not None and previous.record.same_content_as(record) is not True:
            source.continuity += 1
        self.revision += 1
        source.stopped = False
        if record.stream_id:
            self._stream_subjects[record.role, record.stream_id] = record.subject_id
        source.latest = self._sample(source, record, captured_at, capture, consistent=consistent)
        self._history.append(source.latest)
        return Ingested("accepted" if consistent else "inconsistent_time", source.latest)

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
