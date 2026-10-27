import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg import OperationalError
from pydantic import TypeAdapter

from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.entry import main as entry_main
from fakeyou.cli.main import main


@pytest.fixture(autouse=True)
def _restore_process_environment() -> Iterator[None]:
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def _scrub_process_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [key for key in os.environ if key.startswith("FAKEYOU_")]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)


def test_invalid_configuration_reports_config_invalid_with_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("FAKEYOU_MEMORY__RECALL__RESULT_LIMIT=0\n")
    monkeypatch.chdir(tmp_path)
    _scrub_process_configuration(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "db", "status"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == ExitCode.INPUT_REJECTED
    err = capsys.readouterr().err
    assert "fakeyou: error: invalid configuration: memory.recall.result_limit" in err


def test_unknown_configuration_key_reports_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("FAKEYOU_NOPE__NOPE=1\n")
    monkeypatch.chdir(tmp_path)
    _scrub_process_configuration(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "db", "status"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == ExitCode.INPUT_REJECTED
    assert "Unknown configuration key: FAKEYOU_NOPE__NOPE" in capsys.readouterr().err


def test_database_driver_error_reports_unreachable_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("")
    monkeypatch.chdir(tmp_path)
    _scrub_process_configuration(monkeypatch)

    def handler(*args: Any, **_kwargs: Any) -> None:
        del args
        raise OperationalError("connection reset by peer")

    monkeypatch.setattr("fakeyou.cli.main.show_session_report", handler)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "report", "--meeting-id", "meeting-1"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == ExitCode.INFRA_FAILURE
    err = capsys.readouterr().err
    assert "unreachable" in err
    assert "connection reset" not in err


def test_handler_validation_error_reports_input_invalid_with_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("")
    monkeypatch.chdir(tmp_path)
    _scrub_process_configuration(monkeypatch)

    def handler(*args: Any, **_kwargs: Any) -> None:
        del args
        TypeAdapter(int).validate_python("not a number")

    monkeypatch.setattr("fakeyou.cli.main.show_session_report", handler)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "report", "--meeting-id", "meeting-1"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == ExitCode.INPUT_REJECTED
    assert "fakeyou: error: invalid input: " in capsys.readouterr().err


def test_entry_exports_dotenv_and_the_embedding_dimension_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("FAKEYOU_MEMORY__EMBEDDER__DIM=384\n")
    monkeypatch.chdir(tmp_path)
    _scrub_process_configuration(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "--help"])
    with pytest.raises(SystemExit) as caught:
        entry_main()
    assert caught.value.code == 0
    assert os.environ["EMBEDDING_DIM"] == "384"
    capsys.readouterr()


def test_entry_creates_new_files_and_directories_for_the_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _scrub_process_configuration(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "--help"])
    previous = os.umask(0o022)
    try:
        with pytest.raises(SystemExit):
            entry_main()
        effective = os.umask(previous)
    finally:
        os.umask(previous)
    capsys.readouterr()
    assert effective == 0o077
