import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

import fakeyou.cli.diagnostics as cli_diagnostics
import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from tests._error_support import message_pattern


def test_report_command_prints_summary_from_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "meeting-1.jsonl").write_text(
        json.dumps(
            {
                "event": "session_finished",
                "meeting_id": "meeting-1",
                "at": "2026-09-30T10:00:00+00:00",
                "phase": "ended",
                "terminal_reason": "stopped_by_owner",
                "exit_code": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "report", "--meeting-id", "meeting-1", "--journal-dir", str(journal_dir)],
    )

    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["meeting_id"] == "meeting-1"
    assert report["result"]["terminal_reason"] == "stopped_by_owner"


def test_report_command_rejects_an_unknown_meeting(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="no journal") as caught:
        cli_diagnostics.show_session_report(SessionId("missing"), tmp_path, settings)
    assert caught.value.code is ErrorCode.DIAGNOSTICS_JOURNAL_MISSING


def test_report_command_maps_unreadable_journal_to_report_failed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "meeting-1.jsonl").write_text("{}\n", encoding="utf-8")

    def unreadable(_path: Path) -> NoReturn:
        raise OSError("disk gone")

    monkeypatch.setattr(cli_diagnostics, "read_journal_with_corrupt", unreadable)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.DIAGNOSTICS_REPORT_FAILED)
    ) as caught:
        cli_diagnostics.show_session_report(SessionId("meeting-1"), journal_dir, settings)

    assert caught.value.code is ErrorCode.DIAGNOSTICS_REPORT_FAILED
    assert str(caught.value) == "session report failed: OSError"


async def test_browser_tools_command_maps_launch_errors_to_tools_failed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    from playwright.async_api import Error as PlaywrightError

    @asynccontextmanager
    async def failing(*args: Any, **_kwargs: Any) -> Any:
        del args
        raise PlaywrightError("launch blew up")
        yield []

    monkeypatch.setattr(cli_diagnostics, "open_browser_tools", failing)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.BROWSER_TOOLS_FAILED)
    ) as caught:
        await cli_diagnostics.show_browser_tools(settings)

    assert caught.value.code is ErrorCode.BROWSER_TOOLS_FAILED
    assert str(caught.value) == "browser tools failed: Error"


def test_report_command_rejects_an_invalid_meeting_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "report", "--meeting-id", "bad id"])

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == ExitCode.INPUT_REJECTED
    assert "invalid --meeting-id: 'bad id'" in capsys.readouterr().err


def test_report_command_surfaces_corrupt_journal_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "meeting-1.jsonl").write_text(
        json.dumps(
            {
                "event": "session_finished",
                "meeting_id": "meeting-1",
                "at": "2026-09-30T10:00:00+00:00",
                "phase": "ended",
                "terminal_reason": "stopped_by_owner",
                "exit_code": 0,
            }
        )
        + "\nnot json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "report", "--meeting-id", "meeting-1", "--journal-dir", str(journal_dir)],
    )

    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["journal"] == {"corrupt_lines": 1}
