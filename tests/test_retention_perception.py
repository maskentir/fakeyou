import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.audio import perception_purge_expired
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.retention import PurgeReport, purge_perception_expired
from tests._error_support import message_pattern

DAY = 86400.0


def aged_file(path: Path, age_days: float, payload: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path.stat().st_size


def meeting_dir(root: Path, meeting: str) -> MeetingDir:
    return MeetingDir(root, SessionId(meeting))


def test_purge_removes_expired_screens_nested_under_the_perception_dir(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    layout = meeting_dir(root, "meeting-old")
    freed = aged_file(layout.screens_dir / "screen-old.jpg", 9.0, b"\xff\xd8old-screen" * 64)
    aged_file(layout.screens_dir / "screen-new.webp", 1.0, b"RIFFnew-screen" * 64)
    aged_file(layout.perception_dir / "readings.jsonl", 1.0, b'{"ref":"screen-new"}\n')

    report = perception_purge_expired(root=root, days=None, settings=settings)

    assert isinstance(report, PurgeReport)
    assert report.removed == ["meeting-old/screens/screen-old.jpg"]
    assert report.kept == 2
    assert report.freed_bytes == freed
    assert not (layout.screens_dir / "screen-old.jpg").exists()
    assert (layout.screens_dir / "screen-new.webp").exists()
    assert (layout.perception_dir / "readings.jsonl").exists()
    assert json.loads(capsys.readouterr().out) == {
        "removed": ["meeting-old/screens/screen-old.jpg"],
        "kept": 2,
        "freed_bytes": freed,
    }


def test_text_outlives_raw_screens_and_unknown_files_are_kept(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "meetings"
    layout = meeting_dir(root, "meeting-text")
    aged_file(layout.screens_dir / "screen-old.PNG", 9.0, b"\x89PNGold" * 64)
    aged_file(layout.perception_dir / "screen-aaaa.json", 12.0, b'{"title":"stale"}')
    aged_file(layout.perception_dir / "chat.txt", 40.0, b"expired chat")
    aged_file(layout.perception_dir / "trace.bin", 400.0, b"\x00" * 8)

    report = purge_perception_expired(root=root, days=None, settings=settings)

    assert report.removed == ["meeting-text/chat.txt", "meeting-text/screens/screen-old.PNG"]
    assert report.kept == 2
    assert (layout.perception_dir / "screen-aaaa.json").exists()
    assert (layout.perception_dir / "trace.bin").exists()


def test_days_override_applies_to_raw_screens_only(settings: Settings, tmp_path: Path) -> None:
    root = tmp_path / "meetings"
    layout = meeting_dir(root, "meeting-1")
    aged_file(layout.screens_dir / "screen-9d.jpeg", 9.0, b"\xff\xd8" * 8)
    aged_file(layout.screens_dir / "screen-25d.png", 25.0, b"\x89PNG" * 8)
    aged_file(layout.perception_dir / "readings.jsonl", 40.0, b"{}\n")

    report = purge_perception_expired(root=root, days=20, settings=settings)

    assert report.removed == ["meeting-1/readings.jsonl", "meeting-1/screens/screen-25d.png"]
    assert (layout.screens_dir / "screen-9d.jpeg").exists()


def test_purge_defaults_to_the_record_dir_and_skips_meetings_without_perception(
    settings: Settings,
) -> None:
    root = settings.paths.record_dir
    seen = meeting_dir(root, "meeting-seen")
    audio_only = meeting_dir(root, "meeting-audio")
    aged_file(seen.screens_dir / "screen.jpg", 30.0, b"\xff\xd8" * 8)
    aged_file(audio_only.audio_dir / "slot-0.wav", 30.0, b"\x00\x01" * 8)

    report = purge_perception_expired(root=None, days=None, settings=settings)

    assert report.removed == ["meeting-seen/screens/screen.jpg"]
    assert report.kept == 0
    assert (audio_only.audio_dir / "slot-0.wav").exists()


def test_purge_maps_filesystem_errors_to_purge_failed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "meetings"
    aged_file(meeting_dir(root, "meeting-old").screens_dir / "screen.jpg", 9.0, b"\xff\xd8")

    def refuse(*args: Any, **_kwargs: Any) -> None:
        del args
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.PERCEPTION_PURGE_FAILED)
    ) as caught:
        purge_perception_expired(root=root, days=None, settings=settings)

    assert caught.value.code is ErrorCode.PERCEPTION_PURGE_FAILED
    assert str(caught.value) == "perception purge failed: OSError"


def test_purge_rejects_bad_input(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="positive") as rejected:
        perception_purge_expired(root=tmp_path, days=0, settings=settings)
    assert rejected.value.code is ErrorCode.PERCEPTION_RETENTION_DAYS_NOT_POSITIVE
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.PERCEPTION_ROOT_MISSING)
    ) as missing:
        perception_purge_expired(root=tmp_path / "no-such-root", days=None, settings=settings)
    assert missing.value.code is ErrorCode.PERCEPTION_ROOT_MISSING
    assert missing.value.exit_code is ExitCode.INPUT_REJECTED
