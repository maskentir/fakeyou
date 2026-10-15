import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.audio import audio_purge_expired
from fakeyou.config.settings import Settings
from tests._error_support import message_pattern

DAY = 86400.0


def _meeting_audio(root: Path, meeting: str, *, age_days: float, with_sidecar: bool = True) -> int:
    audio_dir = root / meeting / "audio"
    audio_dir.mkdir(parents=True)
    wav = audio_dir / "slot-0.wav"
    wav.write_bytes(b"\x00\x01" * 80)
    if with_sidecar:
        (audio_dir / "slots.json").write_text(json.dumps({"schema": "slot_audio/v1"}))
    stamp = time.time() - age_days * DAY
    for path in audio_dir.rglob("*"):
        os.utime(path, (stamp, stamp))
    os.utime(audio_dir, (stamp, stamp))
    return sum(path.stat().st_size for path in audio_dir.rglob("*") if path.is_file())


def test_purge_expired_removes_old_audio_and_reports_json(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    freed = _meeting_audio(root, "meeting-old", age_days=40.0)
    _meeting_audio(root, "meeting-new", age_days=1.0)

    audio_purge_expired(root=root, days=None, settings=settings)

    report = json.loads(capsys.readouterr().out)
    assert report == {"removed": ["meeting-old"], "kept": 1, "freed_bytes": freed}
    assert not (root / "meeting-old" / "audio").exists()
    assert (root / "meeting-new" / "audio" / "slot-0.wav").exists()


def test_purge_expired_uses_directory_mtime_without_sidecar(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    freed = _meeting_audio(root, "meeting-old", age_days=40.0, with_sidecar=False)

    audio_purge_expired(root=root, days=None, settings=settings)

    report = json.loads(capsys.readouterr().out)
    assert report == {"removed": ["meeting-old"], "kept": 0, "freed_bytes": freed}
    assert not (root / "meeting-old" / "audio").exists()


def test_purge_expired_keeps_young_audio(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    _meeting_audio(root, "meeting-new", age_days=1.0)

    audio_purge_expired(root=root, days=None, settings=settings)

    report = json.loads(capsys.readouterr().out)
    assert report == {"removed": [], "kept": 1, "freed_bytes": 0}
    assert (root / "meeting-new" / "audio").exists()


def test_purge_expired_honors_days_override(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    _meeting_audio(root, "meeting-old", age_days=40.0)

    audio_purge_expired(root=root, days=90, settings=settings)

    report = json.loads(capsys.readouterr().out)
    assert report == {"removed": [], "kept": 1, "freed_bytes": 0}
    assert (root / "meeting-old" / "audio").exists()


def test_purge_expired_defaults_to_settings_root(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    root = settings.paths.record_dir
    freed = _meeting_audio(root, "meeting-old", age_days=40.0)

    audio_purge_expired(root=None, days=None, settings=settings)

    report = json.loads(capsys.readouterr().out)
    assert report == {"removed": ["meeting-old"], "kept": 0, "freed_bytes": freed}


def test_purge_expired_with_missing_root_exits_two(settings: Settings, tmp_path: Path) -> None:
    missing = tmp_path / "no-such-root"

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.AUDIO_ROOT_MISSING)
    ) as outcome:
        audio_purge_expired(root=missing, days=None, settings=settings)

    assert outcome.value.code is ErrorCode.AUDIO_ROOT_MISSING
    assert outcome.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(outcome.value) == f"audio root is missing: {missing}"


def test_purge_expired_maps_filesystem_errors_to_purge_failed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "meetings"
    _meeting_audio(root, "meeting-old", age_days=40.0)

    def boom(*args: Any, **_kwargs: Any) -> None:
        del args
        raise OSError("disk gone")

    monkeypatch.setattr("shutil.rmtree", boom)

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.AUDIO_PURGE_FAILED)) as caught:
        audio_purge_expired(root=root, days=None, settings=settings)

    assert caught.value.code is ErrorCode.AUDIO_PURGE_FAILED
    assert str(caught.value) == "audio purge failed: OSError"


def test_purge_expired_with_non_positive_days_raises(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="positive") as outcome:
        audio_purge_expired(root=tmp_path, days=0, settings=settings)

    assert outcome.value.code is ErrorCode.AUDIO_RETENTION_DAYS_NOT_POSITIVE


def test_audio_purge_expired_dispatches_root_and_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_purge(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "audio_purge_expired", fake_purge)
    monkeypatch.setattr(
        sys, "argv", ["fakeyou", "audio", "purge-expired", "--root", str(tmp_path), "--days", "7"]
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["root"] == tmp_path
    assert call["days"] == 7
    assert set(call) == {"root", "days", "settings"}


def test_audio_commands_dispatch_without_leaking_parser_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_purge(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "audio_purge_expired", fake_purge)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "audio", "purge-expired", "--root", str(tmp_path)])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert set(call) == {"root", "days", "settings"}


def test_visual_purge_command_dispatches_root_and_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_purge(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "visual_purge_expired", fake_purge)
    monkeypatch.setattr(
        sys, "argv", ["fakeyou", "visual", "purge-expired", "--root", str(tmp_path), "--days", "3"]
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["root"] == tmp_path
    assert call["days"] == 3
    assert set(call) == {"root", "days", "settings"}
