from datetime import datetime
from typing import Protocol

from fakeyou.db.owner_model import (
    OwnerModelEntry,
    OwnerModelRevision,
    OwnerModelSnapshot,
    RevisionSource,
    TwinScope,
)
from fakeyou.debrief.debrief_schema import DebriefReport


class ProposalRepository(Protocol):
    async def add(
        self, scope: TwinScope, entry: OwnerModelEntry, source: RevisionSource, at: datetime
    ) -> OwnerModelRevision: ...

    async def proposals(self, scope: TwinScope) -> list[OwnerModelRevision]: ...

    async def snapshot(
        self, scope: TwinScope, version: int | None = None
    ) -> OwnerModelSnapshot: ...


def _held_as(entry: OwnerModelEntry) -> tuple[str, str, str]:
    return (entry.kind, entry.topic.casefold(), entry.statement.casefold())


def entries_from_proposals(report: DebriefReport) -> tuple[OwnerModelEntry, ...]:
    entries: list[OwnerModelEntry] = []
    for proposal in report.owner_model_proposals:
        quote = proposal.evidence[0].quote
        entries.append(
            OwnerModelEntry(
                kind=proposal.kind,
                topic=proposal.topic,
                statement=proposal.statement,
                confidence="leaning",
                disclosure="owner_only",
                reason=f'debrief {report.meeting_id}: "{quote}"',
            )
        )
    return tuple(entries)


async def publish_proposals(
    repository: ProposalRepository, scope: TwinScope, report: DebriefReport, *, at: datetime
) -> int:
    candidates = entries_from_proposals(report)
    if not candidates:
        return 0
    held = {_held_as(revision.entry) for revision in await repository.proposals(scope)}
    snapshot = await repository.snapshot(scope)
    held.update(_held_as(revision.entry) for revision in snapshot.items)
    written = 0
    for entry in candidates:
        if _held_as(entry) in held:
            continue
        held.add(_held_as(entry))
        await repository.add(scope, entry, "debrief", at)
        written += 1
    return written
