import sys
from typing import Any

import pytest

import fakeyou.cli.main as cli_main


def _dispatch(
    monkeypatch: pytest.MonkeyPatch, handler: str, argv: list[str]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def record(*args: Any, **kwargs: Any) -> None:
        calls.append({**kwargs, **({"options": args[0]} if args else {})})

    monkeypatch.setattr(cli_main, handler, record)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()
    assert exited.value.code == 0
    return calls


def test_worker_start_is_a_detached_api_session(monkeypatch: pytest.MonkeyPatch) -> None:
    [call] = _dispatch(
        monkeypatch,
        "run_session",
        ["worker", "start", "https://meet.google.com/abc-defg-hij", "--meeting-id", "m-1"],
    )

    assert call["detach"] is True
    assert call["options"].meeting_id == "m-1"
    assert call["options"].link == "https://meet.google.com/abc-defg-hij"


def test_worker_status_without_an_id_lists_every_session(monkeypatch: pytest.MonkeyPatch) -> None:
    [call] = _dispatch(monkeypatch, "worker_status", ["worker", "status"])

    assert call["meeting_id"] is None


def test_worker_status_with_an_id_reads_that_session(monkeypatch: pytest.MonkeyPatch) -> None:
    [call] = _dispatch(monkeypatch, "worker_status", ["worker", "status", "meeting-9"])

    assert call["meeting_id"] == "meeting-9"


def test_worker_stop_passes_the_meeting_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    [call] = _dispatch(
        monkeypatch, "worker_stop", ["worker", "stop", "meeting-9", "--timeout", "7"]
    )

    assert call["meeting_id"] == "meeting-9"
    assert call["limit_seconds"] == 7.0


def test_worker_subcommands_reject_hostile_meeting_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", "worker", "stop", "../../x"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 2


def test_worker_ingest_dispatches_the_meeting_to_the_api_job_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_ingest(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "ingest_recorded_meeting", fake_ingest)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "worker", "ingest", "meeting-9"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["meeting_id"] == "meeting-9"
