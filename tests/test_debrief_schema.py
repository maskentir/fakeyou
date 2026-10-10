from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.debrief.debrief_schema import (
    DebriefDraft,
    DebriefItem,
    DebriefReport,
    DebriefValidationError,
    EvidenceRef,
    EvidenceSource,
    ObjectiveFinding,
)


@pytest.fixture
def reference() -> EvidenceRef:
    return EvidenceRef(entry_id="transcript:entry-1", quote="  Friday.  ")


@pytest.fixture
def item(reference: EvidenceRef) -> DebriefItem:
    return DebriefItem(
        kind="decision_claim", text="Friday is stated.", status="stated", evidence=(reference,)
    )


@pytest.fixture
def report_payload(item: DebriefItem) -> dict[str, Any]:
    return {
        "schema_version": "meeting_debrief/v1",
        "owner_id": "schema-owner",
        "twin_id": "schema-twin",
        "meeting_id": "schema-meeting",
        "record_sha256": "a" * 64,
        "context_sha256": None,
        "generated_at": datetime(2026, 10, 3, tzinfo=UTC),
        "provider": "test",
        "model": "test-model",
        "source_count": 1,
        "chunk_count": 1,
        "status": "completed",
        "review_status": "unreviewed",
        "items": (item,),
        "findings": (),
        "evidence_sources": (
            {
                "id": "transcript:entry-1",
                "text": "  Friday.  ",
                "kind": "participant",
                "participant_id": "unknown",
                "at": datetime(2026, 10, 3, tzinfo=UTC),
            },
        ),
        "source_limitations": ("Speaker identity remains unknown.",),
    }


def test_should_preserve_quote_whitespace_and_accept_exact_bounds() -> None:
    assert EvidenceRef(entry_id="transcript:x", quote=" x ").quote == " x "
    assert len(EvidenceRef(entry_id="transcript:x", quote="x" * 1000).quote) == 1000


@pytest.mark.parametrize(
    ("field", "value"), [("entry_id", ""), ("quote", ""), ("quote", "x" * 1001)]
)
def test_should_reject_invalid_reference_fields(field: str, value: str) -> None:
    payload = {"entry_id": "transcript:x", "quote": "Valid"}
    payload[field] = value
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate(payload)


@pytest.mark.parametrize(
    "kind", ["decision_claim", "proposal", "action_item", "open_question", "owner_decision"]
)
@pytest.mark.parametrize("status", ["stated", "proposed", "needs_owner", "uncertain"])
def test_should_enforce_kind_status_compatibility(
    kind: str, status: str, reference: EvidenceRef
) -> None:
    allowed = {
        "decision_claim": {"stated", "needs_owner", "uncertain"},
        "proposal": {"proposed", "needs_owner", "uncertain"},
        "action_item": {"stated", "proposed", "needs_owner", "uncertain"},
        "open_question": {"stated", "needs_owner", "uncertain"},
        "owner_decision": {"needs_owner", "uncertain"},
    }
    payload = {"kind": kind, "text": "Claim", "status": status, "evidence": (reference,)}
    if status in allowed[kind]:
        assert DebriefItem.model_validate(payload).status == status
    else:
        with pytest.raises(ValidationError):
            DebriefItem.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "approval"),
        ("status", "verified"),
        ("text", ""),
        ("text", "x" * 1001),
        ("evidence", ()),
    ],
)
def test_should_reject_invalid_item_fields(field: str, value: object, item: DebriefItem) -> None:
    payload = item.model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        DebriefItem.model_validate(payload)


def test_should_require_one_to_ten_item_evidence_refs(
    reference: EvidenceRef, item: DebriefItem
) -> None:
    payload = item.model_dump()
    payload["evidence"] = (reference,) * 10
    assert len(DebriefItem.model_validate(payload).evidence) == 10
    payload["evidence"] = (reference,) * 11
    with pytest.raises(ValidationError):
        DebriefItem.model_validate(payload)


def test_should_keep_assignee_and_relative_due_text_as_claims(reference: EvidenceRef) -> None:
    value = DebriefItem(
        kind="action_item",
        text="Send draft.",
        status="proposed",
        evidence=(reference,),
        assignee_claim="Alex",
        due_text="next Friday",
    )
    assert value.assignee_claim == "Alex"
    assert value.due_text == "next Friday"
    assert (
        DebriefItem(
            kind="action_item", text="Claim", status="uncertain", evidence=(reference,)
        ).due_text
        is None
    )


@pytest.mark.parametrize("status", ["addressed", "open", "uncertain"])
def test_should_require_evidence_for_addressed_findings_only(
    status: str, reference: EvidenceRef
) -> None:
    payload: dict[str, Any] = {
        "objective_id": "objective:blockers",
        "status": status,
        "evidence": (),
    }
    if status == "addressed":
        with pytest.raises(ValidationError):
            ObjectiveFinding.model_validate(payload)
        payload["evidence"] = (reference,)
    assert ObjectiveFinding.model_validate(payload).status == status


@pytest.mark.parametrize(
    "finding_id", ["objective:blockers", "question:vendor-needs", "objective:" + "a" * 128]
)
def test_should_accept_namespaced_session_ids(finding_id: str) -> None:
    assert (
        ObjectiveFinding(objective_id=finding_id, status="open", evidence=()).objective_id
        == finding_id
    )


@pytest.mark.parametrize(
    "finding_id",
    [
        "blockers",
        "objective:",
        "question:../x",
        "objective:" + "a" * 129,
        "agenda:x",
        "question:x:y",
    ],
)
def test_should_reject_invalid_finding_ids(finding_id: str) -> None:
    with pytest.raises(ValidationError):
        ObjectiveFinding(objective_id=finding_id, status="open", evidence=())


def test_should_reject_unknown_finding_status_and_excessive_evidence(
    reference: EvidenceRef,
) -> None:
    for payload in (
        {"objective_id": "objective:x", "status": "completed", "evidence": ()},
        {"objective_id": "objective:x", "status": "addressed", "evidence": (reference,) * 11},
    ):
        with pytest.raises(ValidationError):
            ObjectiveFinding.model_validate(payload)


def test_should_enforce_draft_and_report_collection_limits(
    item: DebriefItem, report_payload: dict[str, Any]
) -> None:
    finding = ObjectiveFinding(objective_id="question:x", status="uncertain", evidence=())
    values = {"items": (item,) * 100, "findings": (finding,) * 40}
    assert len(DebriefDraft.model_validate(values).items) == 100
    assert len(DebriefReport.model_validate(report_payload | values).findings) == 40
    for field, overflow in (("items", (item,) * 101), ("findings", (finding,) * 41)):
        with pytest.raises(ValidationError):
            DebriefDraft.model_validate(values | {field: overflow})
        with pytest.raises(ValidationError):
            DebriefReport.model_validate(report_payload | {field: overflow})
    assert DebriefDraft(items=(), findings=()).items == ()


@pytest.mark.parametrize("field", ["owner_id", "twin_id", "meeting_id"])
@pytest.mark.parametrize("value", ["", "../unsafe", "x" * 129])
def test_should_reject_invalid_report_scope_ids(
    field: str, value: str, report_payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        DebriefReport.model_validate(report_payload | {field: value})


@pytest.mark.parametrize("field", ["record_sha256", "context_sha256", "correction_sha256"])
@pytest.mark.parametrize("value", ["", "a" * 63, "A" * 64, "g" * 64])
def test_should_reject_invalid_hashes(
    field: str, value: str, report_payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        DebriefReport.model_validate(report_payload | {field: value})


def test_should_accept_optional_context_hash_and_json_roundtrip(
    report_payload: dict[str, Any],
) -> None:
    report = DebriefReport.model_validate(report_payload)
    assert report.context_sha256 is None
    assert DebriefReport.model_validate_json(report.model_dump_json()) == report
    assert (
        DebriefReport.model_validate(report_payload | {"context_sha256": "b" * 64}).context_sha256
        == "b" * 64
    )
    assert report.evidence_sources[0].participant_id == "unknown"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "meeting_debrief/v2"),
        ("status", "failed"),
        ("review_status", "verified"),
        ("source_count", -1),
        ("chunk_count", 0),
        ("chunk_count", 21),
        ("generated_at", datetime(2026, 10, 3)),
        ("provider", ""),
        ("model", ""),
    ],
)
def test_should_reject_invalid_report_metadata(
    field: str, value: object, report_payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        DebriefReport.model_validate(report_payload | {field: value})


@pytest.mark.parametrize("field", ["schema_version", "status", "review_status", "context_sha256"])
def test_should_require_explicit_report_markers(field: str, report_payload: dict[str, Any]) -> None:
    del report_payload[field]
    with pytest.raises(ValidationError):
        DebriefReport.model_validate(report_payload)


@pytest.mark.parametrize(
    ("field", "value"), [("kind", "tool"), ("at", datetime(2026, 10, 3)), ("id", "")]
)
def test_should_reject_invalid_evidence_source_metadata(
    field: str, value: object, report_payload: dict[str, Any]
) -> None:
    source = report_payload["evidence_sources"][0]
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate(source | {field: value})


def test_should_preserve_twin_source_metadata(report_payload: dict[str, Any]) -> None:
    source = report_payload["evidence_sources"][0] | {
        "id": "twin_speech:3",
        "kind": "twin",
        "participant_id": "schema-twin",
    }
    value = EvidenceSource.model_validate(source)
    assert value.id == "twin_speech:3"
    assert value.kind == "twin"
    assert value.at.utcoffset() is not None


@pytest.mark.parametrize("model_index", range(6))
def test_should_freeze_models_forbid_extras_and_hide_error_input(
    model_index: int, reference: EvidenceRef, item: DebriefItem, report_payload: dict[str, Any]
) -> None:
    models: tuple[BaseModel, ...] = (
        reference,
        item,
        ObjectiveFinding(objective_id="question:x", status="open", evidence=()),
        DebriefDraft(items=(item,), findings=()),
        DebriefReport.model_validate(report_payload),
        EvidenceSource.model_validate(report_payload["evidence_sources"][0]),
    )
    value = models[model_index]
    name = next(iter(type(value).model_fields))
    with pytest.raises(ValidationError):
        setattr(value, name, getattr(value, name))
    with pytest.raises(ValidationError) as error:
        type(value).model_validate(value.model_dump() | {"private_input": "secret marker"})
    assert "secret marker" not in str(error.value)


def test_should_expose_validation_error_as_value_error() -> None:
    error = DebriefValidationError(ErrorCode.DEBRIEF_EVIDENCE_INVALID)

    assert isinstance(error, FakeYouError)
    assert isinstance(error, ValueError)
    assert error.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID
    assert str(error) == "invalid evidence reference"


def test_should_default_correction_digest_for_legacy_reports(
    report_payload: dict[str, Any],
) -> None:
    report = DebriefReport.model_validate(report_payload)
    assert report.correction_sha256 is None
    assert (
        DebriefReport.model_validate(report_payload | {"correction_sha256": "c" * 64})
    ).correction_sha256 == "c" * 64
