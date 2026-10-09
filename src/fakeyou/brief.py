import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from fakeyou.ingestion_status import write_atomically
from fakeyou.invariants import SessionId

BriefPermission = Literal[
    "explain_facts", "discuss_options", "ask_brief_questions", "meet_act", "meet_chat"
]
KnowledgeScope = Literal["brief_only", "persona_memory"]
BriefText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

CONTEXT_FILE_NAME = "meeting-context.json"
MAX_BRIEF_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024


class BriefItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: SessionId
    text: BriefText


class MeetingBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["meeting_brief/v1"]
    owner_id: SessionId
    twin_id: SessionId
    meeting_id: SessionId
    agenda: Annotated[tuple[BriefText, ...], Field(max_length=20)] = ()
    objectives: Annotated[tuple[BriefItem, ...], Field(max_length=20)] = ()
    approved_facts: Annotated[tuple[BriefItem, ...], Field(max_length=30)] = ()
    questions: Annotated[tuple[BriefItem, ...], Field(max_length=20)] = ()
    permissions: Annotated[
        frozenset[BriefPermission],
        PlainSerializer(sorted, return_type=list[BriefPermission], when_used="json"),
    ] = frozenset()
    knowledge_scope: KnowledgeScope = "brief_only"

    @model_validator(mode="after")
    def item_ids_are_unique(self) -> Self:
        collections = {
            "objectives": self.objectives,
            "approved_facts": self.approved_facts,
            "questions": self.questions,
        }
        for name, collection in collections.items():
            ids = [item.id for item in collection]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} item ids must be unique")
        return self


def brief_digest(brief: MeetingBrief) -> str:
    payload = brief.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MeetingContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["meeting_context/v1"]
    brief: MeetingBrief
    brief_sha256: Sha256Hex
    persona_sha256: Sha256Hex

    @model_validator(mode="after")
    def brief_matches_its_digest(self) -> Self:
        if brief_digest(self.brief) != self.brief_sha256:
            raise ValueError("brief_sha256 does not match the brief")
        return self


def validate_brief_scope(
    brief: MeetingBrief, *, owner_id: str, twin_id: str, meeting_id: str
) -> None:
    if (brief.owner_id, brief.twin_id, brief.meeting_id) != (owner_id, twin_id, meeting_id):
        raise ValueError("meeting brief owner_id, twin_id, and meeting_id must match the session")


def load_meeting_brief(path: Path, *, owner_id: str, twin_id: str, meeting_id: str) -> MeetingBrief:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BRIEF_BYTES + 1)
    if len(raw) > MAX_BRIEF_BYTES:
        raise ValueError(f"meeting brief must be at most {MAX_BRIEF_BYTES} bytes")
    brief = MeetingBrief.model_validate_json(raw)
    validate_brief_scope(brief, owner_id=owner_id, twin_id=twin_id, meeting_id=meeting_id)
    return brief


def context_path(record_path: Path) -> Path:
    return record_path.parent / CONTEXT_FILE_NAME


def read_meeting_context(path: Path) -> MeetingContext | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONTEXT_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError(f"meeting context must be at most {MAX_CONTEXT_BYTES} bytes")
    return MeetingContext.model_validate_json(raw)


def write_meeting_context(path: Path, context: MeetingContext) -> None:
    existing = read_meeting_context(path)
    if existing == context:
        return
    if existing is not None:
        raise ValueError("meeting context differs from the recorded snapshot")
    write_atomically(path, (context.model_dump_json(indent=2) + "\n").encode("utf-8"))
