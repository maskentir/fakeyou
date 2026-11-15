from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from fakeyou.foundation.clock_mapping import MappedInstant
from fakeyou.foundation.content_digest import ContentSample, DigestRecord, DigestRole

IngestOutcome = Literal["accepted", "repeated", "out_of_order", "inconsistent_time"]
SourceKey = tuple[DigestRole, str]


@dataclass(slots=True)
class _Source:
    latest: ContentSample | None = None
    highest_sequence: int | None = None
    media_timestamp: float | None = None
    stopped: bool = False
    gaps: int = 0
    repeated: int = 0
    discarded: int = 0


@dataclass(slots=True)
class SampleLedger:
    revision: int = 0
    _sources: dict[SourceKey, _Source] = field(default_factory=dict)

    def ingest(
        self, record: DigestRecord, *, captured_at: datetime, capture: MappedInstant | None
    ) -> IngestOutcome:
        source = self._sources.setdefault((record.role, record.subject_id), _Source())
        highest = source.highest_sequence
        if highest is not None and record.sequence <= highest:
            source.discarded += 1
            return "out_of_order"
        if highest is not None and record.sequence > highest + 1:
            source.gaps += 1
        source.highest_sequence = record.sequence
        stamp = record.media_timestamp
        if stamp is not None and source.media_timestamp is not None:
            if stamp <= source.media_timestamp:
                source.repeated += 1
                return "repeated"
        if stamp is not None:
            source.media_timestamp = stamp
        previous = source.latest
        consistent = previous is None or captured_at >= previous.captured_at
        self.revision += 1
        source.stopped = False
        source.latest = ContentSample(record, captured_at, capture, self.revision, consistent)
        return "accepted" if consistent else "inconsistent_time"

    def stop(self, role: DigestRole, subject_id: str) -> None:
        source = self._sources.get((role, subject_id))
        if source is None or source.stopped:
            return
        self.revision += 1
        source.stopped = True

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
