import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.commands import Command, memory_ingest_args
from fakeyou.catalog.errors import ErrorCode
from fakeyou.config.identifiers import SessionId
from fakeyou.worker.worker_backend import WorkerError
from tests._persistence_fakes import FINISHED_ROWS, write_journal
from tests._runtime_fakes import FakeRunner
from tests._worker_support import docker_argv, ingest_backend, running_container, start_request


@pytest.fixture(autouse=True)
def _fixed_tool_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_memory_ingest_args_derive_the_head_from_the_command_catalog() -> None:
    args = memory_ingest_args(meeting_id=SessionId("meeting-9"), persona=Path("/tmp/persona.json"))

    assert args[:2] == str(Command.MEMORY_INGEST).split(" ")
    assert args[2:] == ["--meeting-id", "meeting-9", "--persona", "/tmp/persona.json"]


def test_memory_ingest_args_quote_paths_and_carry_identity() -> None:
    command = shlex.join(
        memory_ingest_args(
            meeting_id=SessionId("meeting-9"),
            persona=Path("/tmp/my persona.json"),
            owner_id=SessionId("owner-1"),
            twin_id=SessionId("twin-1"),
        )
    )

    assert command == (
        "fakeyou memory-ingest --meeting-id meeting-9 --persona '/tmp/my persona.json'"
        " --owner-id owner-1 --twin-id twin-1"
    )


def write_ingestion(path: Path, status: str) -> None:
    from typing import cast

    from fakeyou.persistence.ingestion_status import IngestionState, IngestionStatus, write_status

    path.parent.mkdir(parents=True, exist_ok=True)
    write_status(
        path,
        IngestionStatus(
            meeting_id="meeting-9",
            group_id="group-1",
            status=cast(IngestionState, status),
        ),
    )


def write_live_lock(path: Path) -> None:
    from datetime import UTC, datetime

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": "other-container",
                "pid": 42,
                "purpose": "meeting",
                "token": "live",
                "heartbeat": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_ingest_runs_memory_ingest_in_a_foreground_container(tmp_path: Path) -> None:
    from fakeyou.persistence.ingestion_status import status_path
    from fakeyou.persistence.record import meeting_record_path

    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    record = meeting_record_path(meetings, SessionId("meeting-9"))
    write_ingestion(status_path(record), "completed")
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.meeting_id == "meeting-9"
    assert ingested.status == "completed"
    run = next(command for command in runner.commands if "run" in command)
    assert "-d" not in run
    assert "--rm" in run
    assert "--name" in run
    assert "fakeyou-ingest-meeting-9" in run
    assert run.count("-f") == 2
    tail = run[run.index("fakeyou") + 1 :]
    assert tail == [
        "memory-ingest",
        "--meeting-id",
        "meeting-9",
        "--persona",
        ".runtime/persona.json",
    ]


def test_ingest_forwards_owner_and_twin_identity(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    backend.ingest("meeting-9", persona_path=persona, owner_id="wops_owner", twin_id="wops_twin")

    run = next(command for command in runner.commands if "run" in command)
    tail = run[run.index("fakeyou") + 1 :]
    assert tail == [
        "memory-ingest",
        "--meeting-id",
        "meeting-9",
        "--persona",
        ".runtime/persona.json",
        "--owner-id",
        "wops_owner",
        "--twin-id",
        "wops_twin",
    ]


def test_ingest_omits_identity_flags_when_unset(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    backend.ingest("meeting-9", persona_path=persona)

    run = next(command for command in runner.commands if "run" in command)
    tail = run[run.index("fakeyou") + 1 :]
    assert "--owner-id" not in tail
    assert "--twin-id" not in tail


def test_ingest_labels_the_job_container_with_the_ingest_role(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def run(command: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        if "run" in args:
            for part in args:
                if part.endswith(".yml") and Path(part).exists():
                    seen["override"] = Path(part).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    backend.ingest("meeting-9", persona_path=persona)

    assert 'fakeyou.role: "ingest"' in seen["override"]
    assert 'fakeyou.meeting-id: "meeting-9"' in seen["override"]


def test_ingest_refuses_a_session_that_is_still_running(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("docker ps", running_container("abc123", "fakeyou-worker-meeting-9"))
    backend = ingest_backend(tmp_path, runner)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="still running") as caught:
        backend.ingest("meeting-9", persona_path=persona)

    assert caught.value.code is ErrorCode.WORKER_STILL_RUNNING

    assert all("run" not in command for command in runner.commands)


def test_ingest_refuses_while_another_ingest_job_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.default_output = running_container("ingest1", "fakeyou-ingest-meeting-9")
    runner.expect("label=fakeyou.role=worker", "")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="ingest job .* already running") as caught:
        backend.ingest("meeting-9", persona_path=persona)

    assert caught.value.code is ErrorCode.WORKER_INGEST_ALREADY_RUNNING

    assert all("run" not in command for command in runner.commands)
    queries = [command for command in runner.commands if command[:2] == docker_argv("ps")]
    assert any("label=fakeyou.role=ingest" in " ".join(command) for command in queries)


def test_ingest_refuses_a_session_that_has_not_finished(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(
        tmp_path / "journals" / "meeting-9.jsonl",
        [
            {
                "event": "join_requested",
                "meeting_id": "meeting-9",
                "at": "2026-10-05T02:00:00+00:00",
                "source": "runtime",
            }
        ],
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="no finished session") as caught:
        backend.ingest("meeting-9", persona_path=persona)

    assert caught.value.code is ErrorCode.WORKER_INGEST_NO_FINISHED_SESSION

    assert all("run" not in command for command in runner.commands)


def test_ingest_refuses_a_live_record_lock(tmp_path: Path) -> None:
    from fakeyou.persistence.ingestion_status import lock_path
    from fakeyou.persistence.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    write_live_lock(lock_path(meeting_record_path(meetings, SessionId("meeting-9"))))
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="record is locked") as caught:
        backend.ingest("meeting-9", persona_path=persona)

    assert caught.value.code is ErrorCode.RECORD_LOCKED

    assert all("run" not in command for command in runner.commands)


def test_ingest_refuses_a_corrupt_record_lock(tmp_path: Path) -> None:
    from fakeyou.persistence.ingestion_status import lock_path
    from fakeyou.persistence.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    corrupt = lock_path(meeting_record_path(meetings, SessionId("meeting-9")))
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{corrupt", encoding="utf-8")
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="lock is corrupt") as caught:
        backend.ingest("meeting-9", persona_path=persona)

    assert caught.value.code is ErrorCode.RECORD_LOCK_CORRUPT

    assert all("run" not in command for command in runner.commands)


def test_ingest_rejects_persona_files_outside_the_mounted_volumes(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    outside = tmp_path / "persona.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="not visible inside") as caught:
        backend.ingest("meeting-9", persona_path=outside)

    assert caught.value.code is ErrorCode.WORKER_FILE_NOT_VISIBLE

    assert all("run" not in command for command in runner.commands)


def test_ingest_reports_an_interrupted_job_for_resume(tmp_path: Path) -> None:
    from fakeyou.persistence.ingestion_status import status_path
    from fakeyou.persistence.record import meeting_record_path

    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    record = meeting_record_path(meetings, SessionId("meeting-9"))
    write_ingestion(status_path(record), "pending")

    def run(command: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        if "run" in args:
            write_ingestion(status_path(record), "interrupted")
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.status == "interrupted"


def test_ingest_resumes_from_an_interrupted_status(tmp_path: Path) -> None:
    from fakeyou.persistence.ingestion_status import status_path
    from fakeyou.persistence.record import meeting_record_path

    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    record = meeting_record_path(meetings, SessionId("meeting-9"))
    write_ingestion(status_path(record), "interrupted")
    commands: list[list[str]] = []

    def run(command: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        commands.append(args)
        if "run" in args:
            write_ingestion(status_path(record), "completed")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.status == "completed"
    assert any("run" in command for command in commands)


def test_ingest_reports_unknown_when_the_job_writes_no_status(tmp_path: Path) -> None:
    from fakeyou.persistence.ingestion_status import status_path
    from fakeyou.persistence.record import meeting_record_path

    runner = FakeRunner()
    runner.fail_with(" run ", 1, "boom")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    record = meeting_record_path(meetings, SessionId("meeting-9"))
    write_ingestion(status_path(record), "pending")
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.status == "unknown"


def test_ingest_reports_an_interrupted_run_on_keyboard_interrupt(tmp_path: Path) -> None:
    def run(command: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        if "run" in args:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="interrupted") as caught:
        backend.ingest("meeting-9", persona_path=persona)

    assert caught.value.code is ErrorCode.WORKER_INGEST_INTERRUPTED


def test_status_shows_ingestion_and_debrief_for_a_finished_meeting(
    tmp_path: Path,
) -> None:
    from fakeyou.persistence.ingestion_status import status_path
    from fakeyou.persistence.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    record = meeting_record_path(meetings, SessionId("meeting-9"))
    write_ingestion(status_path(record), "completed")

    state = backend.status("meeting-9")

    assert state.phase == "ended"
    assert state.ingestion == "completed"
    assert state.debrief == "missing"


def test_stop_ignores_a_running_ingest_container(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("label=fakeyou.role=worker", "")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)

    stopped = backend.stop("meeting-9", timeout_seconds=0.0)

    assert stopped.container_id is None
    assert stopped.terminal_reason == "ended_by_host"
    assert stopped.escalated is False
    queries = [command for command in runner.commands if command[:2] == docker_argv("ps")]
    assert queries
    assert all("label=fakeyou.role=worker" in " ".join(command) for command in queries)
    assert all(
        command[:2] != docker_argv("stop") and command[:2] != docker_argv("kill")
        for command in runner.commands
    )


def test_status_hides_a_running_ingest_container(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("label=fakeyou.role=worker", "")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)

    state = backend.status("meeting-9")

    assert state.container_state == "absent"
    assert state.phase == "ended"
    queries = [command for command in runner.commands if command[:2] == docker_argv("ps")]
    assert queries
    assert all("label=fakeyou.role=worker" in " ".join(command) for command in queries)


def test_start_refuses_while_an_ingest_job_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.default_output = running_container("ingest1", "fakeyou-ingest-meeting-9")
    runner.expect("label=fakeyou.role=worker", "")
    backend = ingest_backend(tmp_path, runner)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkerError, match="ingest job .* already running") as caught:
        backend.start(start_request(runtime))

    assert caught.value.code is ErrorCode.WORKER_INGEST_ALREADY_RUNNING

    assert all("run" not in command for command in runner.commands)
    queries = [command for command in runner.commands if command[:2] == docker_argv("ps")]
    assert any("label=fakeyou.role=ingest" in " ".join(command) for command in queries)


def test_status_shows_pending_ingestion_and_an_invalid_debrief(tmp_path: Path) -> None:
    from fakeyou.persistence.ingestion_status import status_path
    from fakeyou.persistence.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    record = meeting_record_path(meetings, SessionId("meeting-9"))
    write_ingestion(status_path(record), "interrupted")
    report = record.with_name("debrief.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("{invalid", encoding="utf-8")

    state = backend.status("meeting-9")

    assert state.ingestion == "interrupted"
    assert state.debrief == "invalid"


def test_status_reports_an_unreadable_ingestion_status_as_invalid(
    tmp_path: Path,
) -> None:
    from fakeyou.persistence.ingestion_status import status_path
    from fakeyou.persistence.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    meetings = tmp_path / ".runtime" / "audio" / "meetings"
    record = meeting_record_path(meetings, SessionId("meeting-9"))
    corrupt = status_path(record)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{invalid", encoding="utf-8")

    state = backend.status("meeting-9")

    assert state.ingestion == "invalid"
