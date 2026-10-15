import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import conversation_thread_id
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url
from fakeyou.memory.memory import forget_episode, forget_meeting
from fakeyou.memory.operator_access import OperatorAccess, open_operator_memory
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.journal import journal_files
from fakeyou.persistence.ingestion_status import record_lock
from fakeyou.persistence.layout import meeting_profile_dir
from fakeyou.persistence.record import meeting_record_path

LifecycleThreadId = Callable[[str, str], str]


@dataclass(frozen=True, slots=True)
class ForgetRequest:
    meeting_id: SessionId | None
    episode_uuid: str | None
    keep_record: bool = False


@dataclass(frozen=True, slots=True)
class ForgottenFiles:
    record_removed: bool
    journal_files_removed: int
    conversation_thread_removed: bool
    browser_profile_removed: bool


@dataclass(frozen=True, slots=True)
class ForgetSummary:
    removed_episodes: int
    files: ForgottenFiles | None = None


async def run_forget(
    request: ForgetRequest,
    *,
    access: OperatorAccess,
    lifecycle_thread_id: LifecycleThreadId,
) -> ForgetSummary:
    if request.meeting_id is None:
        if request.episode_uuid is None:
            raise FakeYouError(ErrorCode.MEMORY_FORGET_TARGET_REQUIRED)
        if request.keep_record:
            raise FakeYouError(ErrorCode.MEMORY_KEEP_RECORD_REQUIRES_MEETING)
        return await _forget_episode(request.episode_uuid, access=access)
    if request.episode_uuid is not None:
        raise FakeYouError(ErrorCode.MEMORY_FORGET_TARGET_REQUIRED)
    return await _forget_meeting(
        request.meeting_id,
        keep_record=request.keep_record,
        access=access,
        lifecycle_thread_id=lifecycle_thread_id,
    )


async def _forget_meeting(
    meeting_id: SessionId,
    *,
    keep_record: bool,
    access: OperatorAccess,
    lifecycle_thread_id: LifecycleThreadId,
) -> ForgetSummary:
    with record_lock(
        meeting_record_path(access.settings.paths.record_dir, meeting_id), purpose="forget"
    ):
        async with open_operator_memory(access) as (profile, memory):
            removed = await forget_meeting(
                memory, group_id=profile.memory_group, meeting_id=meeting_id
            )
        files = (
            None
            if keep_record
            else await forget_meeting_files(
                access.settings, profile, meeting_id, lifecycle_thread_id
            )
        )
    return ForgetSummary(removed_episodes=removed, files=files)


async def _forget_episode(episode_uuid: str, *, access: OperatorAccess) -> ForgetSummary:
    async with open_operator_memory(access) as (profile, memory):
        await forget_episode(memory, group_id=profile.memory_group, episode_uuid=episode_uuid)
    return ForgetSummary(removed_episodes=1)


async def forget_meeting_files(
    settings: Settings,
    profile: PersonaProfile,
    meeting_id: SessionId,
    lifecycle_thread_id: LifecycleThreadId,
) -> ForgottenFiles:
    record_removed = _remove_tree(meeting_record_path(settings.paths.record_dir, meeting_id).parent)
    removed_journals = [
        path for path in journal_files(settings.paths.journal_dir, meeting_id) if path.exists()
    ]
    for path in removed_journals:
        _remove_file(path)
    profile_removed = _remove_tree(
        meeting_profile_dir(
            settings.paths.browser_profile_dir, twin_id=profile.twin_id, meeting_id=meeting_id
        )
    )
    url = configured_url(settings)
    thread_removed = False
    if url is not None:
        async with open_checkpointer(
            url,
            connect_timeout_seconds=settings.database.connect_timeout_seconds,
            statement_timeout_seconds=settings.database.statement_timeout_seconds,
            keepalive_idle_seconds=settings.database.keepalive_idle_seconds,
        ) as saver:
            for thread_id in (
                conversation_thread_id(profile.owner_id, profile.twin_id, meeting_id),
                lifecycle_thread_id(profile.twin_id, meeting_id),
            ):
                if await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None:
                    thread_removed = True
                await saver.adelete_thread(thread_id)
    return ForgottenFiles(
        record_removed=record_removed,
        journal_files_removed=len(removed_journals),
        conversation_thread_removed=thread_removed,
        browser_profile_removed=profile_removed,
    )


def _remove_tree(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        shutil.rmtree(path)
    except OSError as error:
        raise _files_incomplete(path, type(error).__name__) from error
    if path.exists():
        raise _files_incomplete(path, "still present after removal")
    return True


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError as error:
        raise _files_incomplete(path, type(error).__name__) from error


def _files_incomplete(path: Path, reason: str) -> FakeYouError:
    return FakeYouError(ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE, path=path.name, reason=reason)
