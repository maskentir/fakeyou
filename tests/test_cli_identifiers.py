import os
import sys
from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode, render_error
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.main import main

LINK = "https://meet.google.com/abc-defg-hij"
PERSONA = "persona.json"


@pytest.fixture
def clean_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in [key for key in os.environ if key.startswith("FAKEYOU_")]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _tree(root: Path) -> list[Path]:
    return sorted(root.rglob("*"))


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (["diarize", "run", "--meeting-id", "../../x"], "--meeting-id", "../../x"),
            id="diarize-run",
        ),
        pytest.param(
            (["diarize", "show", "--meeting-id", "../../x"], "--meeting-id", "../../x"),
            id="diarize-show",
        ),
        pytest.param(
            (["report", "--meeting-id", "../../x"], "--meeting-id", "../../x"), id="report"
        ),
        pytest.param(
            (
                ["debrief", "--meeting-id", "../../x"],
                "--meeting-id",
                "../../x",
            ),
            id="debrief",
        ),
        pytest.param(
            (
                ["memory-ingest", "--meeting-id", "../../x"],
                "--meeting-id",
                "../../x",
            ),
            id="memory-ingest",
        ),
        pytest.param(
            (
                ["memory-forget", "--meeting-id", "../../x", "--persona", PERSONA],
                "--meeting-id",
                "../../x",
            ),
            id="memory-forget",
        ),
        pytest.param((["join", LINK, "--meeting-id", "../x"], "--meeting-id", "../x"), id="join"),
        pytest.param(
            (["join", LINK, "--owner-id", "a/b"], "--owner-id", "a/b"), id="join-owner-slash"
        ),
        pytest.param(
            (["join", LINK, "--twin-id", "../x"], "--twin-id", "../x"), id="join-twin-traversal"
        ),
        pytest.param(
            (
                ["memory-add", "note", "--persona", PERSONA, "--twin-id", "../x"],
                "--twin-id",
                "../x",
            ),
            id="memory-add-twin",
        ),
        pytest.param(
            (["worker", "start", LINK, "--meeting-id", "../x"], "--meeting-id", "../x"),
            id="worker-start",
        ),
        pytest.param(
            (["worker", "status", "../../x"], "meeting_id", "../../x"), id="worker-status"
        ),
        pytest.param((["worker", "stop", "../../x"], "meeting_id", "../../x"), id="worker-stop"),
        pytest.param(
            (["worker", "ingest", "../../x"], "meeting_id", "../../x"),
            id="worker-ingest",
        ),
    ],
)
def test_hostile_identifiers_are_rejected_with_the_catalog_code_before_any_work(
    clean_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[list[str], str, str],
) -> None:
    argv, field, value = case
    before = _tree(clean_cli)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == ExitCode.INPUT_REJECTED
    assert render_error(ErrorCode.SESSION_ID_INVALID, field=field, value=value) in (
        capsys.readouterr().err
    )
    assert _tree(clean_cli) == before
