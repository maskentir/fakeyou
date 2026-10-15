import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, override

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS
from fakeyou.worker.worker_backend import (
    COMPOSE_RUNTIME_ROOT,
    STOP_GRACE_SECONDS,
    ComposeMounts,
    DockerTiming,
    DockerWorkerBackend,
    WorkerError,
)
from tests._error_support import message_pattern
from tests._persistence_fakes import FINISHED_ROWS, write_journal
from tests._runtime_fakes import FakeRunner
from tests._worker_support import docker_argv, running_container, start_request


@pytest.fixture(autouse=True)
def _fixed_tool_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_status_reads_journals_from_the_host_mount(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=Path(".runtime/journals"),
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    write_journal(
        tmp_path / ".runtime" / "audio" / "journals" / "meeting-9.jsonl",
        [
            {
                "event": "session_finished",
                "at": "2026-10-05T02:10:00+00:00",
                "source": "cli",
                "phase": "ended",
                "terminal_reason": "stopped_by_owner",
                "exit_code": 0,
            },
        ],
    )

    state = backend.status("meeting-9")

    assert state.phase == "ended"
    assert state.terminal_reason == "stopped_by_owner"


def test_host_mount_derives_the_runtime_root_from_settings(tmp_path: Path) -> None:
    mounts = ComposeMounts(
        project_dir=tmp_path,
        journal_dir=Path(".runtime/journals"),
        record_dir=Path(".runtime/meetings"),
    )

    assert mounts.host_record_dir == tmp_path / ".runtime" / "audio" / "meetings"
    assert mounts.host_journal_dir == tmp_path / ".runtime" / "audio" / "journals"


def test_host_mount_leaves_dirs_outside_the_compose_mount_unchanged(tmp_path: Path) -> None:
    mounts = ComposeMounts(
        project_dir=tmp_path,
        journal_dir=Path("data/journals"),
        record_dir=Path("data/meetings"),
    )

    assert mounts.host_record_dir == Path("data/meetings")
    assert mounts.host_journal_dir == Path("data/journals")


def test_run_docker_fails_fast_when_docker_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    backend = DockerWorkerBackend(
        runner=FakeRunner(),
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.DOCKER_UNAVAILABLE)) as error:
        backend.status("meeting-9")

    assert error.value.code is ErrorCode.DOCKER_UNAVAILABLE
    assert "PATH" in str(error.value)


def test_container_path_derives_the_mount_from_settings(tmp_path: Path) -> None:
    mounts = ComposeMounts(
        project_dir=tmp_path,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
    )
    persona = tmp_path / ".runtime" / "audio" / "persona.json"
    persona.parent.mkdir(parents=True)
    persona.write_text("{}", encoding="utf-8")

    assert mounts.container_path(persona) == ".runtime/persona.json"


def test_container_path_rejects_files_under_an_unmounted_root(tmp_path: Path) -> None:
    mounts = ComposeMounts(
        project_dir=tmp_path,
        journal_dir=tmp_path / "journals",
        record_dir=Path("data/meetings"),
    )
    persona = tmp_path / "data" / "audio" / "persona.json"
    persona.parent.mkdir(parents=True)
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="not visible") as caught:
        mounts.container_path(persona)

    assert caught.value.code is ErrorCode.WORKER_FILE_NOT_VISIBLE


def test_status_keeps_a_host_side_journal_dir_unchanged(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / ".runtime" / "audio" / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    write_journal(
        tmp_path / ".runtime" / "audio" / "journals" / "meeting-9.jsonl",
        [
            {
                "event": "session_finished",
                "at": "2026-10-05T02:10:00+00:00",
                "source": "cli",
                "phase": "ended",
                "terminal_reason": "ended_by_host",
                "exit_code": 0,
            },
        ],
    )

    state = backend.status("meeting-9")

    assert state.terminal_reason == "ended_by_host"


def test_docker_failures_raise_worker_errors_not_tracebacks(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.blow_up("docker", FileNotFoundError("docker"))
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(WorkerError, match="docker is not available") as caught:
        backend.status("meeting-9")
    assert caught.value.code is ErrorCode.DOCKER_UNAVAILABLE
    assert str(caught.value) == "docker is not available: FileNotFoundError: docker"

    with pytest.raises(WorkerError, match="docker is not available") as caught:
        backend.status_all()
    assert caught.value.code is ErrorCode.DOCKER_UNAVAILABLE
    assert str(caught.value) == "docker is not available: FileNotFoundError: docker"

    failing = FakeRunner()
    failing.fail_with("docker ps", 1, "Cannot connect to the Docker daemon")
    failing_backend = DockerWorkerBackend(
        runner=failing,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    with pytest.raises(WorkerError, match="Cannot connect") as caught:
        failing_backend.status("meeting-9")

    assert caught.value.code is ErrorCode.DOCKER_PS_FAILED


def test_stop_grace_matches_compose_grace_period() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"stop_grace_period:\s*(\d+)s", compose)

    assert match is not None
    assert STOP_GRACE_SECONDS == float(match.group(1))


def test_runtime_mount_matches_compose_volumes() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"- \./(.+?)/audio:/workspace/(.+?)(?::|\s)", compose)

    assert match is not None
    assert match.group(1) == COMPOSE_RUNTIME_ROOT
    assert match.group(2) == COMPOSE_RUNTIME_ROOT


class _TimeoutRecordingRunner(FakeRunner):
    pass


def test_run_docker_applies_the_configured_timeout(tmp_path: Path) -> None:
    runner = _TimeoutRecordingRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
        timing=DockerTiming(timeout_seconds=12.5),
    )

    backend.status_all()

    assert runner.timeouts != []
    assert all(timeout == 12.5 for timeout in runner.timeouts)


def test_start_bounds_the_compose_run_by_the_configured_start_budget(tmp_path: Path) -> None:
    runner = _TimeoutRecordingRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    request = start_request(
        runtime,
        persona_path=runtime / "persona.json",
        brief_path=runtime / "brief.json",
    )

    backend.start(request)

    index = next(i for i, command in enumerate(runner.commands) if "run" in command)
    assert runner.timeouts[index] == DEFAULTS.worker.start_timeout_seconds
    assert all(timeout is not None for i, timeout in enumerate(runner.timeouts) if i != index)


def test_stop_gives_docker_stop_a_grace_covering_timeout(tmp_path: Path) -> None:
    class _CommandTimeoutRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[list[str], Any]] = []

        @override
        def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(([str(part) for part in args[0]], kwargs.get("timeout")))
            return super().__call__(*args, **kwargs)

    runner = _CommandTimeoutRunner()
    runner.expect("docker ps", running_container("abc123", "fakeyou-worker-meeting-9"))
    journal_dir = tmp_path / "journals"
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=journal_dir,
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
        timing=DockerTiming(sleep=lambda _: None),
    )
    write_journal(journal_dir / "meeting-9.jsonl", FINISHED_ROWS)

    backend.stop("meeting-9", timeout_seconds=100.0)

    stop = next(call for call in runner.calls if call[0][:2] == docker_argv("stop"))
    grace = int(stop[0][stop[0].index("--time") + 1])
    assert grace == 100
    assert stop[1] is not None
    assert stop[1] >= grace


def test_run_docker_timeout_maps_to_docker_unavailable(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.blow_up("docker ps", subprocess.TimeoutExpired(["docker", "ps"], 30.0))
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(WorkerError, match="docker is not available") as caught:
        backend.status_all()

    assert caught.value.code is ErrorCode.DOCKER_UNAVAILABLE
    assert str(caught.value).startswith("docker is not available: TimeoutExpired: Command ")


def test_stop_grace_derives_from_teardown_budgets() -> None:
    from fakeyou.config.settings import DEFAULTS
    from fakeyou.foundation.invariants import LEAVE_BUDGET_SECONDS
    from fakeyou.observability.cleanup import CLOSE_BUDGET_SECONDS
    from fakeyou.worker.worker_backend import (
        ASSEMBLY_STACK_CLOSE_COUNT,
        FINISH_CLOSE_COUNT,
        FINISH_SEND_COUNT,
        RUNTIME_STACK_CLOSE_COUNT,
        TEARDOWN_CLOSE_COUNT,
    )

    assert (FINISH_CLOSE_COUNT, RUNTIME_STACK_CLOSE_COUNT, ASSEMBLY_STACK_CLOSE_COUNT) == (
        4,
        5,
        2,
    )
    assert FINISH_SEND_COUNT == 2
    assert STOP_GRACE_SECONDS == (
        TEARDOWN_CLOSE_COUNT * CLOSE_BUDGET_SECONDS
        + FINISH_SEND_COUNT * DEFAULTS.database.send_timeout_seconds
        + LEAVE_BUDGET_SECONDS
        + DEFAULTS.memory.close_hard_seconds
    )
    assert STOP_GRACE_SECONDS == 136.0
