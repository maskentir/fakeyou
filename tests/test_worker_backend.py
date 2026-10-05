import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fakeyou.worker_backend import (
    DockerWorkerBackend,
    WorkerError,
    WorkerStartRequest,
    compose_override,
)


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.outputs: dict[str, str] = {}
        self.sequences: dict[str, list[str]] = {}
        self.failures: dict[str, tuple[int, str]] = {}
        self.blowups: dict[str, Exception] = {}
        self.default_output = ""

    def expect(self, needle: str, output: str) -> None:
        self.outputs[needle] = output

    def expect_sequence(self, needle: str, outputs: list[str]) -> None:
        self.sequences[needle] = list(outputs)

    def fail_with(self, needle: str, returncode: int, stderr: str) -> None:
        self.failures[needle] = (returncode, stderr)

    def blow_up(self, needle: str, error: Exception) -> None:
        self.blowups[needle] = error

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in args[0]]
        self.commands.append(command)
        text = " ".join(command)
        for needle, error in self.blowups.items():
            if needle in text:
                raise error
        for needle, outputs in self.sequences.items():
            if needle in text and outputs:
                return subprocess.CompletedProcess(command, 0, outputs.pop(0), "")
        for needle, (returncode, stderr) in self.failures.items():
            if needle in text:
                return subprocess.CompletedProcess(command, returncode, "", stderr)
        for needle, output in self.outputs.items():
            if needle in text:
                return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, self.default_output, "")


def running_container(container_id: str, name: str) -> str:
    return f"{container_id}\trunning\t{name}\n"


def write_journal(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def start_request(tmp_path: Path, **overrides: Any) -> WorkerStartRequest:
    persona = tmp_path / "persona.json"
    persona.write_text("{}", encoding="utf-8")
    brief = tmp_path / "brief.json"
    brief.write_text("{}", encoding="utf-8")
    values: dict[str, Any] = {
        "link": "https://meet.google.com/abc-defg-hij",
        "meeting_id": "meeting-9",
        "twin_id": "twin-1",
        "persona_path": persona,
        "brief_path": brief,
        "memory": True,
        "conversation": True,
        "audio": True,
        "duration": 600.0,
        "lobby_timeout": 60.0,
    }
    values.update(overrides)
    return WorkerStartRequest(**values)


def test_start_runs_a_labeled_detached_worker_with_join_arguments(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    request = start_request(
        runtime,
        persona_path=runtime / "persona.json",
        brief_path=runtime / "brief.json",
    )

    started = backend.start(request)

    assert started.meeting_id == "meeting-9"
    assert started.container_name == "fakeyou-worker-meeting-9"
    run = next(command for command in runner.commands if "run" in command)
    assert run[:3] == ["docker", "compose", "-f"]
    assert "-d" in run and "--rm" in run
    assert "--name" in run and "fakeyou-worker-meeting-9" in run
    join = run[run.index("join") :]
    assert join[1] == "https://meet.google.com/abc-defg-hij"
    assert "--meeting-id" in join and "meeting-9" in join
    assert "--persona" in join and ".runtime/persona.json" in join
    assert "--brief" in join and ".runtime/brief.json" in join
    assert "--memory" in join and "--converse" in join and "--audio" in join
    assert "--duration" in join and "600.0" in join
    assert "--lobby-timeout" in join and "60.0" in join


def test_compose_override_labels_the_container_per_meeting() -> None:
    assert compose_override("meeting-9", role="worker") == (
        'services:\n  meeting-worker:\n    labels:\n      fakeyou.managed: "true"\n'
        '      fakeyou.meeting-id: "meeting-9"\n      fakeyou.role: "worker"\n'
    )


def test_start_applies_a_label_override_and_queries_by_label(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    request = start_request(runtime)

    backend.start(request)

    run = next(command for command in runner.commands if "run" in command)
    assert run.count("-f") == 2
    ps = next(command for command in runner.commands if command[:2] == ["docker", "ps"])
    assert "label=fakeyou.meeting-id=meeting-9" in " ".join(ps)


def test_start_resumes_an_unfinished_session_with_the_same_meeting_id(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    journal_dir = tmp_path / "journals"
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=journal_dir,
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    write_journal(
        journal_dir / "meeting-9.jsonl",
        [
            {
                "event": "join_requested",
                "meeting_id": "meeting-9",
                "at": "2026-10-05T02:00:00+00:00",
                "source": "runtime",
            }
        ],
    )

    backend.start(start_request(runtime))

    run = next(command for command in runner.commands if "run" in command)
    join = run[run.index("join") :]
    assert "--resume" in join


def test_start_detects_the_unfinished_session_through_the_host_mount(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / ".runtime" / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    write_journal(
        runtime / "journals" / "meeting-9.jsonl",
        [
            {
                "event": "join_requested",
                "meeting_id": "meeting-9",
                "at": "2026-10-05T02:00:00+00:00",
                "source": "runtime",
            }
        ],
    )

    backend.start(start_request(runtime))

    run = next(command for command in runner.commands if "run" in command)
    join = run[run.index("join") :]
    assert "--resume" in join


def test_start_runs_a_fresh_meeting_without_resume(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    backend.start(start_request(runtime))

    run = next(command for command in runner.commands if "run" in command)
    join = run[run.index("join") :]
    assert "--resume" not in join


def test_start_refuses_a_meeting_that_is_already_running(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("docker ps", running_container("abc123", "fakeyou-worker-meeting-9"))
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    with pytest.raises(WorkerError, match="already running"):
        backend.start(start_request(runtime))

    assert all("run" not in command for command in runner.commands)


def test_start_rejects_persona_files_outside_the_mounted_volumes(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    outside = tmp_path / "elsewhere" / "persona.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="not visible"):
        backend.start(start_request(tmp_path, persona_path=outside))


def test_start_rejects_a_missing_persona_file(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    with pytest.raises(WorkerError, match="not found"):
        backend.start(start_request(runtime, persona_path=runtime / "absent.json"))


def test_status_merges_container_state_with_the_journal(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("docker ps", running_container("abc123", "fakeyou-worker-meeting-9"))
    journal_dir = tmp_path / "journals"
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=journal_dir,
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    write_journal(
        journal_dir / "meeting-9.jsonl",
        [
            {"event": "audio_opened", "at": "2026-10-05T02:00:00+00:00", "source": "runtime"},
            {
                "event": "join_finished",
                "at": "2026-10-05T02:01:00+00:00",
                "source": "runtime",
                "status": "admitted",
            },
            {
                "event": "conversation_bound",
                "at": "2026-10-05T02:01:01+00:00",
                "source": "runtime",
            },
            {
                "event": "fault_recorded",
                "at": "2026-10-05T02:02:00+00:00",
                "source": "stt",
                "kind": "reconnect",
            },
        ],
    )

    state = backend.status("meeting-9")

    assert state.meeting_id == "meeting-9"
    assert state.container_id == "abc123"
    assert state.container_state == "running"
    assert state.phase == "conversation"
    assert state.terminal_reason is None
    assert state.last_fault == {
        "source": "stt",
        "kind": "reconnect",
        "at": "2026-10-05T02:02:00+00:00",
    }


def test_status_reports_an_ended_session_from_the_journal(tmp_path: Path) -> None:
    runner = FakeRunner()
    journal_dir = tmp_path / "journals"
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=journal_dir,
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    write_journal(
        journal_dir / "meeting-9.jsonl",
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

    assert state.container_state == "absent"
    assert state.phase == "ended"
    assert state.terminal_reason == "stopped_by_owner"


def test_status_unknown_meeting_has_no_container_and_no_phase(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    state = backend.status("meeting-absent")

    assert state.container_state == "absent"
    assert state.phase is None
    assert state.terminal_reason is None
    assert state.last_fault is None


def test_status_all_lists_every_managed_meeting(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(
        "docker ps",
        "aaa111\trunning\tfakeyou-worker-meeting-1\tmeeting-1\n"
        "bbb222\texited\tfakeyou-worker-meeting-2\tmeeting-2\n",
    )
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    states = backend.status_all()

    assert [state.meeting_id for state in states] == ["meeting-1", "meeting-2"]
    assert states[0].container_state == "running"
    assert states[1].container_state == "exited"


def test_stop_waits_for_session_finished_and_reports_the_reason(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("docker ps", running_container("abc123", "fakeyou-worker-meeting-9"))
    journal_dir = tmp_path / "journals"
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=journal_dir,
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
        sleep=lambda _: None,
    )
    write_journal(
        journal_dir / "meeting-9.jsonl",
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

    stopped = backend.stop("meeting-9", timeout_seconds=5.0)

    assert stopped.terminal_reason == "stopped_by_owner"
    assert stopped.escalated is False
    assert any(command[:2] == ["docker", "stop"] for command in runner.commands)
    assert all("kill" not in command for command in runner.commands)


def test_stop_escalates_when_the_worker_ignores_the_graceful_stop(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    runner.expect("docker ps", running_container("abc123", "fakeyou-worker-meeting-9"))
    journal_dir = tmp_path / "journals"
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=journal_dir,
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
        sleep=lambda _: None,
    )

    stopped = backend.stop("meeting-9", timeout_seconds=0.0)

    assert stopped.escalated is True
    assert stopped.terminal_reason is None
    assert any(command[:2] == ["docker", "kill"] for command in runner.commands)
    stop = next(command for command in runner.commands if command[:2] == ["docker", "stop"])
    assert stop[stop.index("--time") + 1] == "0"


def test_stop_without_a_running_worker_or_session_is_an_error(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(WorkerError, match="no running worker"):
        backend.stop("meeting-absent", timeout_seconds=0.0)


@pytest.mark.parametrize("meeting_id", ['x"\n  evil: injected', "../../etc/x", "a/b", ""])
def test_meeting_ids_are_validated_before_any_docker_call(tmp_path: Path, meeting_id: str) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    with pytest.raises(WorkerError, match="invalid meeting ID"):
        backend.start(start_request(runtime, meeting_id=meeting_id))
    with pytest.raises(WorkerError, match="invalid meeting ID"):
        backend.status(meeting_id)
    with pytest.raises(WorkerError, match="invalid meeting ID"):
        backend.stop(meeting_id, timeout_seconds=0.0)

    assert runner.commands == []


def test_status_all_skips_rows_with_invalid_meeting_ids(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(
        "docker ps",
        "aaa111\trunning\tfakeyou-worker-meeting-1\tmeeting-1\n"
        "bbb222\trunning\tevil\t../../etc/x\n",
    )
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    states = backend.status_all()

    assert [state.meeting_id for state in states] == ["meeting-1"]


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


def test_start_anchors_compose_to_the_project_dir(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    started = backend.start(start_request(runtime))

    assert started.container_id == "abc123"
    run = next(command for command in runner.commands if "run" in command)
    assert run[run.index("-f") + 1] == str(tmp_path / "compose.yaml")


def test_start_rejects_an_empty_container_id(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    with pytest.raises(WorkerError, match="no container id"):
        backend.start(start_request(runtime))


def test_status_all_includes_journal_only_ended_meetings(tmp_path: Path) -> None:
    runner = FakeRunner()
    journal_dir = tmp_path / "journals"
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=journal_dir,
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    write_journal(
        journal_dir / "meeting-9.jsonl",
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

    states = {state.meeting_id: state for state in backend.status_all()}

    assert states["meeting-9"].container_state == "absent"
    assert states["meeting-9"].phase == "ended"


def test_docker_failures_raise_worker_errors_not_tracebacks(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.blow_up("docker", FileNotFoundError("docker"))
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(WorkerError, match="docker is not available"):
        backend.status("meeting-9")
    with pytest.raises(WorkerError, match="docker is not available"):
        backend.status_all()

    failing = FakeRunner()
    failing.fail_with("docker ps", 1, "Cannot connect to the Docker daemon")
    failing_backend = DockerWorkerBackend(
        runner=failing,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    with pytest.raises(WorkerError, match="Cannot connect"):
        failing_backend.status("meeting-9")


@pytest.mark.parametrize(
    "overrides",
    [
        {"link": "http://meet.google.com/abc-defg-hij"},
        {"link": "https://example.com/abc-defg-hij"},
        {"link": "https://meet.google.com/"},
        {"lobby_timeout": 0.0},
        {"duration": -1.0},
        {"initial_speech": "hi", "audio": False},
        {"initial_speech": "  ", "audio": True},
        {"conversation": True, "audio": False},
        {"memory": True, "conversation": False},
        {"memory": True, "persona_path": None},
        {"conversation": False, "audio": False, "memory": False, "brief_path": None},
        {
            "brief_path": Path("brief.json"),
            "persona_path": None,
            "conversation": False,
            "audio": False,
            "memory": False,
        },
        {"brief_path": Path("brief.json"), "persona_path": None},
        {"wake_phrase": "hey", "conversation": False},
        {"barge_in": True, "conversation": False},
    ],
)
def test_start_requests_reject_invalid_join_combinations(tmp_path: Path, overrides: dict) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    if overrides.get("brief_path") is not None:
        overrides["brief_path"] = runtime / "brief.json"

    with pytest.raises(WorkerError):
        backend.start(start_request(runtime, **overrides))

    assert all("run" not in command for command in runner.commands)


def test_start_maps_an_explicit_profile_dir_into_the_container(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    backend.start(start_request(runtime, profile_dir=str(runtime / "profiles" / "twin-1")))

    run = next(command for command in runner.commands if "run" in command)
    join = run[run.index("join") :]
    assert "--profile-dir" in join
    assert ".runtime/profiles/twin-1" in join


def test_start_rejects_a_profile_dir_outside_the_mounts(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    with pytest.raises(WorkerError, match="not visible"):
        backend.start(start_request(runtime, profile_dir=str(tmp_path / "profiles")))


def test_stop_rejects_a_negative_timeout(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(WorkerError, match="timeout"):
        backend.stop("meeting-9", timeout_seconds=-1.0)

    assert runner.commands == []


def test_stop_only_escalates_against_the_same_container(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect_sequence(
        "docker ps",
        [
            running_container("abc123", "fakeyou-worker-meeting-9"),
            running_container("def456", "fakeyou-worker-meeting-9"),
        ],
    )
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
        sleep=lambda _: None,
    )

    stopped = backend.stop("meeting-9", timeout_seconds=0.0)

    assert stopped.escalated is False
    assert all("kill" not in command for command in runner.commands)


def test_session_finished_without_a_reason_keeps_the_earlier_reason(tmp_path: Path) -> None:
    from fakeyou.worker_backend import summarize_session

    summary = summarize_session(
        [
            {"event": "session_finished", "terminal_reason": "stopped_by_owner"},
            {"event": "session_finished"},
        ]
    )

    assert summary["terminal_reason"] == "stopped_by_owner"


def test_summarize_session_maps_a_resume_marker_to_the_join_phase() -> None:
    from fakeyou.worker_backend import summarize_session

    summary = summarize_session(
        [
            {"event": "join_requested"},
            {"event": "conversation_bound"},
            {"event": "session_resumed", "attempt": 2},
        ]
    )

    assert summary["phase"] == "join"


FINISHED_ROWS = [
    {
        "event": "join_requested",
        "meeting_id": "meeting-9",
        "at": "2026-10-05T02:00:00+00:00",
        "source": "runtime",
    },
    {
        "event": "session_finished",
        "meeting_id": "meeting-9",
        "at": "2026-10-05T02:10:00+00:00",
        "source": "cli",
        "phase": "ended",
        "terminal_reason": "ended_by_host",
        "exit_code": 0,
    },
]


def write_ingestion(path: Path, status: str) -> None:
    from typing import cast

    from fakeyou.ingestion_status import IngestionState, IngestionStatus, write_status

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


def ingest_backend(tmp_path: Path, runner: Any) -> DockerWorkerBackend:
    return DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )


def test_ingest_runs_memory_ingest_in_a_foreground_container(tmp_path: Path) -> None:
    from fakeyou.ingestion_status import status_path
    from fakeyou.record import meeting_record_path

    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
    write_ingestion(status_path(record), "completed")
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.meeting_id == "meeting-9"
    assert ingested.status == "completed"
    run = next(command for command in runner.commands if "run" in command)
    assert "-d" not in run
    assert "--rm" in run
    assert "--name" in run and "fakeyou-ingest-meeting-9" in run
    assert run.count("-f") == 2
    tail = run[run.index("fakeyou") + 1 :]
    assert tail == [
        "memory-ingest",
        "--meeting",
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
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    backend.ingest("meeting-9", persona_path=persona, owner_id="wops_owner", twin_id="wops_twin")

    run = next(command for command in runner.commands if "run" in command)
    tail = run[run.index("fakeyou") + 1 :]
    assert tail == [
        "memory-ingest",
        "--meeting",
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
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    backend.ingest("meeting-9", persona_path=persona)

    run = next(command for command in runner.commands if "run" in command)
    tail = run[run.index("fakeyou") + 1 :]
    assert "--owner-id" not in tail
    assert "--twin-id" not in tail


def test_ingest_labels_the_job_container_with_the_ingest_role(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        if "run" in args:
            for part in args:
                if part.endswith(".yml") and Path(part).exists():
                    seen["override"] = Path(part).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
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
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="still running"):
        backend.ingest("meeting-9", persona_path=persona)

    assert all("run" not in command for command in runner.commands)


def test_ingest_refuses_while_another_ingest_job_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.default_output = running_container("ingest1", "fakeyou-ingest-meeting-9")
    runner.expect("label=fakeyou.role=worker", "")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="ingest job .* already running"):
        backend.ingest("meeting-9", persona_path=persona)

    assert all("run" not in command for command in runner.commands)
    queries = [command for command in runner.commands if command[:2] == ["docker", "ps"]]
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
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="no finished session"):
        backend.ingest("meeting-9", persona_path=persona)

    assert all("run" not in command for command in runner.commands)


def test_ingest_refuses_a_live_record_lock(tmp_path: Path) -> None:
    from fakeyou.ingestion_status import lock_path
    from fakeyou.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    write_live_lock(lock_path(meeting_record_path(tmp_path / "meetings", "meeting-9")))
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="record is locked"):
        backend.ingest("meeting-9", persona_path=persona)

    assert all("run" not in command for command in runner.commands)


def test_ingest_refuses_a_corrupt_record_lock(tmp_path: Path) -> None:
    from fakeyou.ingestion_status import lock_path
    from fakeyou.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    corrupt = lock_path(meeting_record_path(tmp_path / "meetings", "meeting-9"))
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{corrupt", encoding="utf-8")
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="lock is corrupt"):
        backend.ingest("meeting-9", persona_path=persona)

    assert all("run" not in command for command in runner.commands)


def test_ingest_rejects_persona_files_outside_the_mounted_volumes(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    outside = tmp_path / "persona.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="not visible inside"):
        backend.ingest("meeting-9", persona_path=outside)

    assert all("run" not in command for command in runner.commands)


def test_ingest_reports_an_interrupted_job_for_resume(tmp_path: Path) -> None:
    from fakeyou.ingestion_status import status_path
    from fakeyou.record import meeting_record_path

    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
    write_ingestion(status_path(record), "pending")

    def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        if "run" in args:
            write_ingestion(status_path(record), "interrupted")
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.status == "interrupted"


def test_ingest_resumes_from_an_interrupted_status(tmp_path: Path) -> None:
    from fakeyou.ingestion_status import status_path
    from fakeyou.record import meeting_record_path

    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
    write_ingestion(status_path(record), "interrupted")
    commands: list[list[str]] = []

    def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        commands.append(args)
        if "run" in args:
            write_ingestion(status_path(record), "completed")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.status == "completed"
    assert any("run" in command for command in commands)


def test_ingest_reports_unknown_when_the_job_writes_no_status(tmp_path: Path) -> None:
    from fakeyou.ingestion_status import status_path
    from fakeyou.record import meeting_record_path

    runner = FakeRunner()
    runner.fail_with(" run ", 1, "boom")
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
    write_ingestion(status_path(record), "pending")
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.status == "unknown"


def test_ingest_reports_an_interrupted_run_on_keyboard_interrupt(tmp_path: Path) -> None:
    def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        if "run" in args:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, 0, "", "")

    backend = ingest_backend(tmp_path, run)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError, match="interrupted"):
        backend.ingest("meeting-9", persona_path=persona)


def test_status_shows_ingestion_and_debrief_for_a_finished_meeting(
    tmp_path: Path,
) -> None:
    from fakeyou.ingestion_status import status_path
    from fakeyou.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
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
    queries = [command for command in runner.commands if command[:2] == ["docker", "ps"]]
    assert queries
    assert all("label=fakeyou.role=worker" in " ".join(command) for command in queries)
    assert all(
        command[:2] != ["docker", "stop"] and command[:2] != ["docker", "kill"]
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
    queries = [command for command in runner.commands if command[:2] == ["docker", "ps"]]
    assert queries
    assert all("label=fakeyou.role=worker" in " ".join(command) for command in queries)


def test_start_refuses_while_an_ingest_job_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.default_output = running_container("ingest1", "fakeyou-ingest-meeting-9")
    runner.expect("label=fakeyou.role=worker", "")
    backend = ingest_backend(tmp_path, runner)
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)

    with pytest.raises(WorkerError, match="ingest job .* still running"):
        backend.start(start_request(runtime))

    assert all("run" not in command for command in runner.commands)
    queries = [command for command in runner.commands if command[:2] == ["docker", "ps"]]
    assert any("label=fakeyou.role=ingest" in " ".join(command) for command in queries)


def test_status_all_lists_only_worker_containers(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("docker ps", "")
    backend = ingest_backend(tmp_path, runner)

    backend.status_all()

    queries = [command for command in runner.commands if command[:2] == ["docker", "ps"]]
    assert queries
    assert all("label=fakeyou.role=worker" in " ".join(command) for command in queries)


def test_status_shows_pending_ingestion_and_an_invalid_debrief(tmp_path: Path) -> None:
    from fakeyou.ingestion_status import status_path
    from fakeyou.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
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
    from fakeyou.ingestion_status import status_path
    from fakeyou.record import meeting_record_path

    runner = FakeRunner()
    backend = ingest_backend(tmp_path, runner)
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
    corrupt = status_path(record)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{invalid", encoding="utf-8")

    state = backend.status("meeting-9")

    assert state.ingestion == "invalid"
