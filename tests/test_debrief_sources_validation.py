from typing import Any

import pytest

from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import (
    DebriefDraft,
    DebriefItem,
    DebriefValidationError,
    EvidenceRef,
    ObjectiveFinding,
)
from fakeyou.debrief.debrief_sources import SourceEntry, validate_draft, validate_evidence
from fakeyou.persistence.brief import MeetingBrief
from tests._debrief_sources_support import AT, source
from tests._debrief_sources_support import (
    brief as brief,
)
from tests._error_support import message_pattern


def test_exact_quote_keeps_original_whitespace() -> None:
    entry = source(text="Before  Friday\n\tafter.")
    reference = EvidenceRef(entry_id=entry.id, quote="  Friday\n\t")

    validate_evidence((reference,), {entry.id: entry})

    assert reference.quote == "  Friday\n\t"


@pytest.mark.parametrize(
    "reference",
    [
        EvidenceRef(entry_id="transcript:entry-1", quote="The owner approved Monday."),
        EvidenceRef(entry_id="transcript:missing", quote="Please ask"),
        EvidenceRef(entry_id="transcript:entry-1", quote="ask  the owner"),
        EvidenceRef(entry_id="transcript:entry-1", quote=" "),
        EvidenceRef(entry_id="transcript:entry-1", quote="please ask"),
    ],
)
def test_invalid_evidence_is_rejected(reference: EvidenceRef) -> None:
    entry = source()
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_EVIDENCE_INVALID)
    ) as caught:
        validate_evidence((reference,), {entry.id: entry})

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID


def test_missing_evidence_is_rejected() -> None:
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_EVIDENCE_MISSING)
    ) as caught:
        validate_evidence((), {})

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_MISSING


def test_draft_item_references_cannot_escape_the_supplied_chunk() -> None:
    entry = source()
    other = source(2)
    draft = DebriefDraft(
        items=(
            DebriefItem(
                kind="owner_decision",
                text="Ask about the deadline.",
                status="needs_owner",
                evidence=(EvidenceRef(entry_id=other.id, quote="Please ask"),),
            ),
        ),
        findings=(),
    )

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_EVIDENCE_INVALID)
    ) as caught:
        validate_draft(draft, sources={entry.id: entry}, brief=None)

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID
    validate_draft(draft, sources={other.id: other}, brief=None)


def test_draft_accepts_namespaced_objectives_and_questions(brief: MeetingBrief) -> None:
    entry = source()
    reference = EvidenceRef(entry_id=entry.id, quote="Please ask")
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="objective:shared", status="addressed", evidence=(reference,)
            ),
            ObjectiveFinding(objective_id="question:shared", status="open", evidence=()),
        ),
    )

    validate_draft(draft, sources={entry.id: entry}, brief=brief)


def test_addressed_finding_rejects_twin_only_evidence(brief: MeetingBrief) -> None:
    twin = SourceEntry(
        id="twin_speech:2",
        text="I will move the deadline to Monday.",
        kind="twin",
        participant_id="twin",
        at=AT,
    )
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="objective:shared",
                status="addressed",
                evidence=(EvidenceRef(entry_id=twin.id, quote="move the deadline"),),
            ),
        ),
    )

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_ADDRESSED_NEEDS_PARTICIPANT)
    ) as caught:
        validate_draft(draft, sources={twin.id: twin}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_ADDRESSED_NEEDS_PARTICIPANT


def test_addressed_finding_accepts_participant_evidence(brief: MeetingBrief) -> None:
    entry = source()
    twin = SourceEntry(id="twin_speech:2", text="Noted.", kind="twin", participant_id="twin", at=AT)
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="objective:shared",
                status="addressed",
                evidence=(
                    EvidenceRef(entry_id=entry.id, quote="Please ask"),
                    EvidenceRef(entry_id=twin.id, quote="Noted"),
                ),
            ),
        ),
    )

    validate_draft(draft, sources={entry.id: entry, twin.id: twin}, brief=brief)


@pytest.mark.parametrize("finding_id", ["objective:missing", "question:missing", "objective:fact"])
def test_draft_rejects_non_brief_finding_ids(brief: MeetingBrief, finding_id: str) -> None:
    draft = DebriefDraft(
        items=(),
        findings=(ObjectiveFinding(objective_id=finding_id, status="uncertain", evidence=()),),
    )

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF)
    ) as caught:
        validate_draft(draft, sources={}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF


def test_draft_without_brief_must_have_no_findings() -> None:
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(objective_id="objective:shared", status="uncertain", evidence=()),
        ),
    )

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF)
    ) as caught:
        validate_draft(draft, sources={}, brief=None)

    assert caught.value.code is ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF
    validate_draft(DebriefDraft(items=(), findings=()), sources={}, brief=None)


@pytest.mark.parametrize("status", ["addressed", "open", "uncertain"])
def test_finding_evidence_cannot_escape_chunk(brief: MeetingBrief, status: Any) -> None:
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="question:shared",
                status=status,
                evidence=(EvidenceRef(entry_id="transcript:missing", quote="Claim"),),
            ),
        ),
    )

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_EVIDENCE_INVALID)
    ) as caught:
        validate_draft(draft, sources={}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID


def test_draft_status_validation_rejects_unvalidated_model_instances() -> None:
    entry = source()
    reference = EvidenceRef(entry_id=entry.id, quote="Please ask")
    invalid_item = DebriefItem.model_construct(
        kind="owner_decision", text="Ask the owner.", status="stated", evidence=(reference,)
    )
    draft = DebriefDraft.model_construct(items=(invalid_item,), findings=())

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_DRAFT_INVALID)
    ) as caught:
        validate_draft(draft, sources={entry.id: entry}, brief=None)

    assert caught.value.code is ErrorCode.DEBRIEF_DRAFT_INVALID


def test_draft_finding_status_must_be_valid_even_without_evidence(brief: MeetingBrief) -> None:
    invalid = ObjectiveFinding.model_construct(
        objective_id="objective:shared", status="completed", evidence=()
    )
    draft = DebriefDraft.model_construct(items=(), findings=(invalid,))

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_DRAFT_INVALID)
    ) as caught:
        validate_draft(draft, sources={}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_DRAFT_INVALID


def test_evidence_map_cannot_alias_a_different_source_id() -> None:
    entry = source(2)
    reference = EvidenceRef(entry_id="transcript:entry-1", quote="Please ask")

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_EVIDENCE_INVALID)
    ) as caught:
        validate_evidence((reference,), {reference.entry_id: entry})

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID


@pytest.mark.parametrize("placeholder", ["null", "NULL", " none ", "unknown", "n/a", ""])
def test_draft_item_reads_placeholder_claims_as_absent(placeholder: str) -> None:
    item = DebriefItem(
        kind="action_item",
        text="Send the draft.",
        status="stated",
        evidence=(EvidenceRef(entry_id="transcript:t1", quote="Please ask"),),
        assignee_claim=placeholder,
        due_text=placeholder,
    )

    assert (item.assignee_claim, item.due_text) == (None, None)


@pytest.mark.parametrize(
    ("field", "value", "valid"),
    [
        ("assignee_claim", "Rani", False),
        ("due_text", "Friday", False),
        ("due_text", "ASK", True),
        ("assignee_claim", "please", True),
    ],
)
def test_draft_claims_must_come_from_the_item_quotes(
    field: str, value: str, *, valid: bool
) -> None:
    entry = source()
    draft = DebriefDraft(
        items=(
            DebriefItem(
                kind="action_item",
                text="Ask about the deadline.",
                status="stated",
                evidence=(EvidenceRef(entry_id=entry.id, quote="Please ask"),),
                **{field: value},
            ),
        ),
        findings=(),
    )

    if valid:
        validate_draft(draft, sources={entry.id: entry}, brief=None)
    else:
        with pytest.raises(DebriefValidationError, match="claim") as caught:
            validate_draft(draft, sources={entry.id: entry}, brief=None)
        assert caught.value.code is ErrorCode.DEBRIEF_CLAIM_NOT_IN_QUOTES


@pytest.mark.parametrize(
    ("claim", "quotes", "valid"),
    [
        ("Monday.", ("send it Monday",), True),
        ("tomorrow  after lunch", ("send it tomorrow after lunch",), True),
        ("a", ("Please ask",), False),
        ("-", ("follow-up",), False),
        ("Please ask", ("Please", "ask"), False),
        ("ask", ("Please", "ask"), True),
    ],
)
def test_claim_grounding_is_per_quote_on_word_boundaries(
    claim: str, quotes: tuple[str, ...], *, valid: bool
) -> None:
    entry = source(text="Please ask: send it Monday, send it tomorrow after lunch, follow-up")
    draft = DebriefDraft(
        items=(
            DebriefItem(
                kind="action_item",
                text="Ask about the deadline.",
                status="stated",
                evidence=tuple(EvidenceRef(entry_id=entry.id, quote=quote) for quote in quotes),
                due_text=claim,
            ),
        ),
        findings=(),
    )

    if valid:
        validate_draft(draft, sources={entry.id: entry}, brief=None)
    else:
        with pytest.raises(DebriefValidationError, match="claim") as caught:
            validate_draft(draft, sources={entry.id: entry}, brief=None)
        assert caught.value.code is ErrorCode.DEBRIEF_CLAIM_NOT_IN_QUOTES
