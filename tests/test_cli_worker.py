import json
import sys
from pathlib import Path
from typing import Any

import pytest

import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.worker import worker_start, worker_status, worker_stop
from fakeyou.config.settings import Settings
from fakeyou.worker.worker_backend import WorkerStarted, WorkerState, WorkerStopped

LINK = "https://meet.google.com/abc-defg-hij"


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start(self, request: Any) -> WorkerStarted:
        self.calls.append(("start", {"request": request}))
        return WorkerStarted(
            meeting_id=request.meeting_id,
            container_id="abc123",
            container_name=f"fakeyou-worker-{request.meeting_id}",
        )

    def status(self, meeting_id: str) -> WorkerState:
        self.calls.append(("status", {"meeting_id": meeting_id}))
        return WorkerState(
            meeting_id=meeting_id,
            container_id="abc123",
            container_name=f"fakeyou-worker-{meeting_id}",
            container_state="running",
            phase="conversation",
            terminal_reason=None,
            last_fault=None,
        )

    def status_all(self) -> list[WorkerState]:
        self.calls.append(("status_all", {}))
        return []

    def stop(self, meeting_id: str, **kwargs: Any) -> WorkerStopped:
        self.calls.append(("stop", {"meeting_id": meeting_id, **kwargs}))
        return WorkerStopped(
            meeting_id=meeting_id,
            container_id="abc123",
            terminal_reason="stopped_by_owner",
            escalated=False,
        )

    def ingest(self, meeting_id: str, **kwargs: Any) -> Any:
        from fakeyou.worker.worker_backend import WorkerIngest

        self.calls.append(("ingest", {"meeting_id": meeting_id, **kwargs}))
        return WorkerIngest(meeting_id=meeting_id, status="completed")


def start_arguments(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "link": LINK,
        "twin_name": None,
        "owner_id": None,
        "twin_id": "twin-1",
        "meeting_id": "meeting-9",
        "lobby_timeout": None,
        "duration": None,
        "profile_dir": None,
        "headless": None,
        "audio_enabled": True,
        "initial_speech": None,
        "conversation_enabled": True,
        "persona_path": None,
        "brief_path": None,
        "memory": False,
        "wake_phrase": None,
        "barge_in": False,
    }
    values.update(overrides)
    return values


def test_worker_start_maps_arguments_and_prints_ids(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()

    worker_start(**start_arguments(), settings=settings, backend=backend)

    [call] = backend.calls
    assert call[0] == "start"
    assert call[1]["request"].meeting_id == "meeting-9"
    assert call[1]["request"].link == LINK
    assert json.loads(capsys.readouterr().out) == {
        "meeting_id": "meeting-9",
        "container_id": "abc123",
        "container_name": "fakeyou-worker-meeting-9",
    }


def test_worker_start_requires_the_postgres_database_url(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKEYOU_COMPOSE_WORKER_DATABASE_URL", raising=False)

    with pytest.raises(FakeYouError, match="FAKEYOU_COMPOSE_WORKER_DATABASE_URL") as caught:
        worker_start(**start_arguments(), settings=settings)

    assert caught.value.code is ErrorCode.CONFIG_WORKER_DATABASE_URL_MISSING


def test_worker_status_prints_one_meeting_or_all(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()

    worker_status(meeting_id="meeting-9", settings=settings, backend=backend)
    single = json.loads(capsys.readouterr().out)
    assert single["meeting_id"] == "meeting-9"
    assert single["container_state"] == "running"

    worker_status(meeting_id=None, settings=settings, backend=backend)
    assert json.loads(capsys.readouterr().out) == []
    assert [call[0] for call in backend.calls] == ["status", "status_all"]


def test_worker_stop_prints_the_terminal_reason(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()

    worker_stop(meeting_id="meeting-9", timeout=30.0, settings=settings, backend=backend)

    assert json.loads(capsys.readouterr().out) == {
        "meeting_id": "meeting-9",
        "container_id": "abc123",
        "terminal_reason": "stopped_by_owner",
        "escalated": False,
    }


def test_worker_commands_never_print_secrets(
    settings: Settings, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from pydantic import SecretStr

    settings.database.url = SecretStr("postgresql://fakeyou:s3cret@localhost/fakeyou")
    backend = FakeBackend()

    worker_start(**start_arguments(), settings=settings, backend=backend)
    worker_status(meeting_id="meeting-9", settings=settings, backend=backend)
    worker_stop(meeting_id="meeting-9", timeout=30.0, settings=settings, backend=backend)

    output = capsys.readouterr().out
    assert "s3cret" not in output
    assert "test-key" not in output
    assert "memory-key" not in output
    assert "password" not in output


def test_worker_subcommands_dispatch_without_leaking_parser_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_start(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "worker_start", fake_start)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "worker", "start", LINK, "--meeting-id", "meeting-9", "--audio"],
    )
    cli_main.main()

    [call] = calls
    assert call["link"] == LINK
    assert call["meeting_id"] == "meeting-9"
    assert call["audio_enabled"] is True
    assert "worker_command" not in call
    assert "command" not in call


def test_worker_status_accepts_an_optional_meeting_id() -> None:
    parser = cli_main.build_parser()

    assert parser.parse_args(["worker", "status"]).meeting_id is None
    assert parser.parse_args(["worker", "status", "meeting-9"]).meeting_id == "meeting-9"
    assert parser.parse_args(["worker", "stop", "meeting-9"]).meeting_id == "meeting-9"


def test_worker_ingest_prints_the_job_status(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    from fakeyou.cli.worker import worker_ingest

    backend = FakeBackend()

    worker_ingest(
        meeting_id="meeting-9",
        persona_path=Path("persona.json"),
        owner_id=None,
        twin_id=None,
        settings=settings,
        backend=backend,
    )

    assert backend.calls == [
        (
            "ingest",
            {
                "meeting_id": "meeting-9",
                "persona_path": Path("persona.json"),
                "owner_id": None,
                "twin_id": None,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out) == {
        "meeting_id": "meeting-9",
        "status": "completed",
    }


def test_worker_ingest_exits_nonzero_unless_completed(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    from fakeyou.cli.worker import worker_ingest
    from fakeyou.worker.worker_backend import WorkerIngest

    backend = FakeBackend()

    def interrupted(meeting_id: str, **kwargs: Any) -> WorkerIngest:
        return WorkerIngest(meeting_id=meeting_id, status="interrupted")

    backend.ingest = interrupted

    with pytest.raises(SystemExit) as caught:
        worker_ingest(
            meeting_id="meeting-9",
            persona_path=Path("persona.json"),
            owner_id=None,
            twin_id=None,
            settings=settings,
            backend=backend,
        )

    assert caught.value.code == ExitCode.FAILURE
    assert json.loads(capsys.readouterr().out)["status"] == "interrupted"


def test_worker_ingest_parses_meeting_and_persona() -> None:
    from fakeyou.cli.worker import worker_ingest

    parser = cli_main.build_parser()

    options = parser.parse_args(["worker", "ingest", "meeting-9", "--persona", "p.json"])

    assert options.handler is worker_ingest
    assert options.meeting_id == "meeting-9"
    assert options.persona_path == Path("p.json")
    assert options.owner_id is None
    assert options.twin_id is None

    options = parser.parse_args(
        [
            "worker",
            "ingest",
            "meeting-9",
            "--persona",
            "p.json",
            "--owner-id",
            "o1",
            "--twin-id",
            "t1",
        ]
    )

    assert options.owner_id == "o1"
    assert options.twin_id == "t1"
