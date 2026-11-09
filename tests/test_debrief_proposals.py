from datetime import UTC, datetime
from pathlib import Path

import fakeyou.debrief.debrief as debrief_module
from fakeyou.db.owner_model import (
    OwnerModelEntry,
    OwnerModelRevision,
    OwnerModelSnapshot,
    RevisionSource,
    TwinScope,
)
from fakeyou.debrief.debrief_schema import DebriefDraft, OwnerModelProposal
from fakeyou.debrief.proposals import entries_from_proposals, publish_proposals
from tests._debrief_support import record_path as record_path
from tests._debrief_support import ref, report_for

AT = datetime(2026, 10, 3, 12, tzinfo=UTC)
SCOPE = TwinScope(owner_id="b2-owner", twin_id="b2-twin")


def proposal(**changes: object) -> OwnerModelProposal:
    values: dict[str, object] = {
        "kind": "position",
        "topic": "beta launch date",
        "statement": "The beta launches on 14 October, not 21 October.",
        "evidence": [ref()],
    }
    return OwnerModelProposal.model_validate(values | changes)


class _Repository:
    def __init__(self, held: tuple[OwnerModelEntry, ...] = ()) -> None:
        self.added: list[OwnerModelEntry] = []
        self._held = held

    async def add(
        self, scope: TwinScope, entry: OwnerModelEntry, source: RevisionSource, at: datetime
    ) -> OwnerModelRevision:
        del scope, at
        self.added.append(entry)
        return OwnerModelRevision(
            revision_id=f"r{len(self.added)}",
            item_id=f"i{len(self.added)}",
            revision=1,
            scope=SCOPE,
            entry=entry,
            source=source,
            status="proposed",
            model_version=None,
            created_at=AT,
            decided_at=None,
        )

    async def proposals(self, scope: TwinScope) -> list[OwnerModelRevision]:
        del scope
        return [
            OwnerModelRevision(
                revision_id="held",
                item_id="held",
                revision=1,
                scope=SCOPE,
                entry=entry,
                source="debrief",
                status="proposed",
                model_version=None,
                created_at=AT,
                decided_at=None,
            )
            for entry in self._held
        ]

    async def snapshot(self, scope: TwinScope, version: int | None = None) -> OwnerModelSnapshot:
        del scope, version
        return OwnerModelSnapshot(version=1, items=())


def test_a_report_without_proposals_still_parses(record_path: Path) -> None:
    report = report_for(record_path)

    assert report.owner_model_proposals == ()


def test_proposals_map_to_entries_with_the_evidence_quote_as_reason(record_path: Path) -> None:
    report = report_for(record_path, owner_model_proposals=[proposal()])

    [entry] = entries_from_proposals(report)

    assert entry.kind == "position"
    assert entry.topic == "beta launch date"
    assert entry.statement == "The beta launches on 14 October, not 21 October."
    assert entry.confidence == "leaning"
    assert entry.disclosure == "owner_only"
    assert entry.reason is not None
    assert "deadline" in entry.reason
    assert "b2-meeting" in entry.reason


def test_merge_drafts_carries_proposals_once() -> None:
    merged = debrief_module.merge_drafts(
        [
            DebriefDraft(items=(), findings=(), owner_model_proposals=(proposal(),)),
            DebriefDraft(items=(), findings=(), owner_model_proposals=(proposal(),)),
        ]
    )

    assert merged.owner_model_proposals == (proposal(),)


async def test_publish_writes_each_new_proposal_once_as_a_debrief_revision(
    record_path: Path,
) -> None:
    report = report_for(record_path, owner_model_proposals=[proposal()])
    repository = _Repository()

    written = await publish_proposals(repository, SCOPE, report, at=AT)
    again = await publish_proposals(
        _Repository(held=tuple(entries_from_proposals(report))), SCOPE, report, at=AT
    )

    assert written == 1
    assert again == 0
    assert len(repository.added) == 1
