from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.operator_access import OperatorAccess, open_operator_memory
from fakeyou.memory.owner_notes import NoteIdentity, OwnerNoteResult, remember_owner_note


@dataclass(frozen=True, slots=True)
class NoteRequest:
    text: str
    source_at: datetime | None
    note_id: UUID | None = None


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
        return await remember_owner_note(
            memory,
            group_id=profile.memory_group,
            text=request.text,
            source_at=request.source_at,
            identity=NoteIdentity(operation, supersedes),
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
