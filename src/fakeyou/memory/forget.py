import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import SessionStatusRepository
from fakeyou.memory.memory import forget_episode, forget_meeting
from fakeyou.memory.operator_access import OperatorAccess, open_operator_memory
from fakeyou.memory.persona import PersonaProfile, load_operator_persona
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import journal_files
from fakeyou.persistence.ingestion_status import record_lock
from fakeyou.persistence.layout import meeting_profile_dir
from fakeyou.persistence.record import meeting_record_path

ForgetStage = Literal["graph", "record", "journal", "browser_profile", "status"]
ForgetStageStatus = Literal["removed", "absent", "failed", "kept"]
FILE_STAGES: tuple[ForgetStage, ...] = ("record", "journal", "browser_profile", "status")
STILL_PRESENT = "still_present"


@dataclass(frozen=True, slots=True)
class ForgetRequest:
    meeting_id: SessionId | None
    episode_uuid: str | None
    keep_record: bool = False


@dataclass(frozen=True, slots=True)
class ForgottenFiles:
    record_removed: bool
    journal_files_removed: int
    status_removed: bool
    browser_profile_removed: bool


@dataclass(frozen=True, slots=True)
class ForgetStageResult:
    stage: ForgetStage
    status: ForgetStageStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ForgetFilesOutcome:
    files: ForgottenFiles
    stages: tuple[ForgetStageResult, ...]


@dataclass(frozen=True, slots=True)
class ForgetSummary:
    removed_episodes: int
    files: ForgottenFiles | None = None
    stages: tuple[ForgetStageResult, ...] = ()
    complete: bool = True


def _complete(stages: tuple[ForgetStageResult, ...]) -> bool:
    return all(stage.status != "failed" for stage in stages)


def require_complete(summary: ForgetSummary) -> ForgetSummary:
    failed = next((stage for stage in summary.stages if stage.status == "failed"), None)
    if failed is not None:
        raise FakeYouError(
            ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE,
            path=failed.stage,
            reason=failed.detail or "failed",
        )
    return summary


async def run_forget(
    request: ForgetRequest,
    *,
    access: OperatorAccess,
    status: SessionStatusRepository,
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
        status=status,
    )


async def _forget_graph(
    access: OperatorAccess, profile: PersonaProfile, meeting_id: SessionId
) -> tuple[int, ForgetStageResult]:
    async def sweep() -> int:
        async with access.open_memory() as memory:
            return await forget_meeting(
                memory, group_id=profile.memory_group, meeting_id=meeting_id
            )

    removed, error = await capture_error(sweep)
    if error is not None or removed is None:
        return 0, ForgetStageResult("graph", "failed", type(error).__name__)
    return removed, ForgetStageResult("graph", "removed" if removed else "absent")


async def _forget_meeting(
    meeting_id: SessionId,
    *,
    keep_record: bool,
    access: OperatorAccess,
    status: SessionStatusRepository,
) -> ForgetSummary:
    with record_lock(
        meeting_record_path(access.settings.paths.record_dir, meeting_id), purpose="forget"
    ):
        profile = await load_operator_persona(access.identity, access.settings)
        removed, graph = await _forget_graph(access, profile, meeting_id)
        if keep_record:
            kept = tuple(ForgetStageResult(stage, "kept") for stage in FILE_STAGES)
            return ForgetSummary(removed, None, (graph, *kept), _complete((graph, *kept)))
        outcome = await forget_meeting_files(access.settings, profile, meeting_id, status)
    stages = (graph, *outcome.stages)
    return ForgetSummary(removed, outcome.files, stages, _complete(stages))


async def _forget_episode(episode_uuid: str, *, access: OperatorAccess) -> ForgetSummary:
    async with open_operator_memory(access) as (profile, memory):
        removed = await forget_episode(
            memory, group_id=profile.memory_group, episode_uuid=episode_uuid
        )
    graph = ForgetStageResult("graph", "removed" if removed else "absent")
    return ForgetSummary(removed_episodes=1 if removed else 0, stages=(graph,), complete=True)


async def forget_meeting_files(
    settings: Settings,
    profile: PersonaProfile,
    meeting_id: SessionId,
    status: SessionStatusRepository,
) -> ForgetFilesOutcome:
    record = _remove_tree(
        "record", meeting_record_path(settings.paths.record_dir, meeting_id).parent
    )
    journal, journals_removed = _remove_journals(settings, meeting_id)
    browser = _remove_tree(
        "browser_profile",
        meeting_profile_dir(
            settings.paths.browser_profile_dir, twin_id=profile.twin_id, meeting_id=meeting_id
        ),
    )
    removed_status = await _remove_status(status, meeting_id)
    return ForgetFilesOutcome(
        files=ForgottenFiles(
            record_removed=record.status == "removed",
            journal_files_removed=journals_removed,
            status_removed=removed_status.status == "removed",
            browser_profile_removed=browser.status == "removed",
        ),
        stages=(record, journal, browser, removed_status),
    )


def _remove_tree(stage: ForgetStage, path: Path) -> ForgetStageResult:
    if not path.exists():
        return ForgetStageResult(stage, "absent")
    try:
        shutil.rmtree(path)
    except OSError as error:
        return ForgetStageResult(stage, "failed", type(error).__name__)
    if path.exists():
        return ForgetStageResult(stage, "failed", STILL_PRESENT)
    return ForgetStageResult(stage, "removed")


def _remove_journals(settings: Settings, meeting_id: SessionId) -> tuple[ForgetStageResult, int]:
    present = [
        path for path in journal_files(settings.paths.journal_dir, meeting_id) if path.exists()
    ]
    removed = 0
    failure: str | None = None
    for path in present:
        try:
            path.unlink()
        except OSError as error:
            failure = failure or type(error).__name__
            continue
        removed += 1
    if failure is not None:
        return ForgetStageResult("journal", "failed", failure), removed
    return ForgetStageResult("journal", "removed" if removed else "absent"), removed


async def _remove_status(status: SessionStatusRepository, meeting_id: str) -> ForgetStageResult:
    async def discard() -> bool:
        existed = await status.get(meeting_id) is not None
        await status.discard(meeting_id)
        return existed

    existed, error = await capture_error(discard)
    if error is not None:
        return ForgetStageResult("status", "failed", type(error).__name__)
    return ForgetStageResult("status", "removed" if existed else "absent")
