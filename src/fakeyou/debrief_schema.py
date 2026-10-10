from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from fakeyou.catalog.errors import FakeYouError
from fakeyou.invariants import SessionId

ABSENT_CLAIMS = frozenset({"", "null", "none", "unknown", "n/a"})


def _absent_as_none(value: object) -> object:
    if isinstance(value, str) and value.strip().casefold() in ABSENT_CLAIMS:
        return None
    return value


ClaimText = Annotated[str | None, BeforeValidator(_absent_as_none)]


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    entry_id: Annotated[str, StringConstraints(min_length=1)]
    quote: Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class DebriefItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["decision_claim", "proposal", "action_item", "open_question", "owner_decision"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    status: Literal["stated", "proposed", "needs_owner", "uncertain"]
    evidence: Annotated[tuple[EvidenceRef, ...], Field(min_length=1, max_length=10)]
    assignee_claim: ClaimText = None
    due_text: ClaimText = None

    @model_validator(mode="after")
    def kind_matches_status(self) -> Self:
        allowed = {
            "decision_claim": {"stated", "needs_owner", "uncertain"},
            "proposal": {"proposed", "needs_owner", "uncertain"},
            "action_item": {"stated", "proposed", "needs_owner", "uncertain"},
            "open_question": {"stated", "needs_owner", "uncertain"},
            "owner_decision": {"needs_owner", "uncertain"},
        }
        if self.status not in allowed[self.kind]:
            raise ValueError("item status is incompatible with kind")
        return self


class ObjectiveFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    objective_id: Annotated[
        str,
        StringConstraints(pattern=r"^(objective|question):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"),
    ]
    status: Literal["addressed", "open", "uncertain"]
    evidence: Annotated[tuple[EvidenceRef, ...], Field(max_length=10)]

    @model_validator(mode="after")
    def addressed_has_evidence(self) -> Self:
        if self.status == "addressed" and not self.evidence:
            raise ValueError("addressed finding requires evidence")
        return self


class DebriefDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    items: Annotated[tuple[DebriefItem, ...], Field(max_length=100)]
    findings: Annotated[tuple[ObjectiveFinding, ...], Field(max_length=40)]


class EvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: Annotated[str, StringConstraints(min_length=1)]
    text: str
    kind: Literal["participant", "twin", "visual"]
    participant_id: str
    at: AwareDatetime


class DebriefReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["meeting_debrief/v1"]
    owner_id: SessionId
    twin_id: SessionId
    meeting_id: SessionId
    record_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    context_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None
    correction_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    generated_at: AwareDatetime
    provider: Annotated[str, StringConstraints(min_length=1)]
    model: Annotated[str, StringConstraints(min_length=1)]
    source_count: Annotated[int, Field(ge=0)]
    chunk_count: Annotated[int, Field(ge=1, le=20)]
    status: Literal["completed"]
    review_status: Literal["unreviewed"]
    items: Annotated[tuple[DebriefItem, ...], Field(max_length=100)]
    findings: Annotated[tuple[ObjectiveFinding, ...], Field(max_length=40)]
    evidence_sources: tuple[EvidenceSource, ...]
    source_limitations: tuple[str, ...]


class DebriefValidationError(FakeYouError, ValueError):
    pass
