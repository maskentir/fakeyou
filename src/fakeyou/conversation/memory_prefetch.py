import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from fakeyou.conversation.turn_events import JournalSink
from fakeyou.foundation.provenance import ExcludedDict, MemoryEvidence
from fakeyou.observability.faults import FaultReporter, capture_error
from fakeyou.observability.provider_faults import classify_exception

MAX_RETAINED_EVIDENCE = 3


class MemorySearch(Protocol):
    async def __call__(self, query: str, *, owner_id: str, twin_id: str) -> MemoryEvidence: ...


@dataclass(frozen=True, slots=True)
class MemoryPrefetch:
    search: MemorySearch
    owner_id: str
    twin_id: str
    timeout_seconds: float


def retain_evidence(
    current: Sequence[MemoryEvidence], update: Sequence[MemoryEvidence]
) -> list[MemoryEvidence]:
    return [*current, *update][-MAX_RETAINED_EVIDENCE:]


def unavailable_evidence() -> MemoryEvidence:
    return MemoryEvidence(
        outcome="unavailable",
        facts=[],
        excluded=ExcludedDict(pending=0, superseded=0, missing_source=0),
    )


async def _search_within(prefetch: MemoryPrefetch, query: str) -> MemoryEvidence:
    async with asyncio.timeout(prefetch.timeout_seconds):
        return await prefetch.search(query, owner_id=prefetch.owner_id, twin_id=prefetch.twin_id)


async def prefetch_memory_evidence(
    prefetch: MemoryPrefetch,
    query: str,
    *,
    journal: JournalSink,
    transcript_id: str,
    faults: FaultReporter | None = None,
) -> MemoryEvidence:
    started = time.monotonic()
    evidence, error = await capture_error(lambda: _search_within(prefetch, query))
    duration_ms = (time.monotonic() - started) * 1000
    if error is None and evidence is not None:
        journal.record(
            "memory_prefetch_finished",
            source="memory",
            transcript_id=transcript_id,
            outcome=evidence["outcome"],
            facts=len(evidence["facts"]),
            duration_ms=duration_ms,
        )
        return evidence
    if faults is not None and error is not None:
        faults.record(classify_exception("memory", error, operation="prefetch"))
    journal.record(
        "memory_prefetch_finished",
        source="memory",
        transcript_id=transcript_id,
        outcome="unavailable",
        error=type(error).__name__,
        duration_ms=duration_ms,
    )
    return unavailable_evidence()
