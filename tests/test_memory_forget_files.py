import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.forget import ForgetStageResult, forget_meeting_files
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.layout import meeting_profile_dir
from fakeyou.persistence.record import meeting_record_path
from tests._session_status_fakes import MemorySessionStatus

MEETING = SessionId("meeting-forget")


def _profile(settings: Settings) -> PersonaProfile:
    return PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Settings Twin",
        role="Project coordinator",
        allowed_tools=frozenset(),
    )


def _record_dir(settings: Settings) -> Path:
    return meeting_record_path(settings.paths.record_dir, MEETING).parent


async def test_forget_meeting_removes_the_meeting_browser_profile(settings: Settings) -> None:
    profile = _profile(settings)
    profile_dir = meeting_profile_dir(
        settings.paths.browser_profile_dir, twin_id=profile.twin_id, meeting_id=MEETING
    )
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("session", encoding="utf-8")

    await forget_meeting_files(settings, profile, MEETING, MemorySessionStatus())

    assert not profile_dir.exists()


async def test_forget_meeting_reports_a_failed_record_stage_without_raising(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_dir(settings).mkdir(parents=True)

    def busy(path: object, *_args: object, **_kwargs: object) -> None:
        del path
        raise PermissionError("record directory is busy")

    monkeypatch.setattr(shutil, "rmtree", busy)

    outcome = await forget_meeting_files(
        settings, _profile(settings), MEETING, MemorySessionStatus()
    )

    record = _stage(outcome.stages, "record")
    assert (record.status, record.detail) == ("failed", "PermissionError")
    assert outcome.files.record_removed is False
    assert _record_dir(settings).exists()


async def test_forget_meeting_reports_a_record_that_survives_removal_as_failed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_dir(settings).mkdir(parents=True)
    monkeypatch.setattr(shutil, "rmtree", lambda *_args, **_kwargs: None)

    outcome = await forget_meeting_files(
        settings, _profile(settings), MEETING, MemorySessionStatus()
    )

    record = _stage(outcome.stages, "record")
    assert (record.status, record.detail) == ("failed", "still_present")
    assert outcome.files.record_removed is False
    assert _record_dir(settings).exists()


def _stage(stages: tuple[ForgetStageResult, ...], name: str) -> ForgetStageResult:
    [found] = [stage for stage in stages if stage.stage == name]
    return found


async def test_forget_files_reports_each_stage_and_reruns_idempotently(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(settings)
    profile_dir = meeting_profile_dir(
        settings.paths.browser_profile_dir, twin_id=profile.twin_id, meeting_id=MEETING
    )
    _record_dir(settings).mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path: object, *_args: object, **_kwargs: object) -> None:
        if Path(str(path)) == profile_dir:
            raise PermissionError("profile busy")
        real_rmtree(str(path))

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)
    first = await forget_meeting_files(settings, profile, MEETING, MemorySessionStatus())
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    second = await forget_meeting_files(settings, profile, MEETING, MemorySessionStatus())

    assert [stage.stage for stage in first.stages] == [
        "record",
        "journal",
        "browser_profile",
        "status",
    ]
    assert {stage.stage: stage.status for stage in first.stages} == {
        "record": "removed",
        "journal": "absent",
        "browser_profile": "failed",
        "status": "absent",
    }
    assert first.files.record_removed is True
    assert {stage.stage: stage.status for stage in second.stages}["browser_profile"] == "removed"
    assert {stage.stage: stage.status for stage in second.stages}["record"] == "absent"
    assert all(str(settings.paths.record_dir) not in (stage.detail or "") for stage in first.stages)


async def test_forget_files_removes_every_journal_it_can_and_counts_them(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.paths.journal_dir.mkdir()
    for name in ("meeting-forget.jsonl", "meeting-forget.jsonl.1", "meeting-forget.jsonl.2"):
        (settings.paths.journal_dir / name).write_text("{}\n", encoding="utf-8")
    real_unlink = Path.unlink

    def flaky_unlink(path: Path, *_args: object, **_kwargs: object) -> None:
        if path.name == "meeting-forget.jsonl.1":
            raise PermissionError("journal busy")
        real_unlink(path)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    outcome = await forget_meeting_files(
        settings, _profile(settings), MEETING, MemorySessionStatus()
    )

    journal = _stage(outcome.stages, "journal")
    assert (journal.status, journal.detail) == ("failed", "PermissionError")
    assert outcome.files.journal_files_removed == 2
    assert [path.name for path in settings.paths.journal_dir.iterdir()] == [
        "meeting-forget.jsonl.1"
    ]


async def test_forget_files_deletes_the_session_status_row(settings: Settings) -> None:
    status = MemorySessionStatus()
    await status.create(MEETING, "owner-1", datetime(2026, 10, 31, tzinfo=UTC))

    outcome = await forget_meeting_files(settings, _profile(settings), MEETING, status)

    assert _stage(outcome.stages, "status").status == "removed"
    assert outcome.files.status_removed is True
    assert await status.get(MEETING) is None
