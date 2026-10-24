from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionIdField
from fakeyou.config.meeting_request import MeetingRequest, validate_meeting_request
from fakeyou.foundation.digest import canonical_json_digest
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.brief import (
    BriefItem,
    BriefPermission,
    BriefText,
    KnowledgeScope,
    MeetingBrief,
)
from fakeyou.persistence.ingestion_status import IngestionState


def _profile_name_is_path(value: str) -> bool:
    return "/" in value or "\\" in value or ".." in value


class BriefDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["meeting_brief/v1", "meeting_brief/v2"]
    owner_id: SessionIdField
    twin_id: SessionIdField
    agenda: Annotated[tuple[BriefText, ...], Field(max_length=20)] = ()
    roster_hint: Annotated[tuple[BriefText, ...], Field(max_length=20)] = ()
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

    def to_brief(self, meeting_id: str) -> MeetingBrief:
        return MeetingBrief.model_validate({**self.model_dump(), "meeting_id": meeting_id})


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    link: str
    meeting_id: str | None = None
    owner_id: str
    twin_id: str
    twin_name: str | None = None
    lobby_timeout: float | None = None
    duration: float | None = None
    profile_name: str | None = None
    headless: bool | None = None
    audio: bool = False
    initial_speech: str | None = None
    conversation: bool = False
    persona: PersonaProfile | None = None
    brief: BriefDocument | None = None
    memory: bool = False
    wake_phrase: str | None = None
    barge_in: bool = False
    participant_policy_enabled: bool = False

    @model_validator(mode="after")
    def request_combinations_hold(self) -> Self:
        if self.profile_name is not None and _profile_name_is_path(self.profile_name):
            raise FakeYouError(
                ErrorCode.INPUT_INVALID,
                reason="profile_name must be a profile name, not a path",
            )
        validate_meeting_request(
            MeetingRequest(
                link=self.link,
                lobby_timeout=self.lobby_timeout,
                duration=self.duration,
                initial_speech=self.initial_speech,
                audio=self.audio,
                conversation=self.conversation,
                memory=self.memory,
                persona_path=Path("persona") if self.persona is not None else None,
                brief_path=Path("brief") if self.brief is not None else None,
                meeting_id=self.meeting_id,
                wake_phrase=self.wake_phrase,
                barge_in=self.barge_in,
                owner_id=self.owner_id,
                twin_id=self.twin_id,
            )
        )
        return self


class SessionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    link: str
    meeting_id: str
    owner_id: str
    twin_id: str
    twin_name: str | None = None
    lobby_timeout: float | None = None
    duration: float | None = None
    profile_name: str | None = None
    headless: bool | None = None
    audio: bool = False
    initial_speech: str | None = None
    conversation: bool = False
    persona: PersonaProfile | None = None
    brief: MeetingBrief | None = None
    memory: bool = False
    wake_phrase: str | None = None
    barge_in: bool = False
    participant_policy_enabled: bool = False
    keys_supplied: bool = False
    phase: str = "created"
    terminal_reason: str | None = None
    created_at: str
    snapshot_sha256: str


def build_snapshot(
    create: SessionCreate, *, meeting_id: str, at: str, keys_supplied: bool = False
) -> SessionSnapshot:
    brief = create.brief.to_brief(meeting_id) if create.brief is not None else None
    payload = create.model_dump(mode="json")
    payload["meeting_id"] = meeting_id
    payload["keys_supplied"] = keys_supplied
    payload["brief"] = brief.model_dump(mode="json") if brief is not None else None
    payload["created_at"] = at
    digest = canonical_json_digest(payload)
    return SessionSnapshot.model_validate({**payload, "snapshot_sha256": digest})


class SessionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    meeting_id: str
    phase: str = "created"
    snapshot_sha256: str
    owner_id: str
    twin_id: str
    created_at: str
    terminal_reason: str | None = None


class JobView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    job_id: str
    kind: str
    meeting_id: str
    status: str
    completed: bool
    succeeded: bool


class IngestionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    status: IngestionState
    completed: bool
    succeeded: bool


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    text: str
    source_at: AwareDatetime | None = None
    note_id: UUID


class NoteCorrect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    text: str
    source_at: AwareDatetime | None = None
    note_id: UUID


class NoteView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    episode_uuid: str
    facts: tuple[str, ...]
    source_at: str
    recorded_at: str
    supersedes: str | None


class ForgetMeeting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    keep_record: bool = False


class ForgetStageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    stage: str
    status: str
    detail: str | None = None


class ForgetView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    removed_episodes: int
    record_removed: bool
    journal_files_removed: int
    lifecycle_thread_removed: bool
    browser_profile_removed: bool
    stages: tuple[ForgetStageView, ...]
    complete: bool


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    error: ErrorBody
