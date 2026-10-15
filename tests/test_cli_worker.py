import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.worker import worker_start, worker_status, worker_stop
from fakeyou.config.settings import Settings
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.worker.worker_backend import (
    WorkerStarted,
    WorkerStartRequest,
    WorkerState,
    WorkerStopped,
)

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


def start_request(**overrides: Any) -> WorkerStartRequest:
    values: dict[str, Any] = {
        "link": LINK,
        "twin_name": None,
        "meeting_id": "meeting-9",
        "lobby_timeout": None,
        "duration": None,
        "profile_dir": None,
        "headless": None,
        "audio": True,
        "initial_speech": None,
        "conversation": True,
        "brief_path": None,
        "memory": False,
        "wake_phrase": None,
        "barge_in": False,
    }
    values.update(overrides)
    identity = values.pop(
        "identity",
        OperatorIdentity(
            values.pop("persona_path", None),
            values.pop("owner_id", None),
            values.pop("twin_id", "twin-1"),
        ),
    )
    return WorkerStartRequest(
        owner_id=identity.owner_id,
        twin_id=identity.twin_id,
        persona_path=identity.persona_path,
        **values,
    )


def test_worker_start_maps_arguments_and_prints_ids(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()

    worker_start(start_request(), settings=settings, backend=backend)

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
        worker_start(start_request(), settings=settings)

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
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pydantic import SecretStr

    settings.database.url = SecretStr("postgresql://fakeyou:s3cret@localhost/fakeyou")
    backend = FakeBackend()

    worker_start(start_request(), settings=settings, backend=backend)
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
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["request"].link == LINK
    assert call["request"].meeting_id == "meeting-9"
    assert call["request"].audio is True
    assert set(call) == {"request", "settings"}


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (["worker", "status"], "worker_status", {"meeting_id": None}),
        (["worker", "status", "meeting-9"], "worker_status", {"meeting_id": "meeting-9"}),
        (["worker", "stop", "meeting-9"], "worker_stop", {"meeting_id": "meeting-9"}),
    ],
)
def test_worker_status_and_stop_dispatch_meeting_id(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, expected: dict[str, Any]
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_command(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, target, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert {key: call[key] for key in expected} == expected


def test_worker_ingest_prints_the_job_status(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    from fakeyou.cli.worker import worker_ingest

    backend = FakeBackend()

    worker_ingest(
        meeting_id="meeting-9",
        identity=OperatorIdentity(Path("persona.json"), None, None),
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

    def interrupted(meeting_id: str, **_kwargs: Any) -> WorkerIngest:
        return WorkerIngest(meeting_id=meeting_id, status="interrupted")

    backend_any = cast(Any, backend)
    backend_any.ingest = interrupted

    with pytest.raises(SystemExit) as caught:
        worker_ingest(
            meeting_id="meeting-9",
            identity=OperatorIdentity(Path("persona.json"), None, None),
            settings=settings,
            backend=backend,
        )

    assert caught.value.code == ExitCode.FAILURE
    assert json.loads(capsys.readouterr().out)["status"] == "interrupted"


def test_worker_ingest_dispatches_meeting_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_ingest(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "worker_ingest", fake_ingest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "worker",
            "ingest",
            "meeting-9",
            "--persona",
            "p.json",
            "--owner-id",
            "o1",
            "--twin-id",
            "t1",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["meeting_id"] == "meeting-9"
    assert call["identity"] == OperatorIdentity(Path("p.json"), "o1", "t1")
    assert set(call) == {"meeting_id", "identity", "settings"}


def test_worker_cli_passes_configured_docker_timeout(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fakeyou.cli.worker as cli_worker

    settings.worker.docker_timeout_seconds = 7.5
    captured: dict[str, Any] = {}

    def fake_backend_class(**kwargs: Any) -> FakeBackend:
        captured.update(kwargs)
        return FakeBackend()

    monkeypatch.setattr(cli_worker, "DockerWorkerBackend", fake_backend_class)
    worker_status(meeting_id=None, settings=settings)

    assert captured["timing"].timeout_seconds == 7.5
