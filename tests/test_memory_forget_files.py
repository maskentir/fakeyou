import shutil
from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.forget import forget_meeting_files
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.persistence.layout import meeting_profile_dir
from fakeyou.persistence.record import meeting_record_path
from tests._error_support import message_pattern

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

    await forget_meeting_files(settings, profile, MEETING, meeting_thread_id)

    assert not profile_dir.exists()


async def test_forget_meeting_fails_when_the_record_cannot_be_removed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_dir(settings).mkdir(parents=True)

    def busy(path: object, *_args: object, **_kwargs: object) -> None:
        del path
        raise PermissionError("record directory is busy")

    monkeypatch.setattr(shutil, "rmtree", busy)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE)
    ) as caught:
        await forget_meeting_files(settings, _profile(settings), MEETING, meeting_thread_id)

    assert caught.value.code is ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE


async def test_forget_meeting_fails_when_the_record_survives_removal(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_dir(settings).mkdir(parents=True)
    monkeypatch.setattr(shutil, "rmtree", lambda *_args, **_kwargs: None)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE)
    ) as caught:
        await forget_meeting_files(settings, _profile(settings), MEETING, meeting_thread_id)

    assert caught.value.code is ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE
    assert _record_dir(settings).exists()
