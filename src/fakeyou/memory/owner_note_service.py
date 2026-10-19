from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from graphiti_core import Graphiti

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.operator_access import OperatorAccess, open_operator_memory
from fakeyou.memory.owner_notes import (
    NoteConflictError,
    NoteIdentity,
    OwnerNoteResult,
    forget_owner_note,
    remember_owner_note,
)


@dataclass(frozen=True, slots=True)
class NoteRequest:
    text: str
    source_at: datetime | None
    note_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class NoteCommand:
    text: str
    source_at: datetime | None
    note_id: UUID
    supersedes: UUID | None = None


@dataclass(frozen=True, slots=True)
class NoteConflict:
    code: str
    episode_uuid: str
    retryable: bool


def note_conflict(error: NoteConflictError, *, episode_uuid: str) -> NoteConflict:
    return NoteConflict(
        code=error.code.value,
        episode_uuid=episode_uuid,
        retryable=error.code is ErrorCode.NOTE_CORRECTION_CONCURRENT,
    )


async def apply_note(memory: Graphiti, *, group_id: str, command: NoteCommand) -> OwnerNoteResult:
    _require_text(command.text)
    _require_usable_source(command.source_at)
    return await remember_owner_note(
        memory,
        group_id=group_id,
        text=command.text,
        source_at=command.source_at,
        identity=NoteIdentity(command.note_id, command.supersedes),
    )


async def apply_note_for(access: OperatorAccess, command: NoteCommand) -> OwnerNoteResult:
    _require_text(command.text)
    _require_usable_source(command.source_at)
    async with open_operator_memory(access) as (profile, memory):
        return await apply_note(memory, group_id=profile.memory_group, command=command)


async def forget_note_for(access: OperatorAccess, episode_uuid: str) -> None:
    async with open_operator_memory(access) as (profile, memory):
        await forget_owner_note(memory, group_id=profile.memory_group, episode_uuid=episode_uuid)


async def add_note(
    request: NoteRequest,
    *,
    access: OperatorAccess,
    announce: Callable[[UUID], None],
) -> OwnerNoteResult:
    _require_text(request.text)
    _require_usable_source(request.source_at)
    return await _remember(request, access=access, announce=announce, supersedes=None)


async def correct_note(
    request: NoteRequest,
    *,
    supersedes: str,
    access: OperatorAccess,
    announce: Callable[[UUID], None],
) -> OwnerNoteResult:
    _require_text(request.text)
    try:
        target = UUID(supersedes)
    except ValueError:
        raise FakeYouError(ErrorCode.MEMORY_EPISODE_NOT_UUID) from None
    _require_usable_source(request.source_at)
    return await _remember(request, access=access, announce=announce, supersedes=target)


async def _remember(
    request: NoteRequest,
    *,
    access: OperatorAccess,
    announce: Callable[[UUID], None],
    supersedes: UUID | None,
) -> OwnerNoteResult:
    async with open_operator_memory(access) as (profile, memory):
        operation = request.note_id if request.note_id is not None else uuid4()
        if request.note_id is None:
            announce(operation)
        return await apply_note(
            memory,
            group_id=profile.memory_group,
            command=NoteCommand(request.text, request.source_at, operation, supersedes),
        )


def _require_text(text: str) -> None:
    if not text.strip():
        raise FakeYouError(ErrorCode.MEMORY_NOTE_TEXT_BLANK)


def _require_usable_source(source_at: datetime | None) -> None:
    if source_at is None:
        return
    if source_at.tzinfo is None or source_at.utcoffset() is None:
        raise FakeYouError(ErrorCode.MEMORY_SOURCE_AT_NAIVE)
    if source_at > datetime.now(UTC):
        raise FakeYouError(ErrorCode.MEMORY_SOURCE_AT_FUTURE)
