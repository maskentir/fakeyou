import shutil
from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.worker.worker_backend import DockerTiming, DockerWorkerBackend, WorkerError
from tests._persistence_fakes import write_journal
from tests._runtime_fakes import FakeRunner
from tests._worker_support import docker_argv, ingest_backend, running_container, start_request


@pytest.fixture(autouse=True)
def _fixed_tool_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


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
        timing=DockerTiming(sleep=lambda _: None),
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
    assert any(command[:2] == docker_argv("stop") for command in runner.commands)
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
        timing=DockerTiming(sleep=lambda _: None),
    )

    stopped = backend.stop("meeting-9", timeout_seconds=0.0)

    assert stopped.escalated is True
    assert stopped.terminal_reason is None
    assert any(command[:2] == docker_argv("kill") for command in runner.commands)
    stop = next(command for command in runner.commands if command[:2] == docker_argv("stop"))
    assert stop[stop.index("--time") + 1] == "0"


def test_stop_without_a_running_worker_or_session_is_an_error(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(WorkerError, match="no running worker") as caught:
        backend.stop("meeting-absent", timeout_seconds=0.0)

    assert caught.value.code is ErrorCode.WORKER_NOT_RUNNING


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
    runtime.mkdir(parents=True, exist_ok=True)

    with pytest.raises(FakeYouError, match="--meeting-id") as caught:
        backend.start(start_request(runtime, meeting_id=meeting_id))
    assert caught.value.code is ErrorCode.SESSION_ID_INVALID

    with pytest.raises(FakeYouError, match="meeting_id") as caught:
        backend.status(meeting_id)
    assert caught.value.code is ErrorCode.SESSION_ID_INVALID

    with pytest.raises(FakeYouError, match="meeting_id") as caught:
        backend.stop(meeting_id, timeout_seconds=0.0)
    assert caught.value.code is ErrorCode.SESSION_ID_INVALID

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


def test_stop_rejects_a_negative_timeout(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )

    with pytest.raises(WorkerError, match="timeout") as caught:
        backend.stop("meeting-9", timeout_seconds=-1.0)

    assert caught.value.code is ErrorCode.WORKER_TIMEOUT_NEGATIVE

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
        timing=DockerTiming(sleep=lambda _: None),
    )

    stopped = backend.stop("meeting-9", timeout_seconds=0.0)

    assert stopped.escalated is False
    assert all("kill" not in command for command in runner.commands)


def test_session_finished_without_a_reason_keeps_the_earlier_reason(tmp_path: Path) -> None:
    del tmp_path
    from fakeyou.worker.worker_backend import summarize_session

    summary = summarize_session(
        [
            {"event": "session_finished", "terminal_reason": "stopped_by_owner"},
            {"event": "session_finished"},
        ]
    )

    assert summary["terminal_reason"] == "stopped_by_owner"


def test_summarize_session_maps_a_resume_marker_to_the_join_phase() -> None:
    from fakeyou.worker.worker_backend import summarize_session

    summary = summarize_session(
        [
            {"event": "join_requested"},
            {"event": "conversation_bound"},
            {"event": "session_resumed", "attempt": 2},
        ]
    )

    assert summary["phase"] == "join"


def test_status_all_lists_only_worker_containers(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect("docker ps", "")
    backend = ingest_backend(tmp_path, runner)

    backend.status_all()

    queries = [command for command in runner.commands if command[:2] == docker_argv("ps")]
    assert queries
    assert all("label=fakeyou.role=worker" in " ".join(command) for command in queries)
