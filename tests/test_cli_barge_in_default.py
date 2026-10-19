import sys
from typing import Any

import pytest

import fakeyou.cli.main as cli_main

LINK = "https://meet.google.com/abc-defg-hij"
BARGE_IN_CASES = [
    ([], False),
    (["--converse"], True),
    (["--converse", "--barge-in"], True),
    (["--converse", "--no-barge-in"], False),
]


@pytest.mark.parametrize(("extra", "expected"), BARGE_IN_CASES)
def test_worker_start_barge_in_follows_converse(
    monkeypatch: pytest.MonkeyPatch, *, extra: list[str], expected: bool
) -> None:
    calls: list[dict[str, Any]] = []

    async def record(options: Any, **kwargs: Any) -> None:
        calls.append({"options": options, **kwargs})

    monkeypatch.setattr(cli_main, "run_session", record)
    argv = ["fakeyou", "worker", "start", LINK, "--meeting-id", "meeting-9", "--audio", *extra]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["options"].barge_in is expected


@pytest.mark.parametrize(("extra", "expected"), BARGE_IN_CASES)
def test_join_barge_in_follows_converse(
    monkeypatch: pytest.MonkeyPatch, *, extra: list[str], expected: bool
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_join(options: Any, **kwargs: Any) -> None:
        calls.append({"options": options, **kwargs})

    monkeypatch.setattr(cli_main, "run_session", fake_join)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "join", LINK, *extra])

    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["options"].barge_in is expected
