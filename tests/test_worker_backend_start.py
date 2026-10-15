import shutil
from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.worker.worker_backend import DockerWorkerBackend, WorkerError, compose_override
from tests._error_support import message_pattern
from tests._persistence_fakes import write_journal
from tests._runtime_fakes import FakeRunner
from tests._worker_support import docker_argv, running_container, start_request


@pytest.fixture(autouse=True)
def _fixed_tool_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_start_runs_a_labeled_detached_worker_with_join_arguments(tmp_path: Path) -> None:
    runner = FakeRunner()
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

    started = backend.start(request)

    assert started.meeting_id == "meeting-9"
    assert started.container_name == "fakeyou-worker-meeting-9"
    run = next(command for command in runner.commands if "run" in command)
    assert run[:3] == docker_argv("compose", "-f")
    assert "-d" in run
    assert "--rm" in run
    assert "--name" in run
    assert "fakeyou-worker-meeting-9" in run
    join = run[run.index("join") :]
    assert join[1] == "https://meet.google.com/abc-defg-hij"
    assert "--meeting-id" in join
    assert "meeting-9" in join
    assert "--persona" in join
    assert ".runtime/persona.json" in join
    assert "--brief" in join
    assert ".runtime/brief.json" in join
    assert "--memory" in join
    assert "--converse" in join
    assert "--audio" in join
    assert "--duration" in join
    assert "600.0" in join
    assert "--lobby-timeout" in join
    assert "60.0" in join


def test_compose_override_labels_the_container_per_meeting() -> None:
    assert compose_override(SessionId("meeting-9"), role="worker") == (
        'services:\n  meeting-worker:\n    labels:\n      fakeyou.managed: "true"\n'
        '      fakeyou.meeting-id: "meeting-9"\n      fakeyou.role: "worker"\n'
    )


def test_start_applies_a_label_override_and_queries_by_label(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    request = start_request(runtime)

    backend.start(request)

    run = next(command for command in runner.commands if "run" in command)
    assert run.count("-f") == 2
    ps = next(command for command in runner.commands if command[:2] == docker_argv("ps"))
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
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
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
    runtime.mkdir(parents=True, exist_ok=True)
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
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)

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
    runtime.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkerError, match="already running") as caught:
        backend.start(start_request(runtime))

    assert caught.value.code is ErrorCode.WORKER_ALREADY_RUNNING
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

    with pytest.raises(WorkerError, match="not visible") as caught:
        backend.start(start_request(tmp_path, persona_path=outside))

    assert caught.value.code is ErrorCode.WORKER_FILE_NOT_VISIBLE


def test_start_rejects_a_missing_persona_file(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkerError, match="not found") as caught:
        backend.start(start_request(runtime, persona_path=runtime / "absent.json"))

    assert caught.value.code is ErrorCode.WORKER_FILE_MISSING


def test_start_anchors_compose_to_the_project_dir(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)

    started = backend.start(start_request(runtime))

    assert started.container_id == "abc123"
    run = next(command for command in runner.commands if "run" in command)
    assert run[run.index("-f") + 1] == str(tmp_path / "compose.yaml")


def test_start_rejects_an_empty_container_id(tmp_path: Path) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkerError, match="no container id") as caught:
        backend.start(start_request(runtime))

    assert caught.value.code is ErrorCode.WORKER_START_NO_CONTAINER


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"link": "http://meet.google.com/abc-defg-hij"}, ErrorCode.MEETING_LINK_INVALID),
        ({"link": "https://example.com/abc-defg-hij"}, ErrorCode.MEETING_LINK_INVALID),
        ({"link": "https://meet.google.com/"}, ErrorCode.MEETING_LINK_INVALID),
        ({"lobby_timeout": 0.0}, ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE),
        ({"duration": -1.0}, ErrorCode.MEETING_DURATION_NOT_POSITIVE),
        ({"initial_speech": "hi", "audio": False}, ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO),
        ({"initial_speech": "  ", "audio": True}, ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO),
        ({"conversation": True, "audio": False}, ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO),
        ({"memory": True, "conversation": False}, ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE),
        ({"memory": True, "persona_path": None}, ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA),
        (
            {"conversation": False, "audio": False, "memory": False, "brief_path": None},
            ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE,
        ),
        (
            {
                "brief_path": Path("brief.json"),
                "persona_path": None,
                "conversation": False,
                "audio": False,
                "memory": False,
            },
            ErrorCode.MEETING_BRIEF_REQUIRES_CONVERSE,
        ),
        (
            {"brief_path": Path("brief.json"), "persona_path": None, "memory": False},
            ErrorCode.MEETING_BRIEF_REQUIRES_PERSONA,
        ),
        (
            {
                "wake_phrase": "hey",
                "conversation": False,
                "memory": False,
                "persona_path": None,
                "brief_path": None,
            },
            ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE,
        ),
        (
            {
                "barge_in": True,
                "conversation": False,
                "memory": False,
                "persona_path": None,
                "brief_path": None,
            },
            ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE,
        ),
    ],
)
def test_start_requests_reject_invalid_join_combinations(
    tmp_path: Path, overrides: dict[str, Any], code: ErrorCode
) -> None:
    runner = FakeRunner()
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)
    if overrides.get("brief_path") is not None:
        overrides["brief_path"] = runtime / "brief.json"

    with pytest.raises(FakeYouError, match=message_pattern(code)) as caught:
        backend.start(start_request(runtime, **overrides))

    assert caught.value.code is code
    assert all("run" not in command for command in runner.commands)


def test_start_maps_an_explicit_profile_dir_into_the_container(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True, exist_ok=True)

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
    runtime.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkerError, match="not visible") as caught:
        backend.start(start_request(runtime, profile_dir=str(tmp_path / "profiles")))

    assert caught.value.code is ErrorCode.WORKER_FILE_NOT_VISIBLE
