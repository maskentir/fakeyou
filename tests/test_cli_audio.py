import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import fakeyou.cli.main as cli_main
from fakeyou.cli.audio import audio_purge_expired
from fakeyou.cli.errors import CliError
from fakeyou.settings import Settings

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
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = settings.paths.record_dir
    freed = _meeting_audio(root, "meeting-old", age_days=40.0)

    audio_purge_expired(root=None, days=None, settings=settings)

    report = json.loads(capsys.readouterr().out)
    assert report == {"removed": ["meeting-old"], "kept": 0, "freed_bytes": freed}


def test_purge_expired_with_missing_root_exits_two(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such-root"

    with pytest.raises(SystemExit) as outcome:
        audio_purge_expired(root=missing, days=None, settings=settings)

    assert outcome.value.code == 2
    assert str(missing) in capsys.readouterr().err


def test_purge_expired_with_non_positive_days_raises(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(CliError, match="positive"):
        audio_purge_expired(root=tmp_path, days=0, settings=settings)


def test_audio_parsers_map_to_handler(tmp_path: Path) -> None:
    parser = cli_main.build_parser()

    options = parser.parse_args(["audio", "purge-expired"])
    assert options.handler is audio_purge_expired
    assert options.root is None
    assert options.days is None

    options = parser.parse_args(["audio", "purge-expired", "--root", str(tmp_path), "--days", "7"])
    assert options.handler is audio_purge_expired
    assert options.root == tmp_path
    assert options.days == 7


def test_audio_commands_dispatch_without_leaking_parser_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_purge(**kwargs: Any) -> None:
        calls.append(kwargs)

    import sys

    monkeypatch.setattr(cli_main, "audio_purge_expired", fake_purge)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "audio", "purge-expired", "--root", str(tmp_path)])
    cli_main.main()

    [call] = calls
    assert "worker_command" not in call
    assert "voiceprint_command" not in call
    assert "audio_command" not in call
    assert "command" not in call
