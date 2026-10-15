import pytest

import fakeyou.debrief.debrief as module
from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import DebriefDraft, DebriefValidationError, ObjectiveFinding
from tests._debrief_support import item, ref


def test_merge_deduplicates_only_exact_items_and_preserves_conflicts() -> None:
    first = item()
    changed = item(text="A conflicting owner request.")
    paraphrase = item(text="The owner should decide.")
    result = module.merge_drafts(
        [
            DebriefDraft(items=(first,), findings=()),
            DebriefDraft(items=(first, changed, paraphrase), findings=()),
        ]
    )
    assert result.items == (first, changed, paraphrase)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("addressed", "uncertain"), "uncertain"),
        (("open", "addressed"), "addressed"),
        (("uncertain", "open"), "open"),
    ],
)
def test_merge_findings_uses_conservative_priority(
    statuses: tuple[str, str], expected: str
) -> None:
    evidence = ((ref(),), (ref(2),)) if statuses != ("uncertain", "open") else ((), ())
    findings = [
        ObjectiveFinding.model_validate(
            {"objective_id": "objective:o1", "status": state, "evidence": refs}
        )
        for state, refs in zip(statuses, evidence, strict=True)
    ]
    result = module.merge_drafts(
        [DebriefDraft(items=(), findings=(finding,)) for finding in findings]
    )
    assert result.findings[0].status == expected
    assert result.findings[0].evidence == tuple(dict.fromkeys(e for refs in evidence for e in refs))


def test_merge_rejects_more_than_100_distinct_items() -> None:
    drafts = [
        DebriefDraft(items=tuple(item(text=f"Claim {i}") for i in range(100)), findings=()),
        DebriefDraft(items=(item(text="Overflow"),), findings=()),
    ]
    with pytest.raises(DebriefValidationError, match="100") as caught:
        module.merge_drafts(drafts)
    assert caught.value.code is ErrorCode.DEBRIEF_MERGED_TOO_MANY_ITEMS


def test_merge_rejects_evidence_overflow_instead_of_dropping_sources() -> None:
    drafts = [
        DebriefDraft(
            items=(),
            findings=(
                ObjectiveFinding(
                    objective_id="objective:o1",
                    status="addressed",
                    evidence=tuple(ref(i) for i in range(start, start + 6)),
                ),
            ),
        )
        for start in (1, 7)
    ]
    with pytest.raises(DebriefValidationError, match="10") as caught:
        module.merge_drafts(drafts)
    assert caught.value.code is ErrorCode.DEBRIEF_MERGED_FINDING_TOO_MANY_REFS
