import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fakeyou.browser import ProfileInUseError, profile_lock, profile_lock_path
from fakeyou.cli.errors import CliError
from fakeyou.settings import Settings

LINK = "https://meet.google.com/abc-defg-hij"


@pytest.mark.asyncio
async def test_join_uses_a_per_meeting_guest_profile(
    settings: Settings, join: Any, session_run: Any, tmp_path: Path
) -> None:
    settings.paths.browser_profile_dir = tmp_path / "profiles"

    await join(LINK, twin_id="twin-1", meeting_id="meeting-1")
    first = session_run["config"].browser_profile_dir
    await join(LINK, twin_id="twin-1", meeting_id="meeting-2")
    second = session_run["config"].browser_profile_dir

    assert first == tmp_path / "profiles" / "twin-1" / "meeting-1"
    assert second == tmp_path / "profiles" / "twin-1" / "meeting-2"


@pytest.mark.asyncio
async def test_join_refuses_a_second_worker_on_a_signed_in_profile(
    settings: Settings, join: Any, session_run: Any, tmp_path: Path
) -> None:
    profile = tmp_path / "signed-in"

    with profile_lock(profile):
        with pytest.raises(CliError, match="in use"):
            await join(LINK, meeting_id="meeting-9", profile_dir=str(profile))
    assert "config" not in session_run


@pytest.mark.asyncio
async def test_join_releases_the_signed_in_profile_after_the_session(
    settings: Settings, join: Any, session_run: Any, tmp_path: Path
) -> None:
    profile = tmp_path / "signed-in"

    await join(LINK, meeting_id="meeting-9", profile_dir=str(profile))

    assert session_run["config"].browser_profile_dir == profile
    with profile_lock(profile):
        pass


def test_profile_lock_takes_over_a_stale_foreign_lease(tmp_path: Path) -> None:
    profile = tmp_path / "signed-in"
    path = profile_lock_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": "other-container",
                "pid": 42,
                "purpose": "meeting",
                "token": "stale",
                "heartbeat": datetime(2026, 10, 3, 8, 0, tzinfo=UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with profile_lock(profile, stale_after_seconds=60.0):
        assert json.loads(path.read_text(encoding="utf-8"))["token"] != "stale"
    assert not path.exists()


def test_profile_lock_refuses_a_live_foreign_lease(tmp_path: Path) -> None:
    profile = tmp_path / "signed-in"
    path = profile_lock_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": "other-container",
                "pid": 999999999,
                "purpose": "meeting",
                "token": "foreign",
                "heartbeat": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProfileInUseError, match="in use"):
        with profile_lock(profile, stale_after_seconds=60.0):
            pytest.fail("live foreign lease must not be taken over")
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "foreign"
