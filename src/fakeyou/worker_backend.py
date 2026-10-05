import math
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import TypeAdapter

from fakeyou.invariants import SessionId
from fakeyou.journal import journal_path
from fakeyou.report import describe_session, read_journal

WORKER_SERVICE = "meeting-worker"
MANAGED_LABEL = "fakeyou.managed"
MEETING_LABEL = "fakeyou.meeting-id"
ROLE_LABEL = "fakeyou.role"
CONTAINER_PREFIX = "fakeyou-worker-"
STOP_GRACE_SECONDS = 30.0
STATUS_POLL_SECONDS = 0.5

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class WorkerError(Exception):
    pass


_MEETING_ID = TypeAdapter(SessionId)


def checked_meeting_id(meeting_id: str) -> str:
    try:
        return _MEETING_ID.validate_python(meeting_id)
    except ValueError:
        raise WorkerError(f"invalid meeting ID: {meeting_id!r}") from None


@dataclass(frozen=True, slots=True)
class WorkerStartRequest:
    link: str
    meeting_id: str
    owner_id: str | None = None
    twin_id: str | None = None
    twin_name: str | None = None
    lobby_timeout: float | None = None
    duration: float | None = None
    profile_dir: str | None = None
    headless: bool | None = None
    audio: bool = False
    initial_speech: str | None = None
    conversation: bool = False
    persona_path: Path | None = None
    brief_path: Path | None = None
    memory: bool = False
    wake_phrase: str | None = None
    barge_in: bool = False

    def __post_init__(self) -> None:
        checked_meeting_id(self.meeting_id)
        parsed = urlparse(self.link)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "meet.google.com"
            or parsed.path.strip("/") == ""
        ):
            raise WorkerError("link must be an https://meet.google.com/ meeting URL")
        if self.lobby_timeout is not None and not self.lobby_timeout > 0:
            raise WorkerError("--lobby-timeout must be positive")
        if self.duration is not None and not self.duration > 0:
            raise WorkerError("--duration must be positive")
        if self.initial_speech is not None and (not self.audio or not self.initial_speech.strip()):
            raise WorkerError("--speak requires --audio and nonempty text")
        if self.conversation and not self.audio:
            raise WorkerError("--converse requires --audio")
        if self.memory and not self.conversation:
            raise WorkerError("--memory requires --converse")
        if self.persona_path is not None and not self.conversation:
            raise WorkerError("--persona requires --converse")
        if self.memory and self.persona_path is None:
            raise WorkerError("--memory requires --persona")
        if self.brief_path is not None and not self.conversation:
            raise WorkerError("--brief requires --converse")
        if self.brief_path is not None and self.persona_path is None:
            raise WorkerError("--brief requires --persona")
        if self.wake_phrase and not self.conversation:
            raise WorkerError("--wake-phrase requires --converse")
        if self.barge_in and not self.conversation:
            raise WorkerError("--barge-in requires --converse")


@dataclass(frozen=True, slots=True)
class WorkerStarted:
    meeting_id: str
    container_id: str
    container_name: str


@dataclass(frozen=True, slots=True)
class WorkerState:
    meeting_id: str
    container_id: str | None
    container_name: str | None
    container_state: str
    phase: str | None
    terminal_reason: str | None
    last_fault: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class WorkerStopped:
    meeting_id: str
    container_id: str | None
    terminal_reason: str | None
    escalated: bool


@dataclass(frozen=True, slots=True)
class WorkerIngest:
    meeting_id: str
    status: str


class WorkerBackend(Protocol):
    def start(self, request: WorkerStartRequest) -> WorkerStarted: ...
    def status(self, meeting_id: str) -> WorkerState: ...
    def status_all(self) -> list[WorkerState]: ...
    def stop(self, meeting_id: str, *, timeout_seconds: float) -> WorkerStopped: ...
    def ingest(self, meeting_id: str, *, persona_path: Path) -> WorkerIngest: ...


def compose_override(meeting_id: str, *, role: str) -> str:
    return (
        "services:\n"
        f"  {WORKER_SERVICE}:\n"
        "    labels:\n"
        f'      {MANAGED_LABEL}: "true"\n'
        f'      {MEETING_LABEL}: "{meeting_id}"\n'
        f'      {ROLE_LABEL}: "{role}"\n'
    )


def summarize_session(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    phase: str | None = None
    terminal_reason: str | None = None
    last_fault: dict[str, Any] | None = None
    for row in rows:
        event = row.get("event")
        if event == "audio_opened":
            phase = "preparing"
        elif event in ("join_requested", "session_resumed"):
            phase = "join"
        elif event == "join_finished":
            phase = "admitted" if row.get("status") == "admitted" else "ended"
        elif event in ("microphone_enabled", "conversation_bound"):
            phase = "conversation"
        elif event == "session_finished":
            phase = "ended"
            if row.get("terminal_reason") is not None:
                terminal_reason = row.get("terminal_reason")
        elif event == "cli_exited" and terminal_reason is None:
            terminal_reason = row.get("terminal_reason")
        elif event == "fault_recorded":
            last_fault = {
                "source": row.get("source"),
                "kind": row.get("kind"),
                "at": row.get("at"),
            }
    return {"phase": phase, "terminal_reason": terminal_reason, "last_fault": last_fault}


class DockerWorkerBackend:
    def __init__(
        self,
        *,
        journal_dir: Path,
        project_dir: Path | None = None,
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._journal_dir = journal_dir
        self._project_dir = project_dir or Path.cwd()
        self._run: Runner = runner or subprocess.run
        self._sleep = sleep

    def start(self, request: WorkerStartRequest) -> WorkerStarted:
        if self._running_container(request.meeting_id) is not None:
            raise WorkerError(f"worker for meeting {request.meeting_id} is already running")
        join_args = self._join_args(request)
        if describe_session(self._host_journal_dir(), request.meeting_id) == "unfinished":
            join_args.append("--resume")
        name = f"{CONTAINER_PREFIX}{request.meeting_id}"
        override: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yml", prefix="fakeyou-worker-", delete=False
            ) as handle:
                handle.write(compose_override(request.meeting_id, role="worker"))
                override = handle.name
            completed = self._run_docker(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(self._project_dir / "compose.yaml"),
                    "-f",
                    override,
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    name,
                    WORKER_SERVICE,
                    "fakeyou",
                    *join_args,
                ]
            )
        finally:
            if override is not None:
                Path(override).unlink(missing_ok=True)
        if completed.returncode != 0:
            raise WorkerError(f"worker start failed: {completed.stderr.strip()}")
        container_id = completed.stdout.strip()
        if not container_id:
            raise WorkerError("worker start produced no container id")
        return WorkerStarted(
            meeting_id=request.meeting_id,
            container_id=container_id,
            container_name=name,
        )

    def status(self, meeting_id: str) -> WorkerState:
        checked_meeting_id(meeting_id)
        return self._state(meeting_id, self._find_container(meeting_id))

    def status_all(self) -> list[WorkerState]:
        completed = self._run_docker(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={MANAGED_LABEL}=true",
                "--format",
                '{{.ID}}\t{{.State}}\t{{.Names}}\t{{.Label "fakeyou.meeting-id"}}',
            ]
        )
        if completed.returncode != 0:
            raise WorkerError(f"docker ps failed: {completed.stderr.strip()}")
        states: dict[str, WorkerState] = {}
        for line in completed.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or not parts[0] or not parts[3]:
                continue
            try:
                checked_meeting_id(parts[3])
            except WorkerError:
                continue
            states[parts[3]] = self._state(parts[3], (parts[0], parts[1], parts[2]))
        for meeting_id in self._journal_meeting_ids():
            if meeting_id not in states:
                states[meeting_id] = self._state(meeting_id, None)
        return [states[meeting_id] for meeting_id in sorted(states)]

    def stop(self, meeting_id: str, *, timeout_seconds: float) -> WorkerStopped:
        checked_meeting_id(meeting_id)
        if timeout_seconds < 0:
            raise WorkerError("--timeout must not be negative")
        container_id = self._running_container(meeting_id)
        if container_id is None:
            summary = summarize_session(self._read_session(meeting_id))
            if summary["phase"] == "ended":
                return WorkerStopped(
                    meeting_id=meeting_id,
                    container_id=None,
                    terminal_reason=summary["terminal_reason"],
                    escalated=False,
                )
            raise WorkerError(f"no running worker for meeting {meeting_id}")
        grace = min(int(STOP_GRACE_SECONDS), math.ceil(timeout_seconds))
        self._run_docker(["docker", "stop", "--time", str(grace), container_id])
        deadline = time.monotonic() + timeout_seconds
        terminal_reason: str | None = None
        while True:
            summary = summarize_session(self._read_session(meeting_id))
            if summary["phase"] == "ended":
                terminal_reason = summary["terminal_reason"]
                break
            if time.monotonic() >= deadline:
                break
            self._sleep(STATUS_POLL_SECONDS)
        if terminal_reason is None and self._running_container(meeting_id) == container_id:
            self._run_docker(["docker", "kill", container_id])
            return WorkerStopped(
                meeting_id=meeting_id,
                container_id=container_id,
                terminal_reason=None,
                escalated=True,
            )
        return WorkerStopped(
            meeting_id=meeting_id,
            container_id=container_id,
            terminal_reason=terminal_reason,
            escalated=False,
        )

    def ingest(self, meeting_id: str, *, persona_path: Path) -> WorkerIngest:
        raise NotImplementedError

    def _run_docker(self, args: list[str]) -> "subprocess.CompletedProcess[str]":
        try:
            return self._run(args, capture_output=True, text=True, check=False)
        except OSError as error:
            raise WorkerError(f"docker is not available: {error}") from None

    def _state(self, meeting_id: str, found: tuple[str, str, str] | None) -> WorkerState:
        summary = summarize_session(self._read_session(meeting_id))
        if found is None:
            return WorkerState(
                meeting_id=meeting_id,
                container_id=None,
                container_name=None,
                container_state="absent",
                phase=summary["phase"],
                terminal_reason=summary["terminal_reason"],
                last_fault=summary["last_fault"],
            )
        container_id, state, container_name = found
        return WorkerState(
            meeting_id=meeting_id,
            container_id=container_id,
            container_name=container_name,
            container_state=state,
            phase=summary["phase"],
            terminal_reason=summary["terminal_reason"],
            last_fault=summary["last_fault"],
        )

    def _read_session(self, meeting_id: str) -> list[dict[str, Any]]:
        return read_journal(journal_path(self._host_journal_dir(), meeting_id))

    def _host_journal_dir(self) -> Path:
        resolved = (self._project_dir / self._journal_dir).resolve()
        runtime_root = (self._project_dir / ".runtime").resolve()
        try:
            relative = resolved.relative_to(runtime_root)
        except ValueError:
            return self._journal_dir
        if relative.parts and relative.parts[0] == "audio":
            return self._journal_dir
        return self._project_dir / ".runtime" / "audio" / relative

    def _journal_meeting_ids(self) -> list[str]:
        directory = self._host_journal_dir()
        if not directory.is_dir():
            return []
        meeting_ids: list[str] = []
        for path in directory.glob("*.jsonl"):
            try:
                meeting_ids.append(checked_meeting_id(path.stem))
            except WorkerError:
                continue
        return meeting_ids

    def _running_container(self, meeting_id: str) -> str | None:
        completed = self._run_docker(
            [
                "docker",
                "ps",
                "--filter",
                f"label={MEETING_LABEL}={meeting_id}",
                "--format",
                "{{.ID}}",
            ]
        )
        if completed.returncode != 0:
            raise WorkerError(f"docker ps failed: {completed.stderr.strip()}")
        for line in completed.stdout.splitlines():
            if line.strip():
                return line.strip()
        return None

    def _find_container(self, meeting_id: str) -> tuple[str, str, str] | None:
        completed = self._run_docker(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={MEETING_LABEL}={meeting_id}",
                "--format",
                "{{.ID}}\t{{.State}}\t{{.Names}}",
            ]
        )
        if completed.returncode != 0:
            raise WorkerError(f"docker ps failed: {completed.stderr.strip()}")
        for line in completed.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0]:
                return parts[0], parts[1], parts[2]
        return None

    def _join_args(self, request: WorkerStartRequest) -> list[str]:
        args = ["join", request.link, "--meeting-id", request.meeting_id]
        if request.twin_name is not None:
            args += ["--twin-name", request.twin_name]
        if request.owner_id is not None:
            args += ["--owner-id", request.owner_id]
        if request.twin_id is not None:
            args += ["--twin-id", request.twin_id]
        if request.lobby_timeout is not None:
            args += ["--lobby-timeout", str(request.lobby_timeout)]
        if request.duration is not None:
            args += ["--duration", str(request.duration)]
        if request.profile_dir is not None:
            args += [
                "--profile-dir",
                self._container_path(Path(request.profile_dir), must_exist=False),
            ]
        if request.headless:
            args += ["--headless"]
        if request.audio:
            args += ["--audio"]
        if request.initial_speech is not None:
            args += ["--speak", request.initial_speech]
        if request.conversation:
            args += ["--converse"]
        if request.persona_path is not None:
            args += ["--persona", self._container_path(request.persona_path)]
        if request.brief_path is not None:
            args += ["--brief", self._container_path(request.brief_path)]
        if request.memory:
            args += ["--memory"]
        if request.wake_phrase is not None:
            args += ["--wake-phrase", request.wake_phrase]
        if request.barge_in:
            args += ["--barge-in"]
        return args

    def _container_path(self, path: Path, *, must_exist: bool = True) -> str:
        resolved = (self._project_dir / path).resolve()
        for host_dir, container_dir in (
            (self._project_dir / ".runtime" / "audio", ".runtime"),
            (self._project_dir / "examples", "examples"),
        ):
            try:
                relative = resolved.relative_to(host_dir.resolve())
            except ValueError:
                continue
            if must_exist and not (host_dir / relative).exists():
                raise WorkerError(f"file not found: {path}")
            return f"{container_dir}/{relative.as_posix()}"
        raise WorkerError(f"file is not visible inside the worker container: {path}")
