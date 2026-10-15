import asyncio
import os
import time
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import fakeyou.orchestration.assembly as assembly_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.audio import visual_purge_expired
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings, VisualSettings
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.visual_retention import PurgeReport
from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
from fakeyou.orchestration.lifecycle import MeetingLifecycle
from fakeyou.orchestration.runtime import MeetingWorker
from fakeyou.visual.visual_store import VisualStore
from tests._error_support import message_pattern
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import FakePage
from tests._persistence_fakes import RecordingJournal

DAY = 86400.0


def aged_file(path: Path, age_days: float, payload: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path.stat().st_size


def visual_dir(root: Path, meeting: str) -> Path:
    directory = root / meeting / "visual"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_purge_removes_expired_png(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    directory = visual_dir(root, "meeting-old")
    freed = aged_file(directory / "shot-old.jpg", 9.0, b"\xff\xd8old-frame" * 64)
    aged_file(directory / "shot-new.jpg", 1.0, b"\xff\xd8new-frame" * 64)
    aged_file(directory / "visuals.jsonl", 1.0, b'{"visual_id":"fresh"}\n')

    report = visual_purge_expired(root=root, days=None, settings=settings)

    assert isinstance(report, PurgeReport)
    assert report.freed_bytes == freed
    assert report.removed == ["meeting-old/shot-old.jpg"]
    assert not (directory / "shot-old.jpg").exists()
    assert (directory / "shot-new.jpg").exists()
    assert (directory / "visuals.jsonl").exists()
    assert "freed_bytes" in capsys.readouterr().out


def test_text_kept_30d(settings: Settings, tmp_path: Path) -> None:
    root = tmp_path / "meetings"
    directory = visual_dir(root, "meeting-text")
    aged_file(directory / "shot-old.jpg", 9.0, b"\xff\xd8old-frame" * 64)
    aged_file(directory / "frame-aaaa.caption.json", 12.0, b'{"text":"stale caption"}')
    aged_file(directory / "frame-bbbb.caption.json", 40.0, b'{"text":"expired caption"}')

    report = visual_purge_expired(root=root, days=None, settings=settings)

    assert (directory / "frame-aaaa.caption.json").exists()
    assert not (directory / "frame-bbbb.caption.json").exists()
    assert not (directory / "shot-old.jpg").exists()
    assert report.kept == 1


def test_visual_purge_maps_filesystem_errors_to_purge_failed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    root = tmp_path / "meetings"
    visual_dir(root, "meeting-old")

    def boom(*args: Any, **_kwargs: Any) -> None:
        del args
        raise OSError("disk gone")

    monkeypatch.setattr("fakeyou.persistence.retention.purge_visual_files", boom)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VISUAL_PURGE_FAILED)
    ) as caught:
        visual_purge_expired(root=root, days=None, settings=settings)

    assert caught.value.code is ErrorCode.VISUAL_PURGE_FAILED
    assert str(caught.value) == "visual purge failed: OSError"


def test_purge_rejects_bad_input(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="positive") as rejected:
        visual_purge_expired(root=tmp_path, days=0, settings=settings)
    assert rejected.value.code is ErrorCode.VISUAL_RETENTION_DAYS_NOT_POSITIVE
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VISUAL_ROOT_MISSING)
    ) as missing:
        visual_purge_expired(root=tmp_path / "no-such-root", days=None, settings=settings)
    assert missing.value.code is ErrorCode.VISUAL_ROOT_MISSING
    assert missing.value.exit_code is ExitCode.INPUT_REJECTED


async def test_flag_off_disables_everything(settings: Settings, tmp_path: Path) -> None:
    assert settings.visual.enabled is False
    assert VisualSettings().enabled is False
    assert assembly_module.create_visual_captioner(settings) is None

    store = VisualStore(tmp_path / "visual", journal=RecordingJournal())
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    clock = FakeClock()
    worker = MeetingWorker(
        SessionConfig(
            owner_id=SessionId("owner-1"),
            twin_id=SessionId("twin-1"),
            meeting_id=SessionId("meeting-1"),
            link="https://meet.example.com/abc-defg-hij",
            twin_name="Twin",
        ),
        lifecycle,
        FakePage(),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        visual_store=store,
    )
    await worker.start_visual_observer()
    assert worker.visual_observer is None


async def test_flag_on_starts_observer(tmp_path: Path) -> None:
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    clock = FakeClock()
    worker = MeetingWorker(
        SessionConfig(
            owner_id=SessionId("owner-1"),
            twin_id=SessionId("twin-1"),
            meeting_id=SessionId("meeting-1"),
            link="https://meet.example.com/abc-defg-hij",
            twin_name="Twin",
            visual_enabled=True,
        ),
        lifecycle,
        FakePage(),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        visual_store=VisualStore(tmp_path / "visual"),
    )
    await worker.start_visual_observer()
    assert worker.visual_observer is not None
    await worker.close_visual_observer()
    assert worker.visual_observer is None
