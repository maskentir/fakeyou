import json
import sys
from pathlib import Path

import pytest

import fakeyou.cli.diagnostics as cli_diagnostics
import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError


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


def test_report_command_rejects_an_unknown_meeting(settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="no journal") as caught:
        cli_diagnostics.show_session_report("missing", tmp_path, settings)
    assert caught.value.code is ErrorCode.DIAGNOSTICS_JOURNAL_MISSING


def test_report_command_rejects_an_invalid_meeting_id(settings, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pattern"):
        cli_diagnostics.show_session_report("bad id", tmp_path, settings)
