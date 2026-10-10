import json

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.output import emit_error, emit_json, emit_progress, emit_text


def test_emit_json_uses_indent_two_without_ascii_escaping(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_json({"name": "twin-ört", "count": 2})

    captured = capsys.readouterr()
    assert captured.out == '{\n  "name": "twin-ört",\n  "count": 2\n}\n'
    assert captured.err == ""
    assert json.loads(captured.out) == {"name": "twin-ört", "count": 2}


def test_emit_text_writes_a_single_stdout_line(capsys: pytest.CaptureFixture[str]) -> None:
    emit_text("status: ok")

    captured = capsys.readouterr()
    assert captured.out == "status: ok\n"
    assert captured.err == ""


def test_emit_progress_writes_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    emit_progress("fakeyou: note-id operation")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "fakeyou: note-id operation\n"


def test_emit_error_renders_the_catalog_message(capsys: pytest.CaptureFixture[str]) -> None:
    emit_error(FakeYouError(ErrorCode.CONFIG_INVALID, reason="memory.global: down"))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "fakeyou: error: invalid configuration: memory.global: down\n"
