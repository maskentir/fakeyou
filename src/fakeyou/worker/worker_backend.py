import math
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from fakeyou.catalog.binaries import resolve_docker_binary
from fakeyou.catalog.commands import memory_ingest_args
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId, parse_session_id
from fakeyou.config.meeting_request import MeetingRequest, validate_meeting_request
from fakeyou.config.settings import DEFAULTS
from fakeyou.debrief.debrief import debrief_report_status
from fakeyou.foundation.invariants import LEAVE_BUDGET_SECONDS
from fakeyou.observability.cleanup import CLOSE_BUDGET_SECONDS
from fakeyou.observability.journal import journal_path
from fakeyou.observability.provider_faults import trim_detail
from fakeyou.observability.report import describe_session, read_journal
from fakeyou.persistence.ingestion_status import lock_path, read_status, status_path
from fakeyou.persistence.lease import describe_lease
from fakeyou.persistence.record import meeting_record_path

WORKER_SERVICE = "meeting-worker"
MANAGED_LABEL = "fakeyou.managed"
MEETING_LABEL = "fakeyou.meeting-id"
ROLE_LABEL = "fakeyou.role"
CONTAINER_PREFIX = "fakeyou-worker-"
INGEST_CONTAINER_PREFIX = "fakeyou-ingest-"
FINISH_CLOSE_COUNT = 4
RUNTIME_STACK_CLOSE_COUNT = 5
ASSEMBLY_STACK_CLOSE_COUNT = 2
FINISH_SEND_COUNT = 2
TEARDOWN_CLOSE_COUNT = FINISH_CLOSE_COUNT + RUNTIME_STACK_CLOSE_COUNT + ASSEMBLY_STACK_CLOSE_COUNT
STOP_GRACE_SECONDS = (
    TEARDOWN_CLOSE_COUNT * CLOSE_BUDGET_SECONDS
    + FINISH_SEND_COUNT * DEFAULTS.database.send_timeout_seconds
    + LEAVE_BUDGET_SECONDS
    + DEFAULTS.memory.close_hard_seconds
)
COMPOSE_RUNTIME_ROOT = ".runtime"
_HOST_AUDIO_MOUNT = "audio"
STATUS_POLL_SECONDS = 0.5

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class DockerTiming:
    sleep: Callable[[float], None] = time.sleep
    timeout_seconds: float = DEFAULTS.worker.docker_timeout_seconds
    start_timeout_seconds: float = DEFAULTS.worker.start_timeout_seconds


class WorkerError(FakeYouError):
    pass


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
        validate_meeting_request(
            MeetingRequest(
                link=self.link,
                lobby_timeout=self.lobby_timeout,
                duration=self.duration,
                initial_speech=self.initial_speech,
                audio=self.audio,
                conversation=self.conversation,
                memory=self.memory,
                persona_path=self.persona_path,
                brief_path=self.brief_path,
                meeting_id=self.meeting_id,
                wake_phrase=self.wake_phrase,
                barge_in=self.barge_in,
                owner_id=self.owner_id,
                twin_id=self.twin_id,
            )
        )


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
    ingestion: str | None = None
    debrief: str | None = None


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
    def ingest(
        self, meeting_id: str, *, persona_path: Path, owner_id: str | None, twin_id: str | None
    ) -> WorkerIngest: ...


def compose_override(meeting_id: SessionId, *, role: Literal["worker", "ingest"]) -> str:
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


def _valued_flag(flag: str, value: str | float | None) -> list[str]:
    return [] if value is None else [flag, str(value)]


def _bool_flag(flag: str, *, enabled: bool | None) -> list[str]:
    return [flag] if enabled else []


@dataclass(frozen=True, slots=True)
class ComposeMounts:
    project_dir: Path
    journal_dir: Path
    record_dir: Path

    @property
    def host_journal_dir(self) -> Path:
        return self._host_mounted(self.journal_dir)

    @property
    def host_record_dir(self) -> Path:
        return self._host_mounted(self.record_dir)

    def container_path(self, path: Path, *, must_exist: bool = True) -> str:
        resolved = (self.project_dir / path).resolve()
        mounts = [(root / _HOST_AUDIO_MOUNT, root.name) for root in self._mount_roots()]
        mounts.append((self.project_dir / "examples", "examples"))
        for host_dir, container_dir in mounts:
            try:
                relative = resolved.relative_to(host_dir.resolve())
            except ValueError:
                continue
            if must_exist and not (host_dir / relative).exists():
                raise WorkerError(ErrorCode.WORKER_FILE_MISSING, path=path)
            return f"{container_dir}/{relative.as_posix()}"
        raise WorkerError(ErrorCode.WORKER_FILE_NOT_VISIBLE, path=path)

    def _runtime_root(self, directory: Path) -> Path | None:
        try:
            relative = (
                (self.project_dir / directory).resolve().relative_to(self.project_dir.resolve())
            )
        except ValueError:
            return None
        if len(relative.parts) < 2:
            return None
        root = (self.project_dir / relative.parts[0]).resolve()
        if root != (self.project_dir / COMPOSE_RUNTIME_ROOT).resolve():
            return None
        return root

    def _host_mounted(self, directory: Path) -> Path:
        runtime_root = self._runtime_root(directory)
        if runtime_root is None:
            return directory
        resolved = (self.project_dir / directory).resolve()
        try:
            relative = resolved.relative_to(runtime_root)
        except ValueError:
            return directory
        if relative.parts and relative.parts[0] == _HOST_AUDIO_MOUNT:
            return directory
        return runtime_root / _HOST_AUDIO_MOUNT / relative

    def _mount_roots(self) -> list[Path]:
        roots: list[Path] = []
        for directory in (self.record_dir, self.journal_dir):
            root = self._runtime_root(directory)
            if root is not None and root not in roots:
                roots.append(root)
        return roots


class DockerWorkerBackend:
    def __init__(
        self,
        *,
        journal_dir: Path,
        record_dir: Path,
        project_dir: Path | None = None,
        runner: Runner | None = None,
        timing: DockerTiming | None = None,
    ) -> None:
        self._mounts = ComposeMounts(project_dir or Path.cwd(), journal_dir, record_dir)
        self._run: Runner = runner or subprocess.run
        self._timing = timing or DockerTiming()

    def start(self, request: WorkerStartRequest) -> WorkerStarted:
        session_id = parse_session_id(request.meeting_id, field="meeting_id")
        if self._running_container(session_id) is not None:
            raise WorkerError(ErrorCode.WORKER_ALREADY_RUNNING, meeting_id=session_id)
        if self._running_container(session_id, role="ingest") is not None:
            raise WorkerError(ErrorCode.WORKER_INGEST_ALREADY_RUNNING, meeting_id=session_id)
        join_args = self._join_args(request)
        if describe_session(self._mounts.host_journal_dir, session_id) == "unfinished":
            join_args.append("--resume")
        name = f"{CONTAINER_PREFIX}{session_id}"
        override: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yml", prefix="fakeyou-worker-", delete=False
            ) as handle:
                handle.write(compose_override(session_id, role="worker"))
                override = handle.name
            completed = self._run_docker(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(self._mounts.project_dir / "compose.yaml"),
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
                ],
                timeout_seconds=self._timing.start_timeout_seconds,
            )
        finally:
            if override is not None:
                Path(override).unlink(missing_ok=True)
        if completed.returncode != 0:
            raise WorkerError(ErrorCode.WORKER_START_FAILED, stderr=completed.stderr.strip())
        container_id = completed.stdout.strip()
        if not container_id:
            raise WorkerError(ErrorCode.WORKER_START_NO_CONTAINER)
        return WorkerStarted(
            meeting_id=session_id,
            container_id=container_id,
            container_name=name,
        )

    def status(self, meeting_id: str) -> WorkerState:
        session_id = parse_session_id(meeting_id, field="meeting_id")
        return self._state(session_id, self._find_container(session_id))

    def status_all(self) -> list[WorkerState]:
        completed = self._run_docker(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={MANAGED_LABEL}=true",
                "--filter",
                f"label={ROLE_LABEL}=worker",
                "--format",
                '{{.ID}}\t{{.State}}\t{{.Names}}\t{{.Label "fakeyou.meeting-id"}}',
            ]
        )
        if completed.returncode != 0:
            raise WorkerError(ErrorCode.DOCKER_PS_FAILED, stderr=completed.stderr.strip())
        states: dict[str, WorkerState] = {}
        for line in completed.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or not parts[0] or not parts[3]:
                continue
            try:
                session_id = parse_session_id(parts[3], field="meeting_id")
            except FakeYouError:
                continue
            states[session_id] = self._state(session_id, (parts[0], parts[1], parts[2]))
        for meeting_id in self._journal_meeting_ids():
            if meeting_id not in states:
                states[meeting_id] = self._state(meeting_id, None)
        return [states[meeting_id] for meeting_id in sorted(states)]

    def stop(self, meeting_id: str, *, timeout_seconds: float) -> WorkerStopped:
        session_id = parse_session_id(meeting_id, field="meeting_id")
        if timeout_seconds < 0:
            raise WorkerError(ErrorCode.WORKER_TIMEOUT_NEGATIVE)
        container_id = self._running_container(session_id)
        if container_id is None:
            summary = summarize_session(self._read_session(session_id))
            if summary["phase"] == "ended":
                return WorkerStopped(
                    meeting_id=session_id,
                    container_id=None,
                    terminal_reason=summary["terminal_reason"],
                    escalated=False,
                )
            raise WorkerError(ErrorCode.WORKER_NOT_RUNNING, meeting_id=session_id)
        grace = min(int(STOP_GRACE_SECONDS), math.ceil(timeout_seconds))
        self._run_docker(
            ["docker", "stop", "--time", str(grace), container_id],
            timeout_seconds=grace + self._timing.timeout_seconds,
        )
        deadline = time.monotonic() + timeout_seconds
        terminal_reason: str | None = None
        while True:
            summary = summarize_session(self._read_session(session_id))
            if summary["phase"] == "ended":
                terminal_reason = summary["terminal_reason"]
                break
            if time.monotonic() >= deadline:
                break
            self._timing.sleep(STATUS_POLL_SECONDS)
        if terminal_reason is None and self._running_container(session_id) == container_id:
            self._run_docker(["docker", "kill", container_id])
            return WorkerStopped(
                meeting_id=session_id,
                container_id=container_id,
                terminal_reason=None,
                escalated=True,
            )
        return WorkerStopped(
            meeting_id=session_id,
            container_id=container_id,
            terminal_reason=terminal_reason,
            escalated=False,
        )

    def ingest(
        self,
        meeting_id: str,
        *,
        persona_path: Path,
        owner_id: str | None = None,
        twin_id: str | None = None,
    ) -> WorkerIngest:
        session_id = parse_session_id(meeting_id, field="meeting_id")
        owner = parse_session_id(owner_id, field="--owner-id") if owner_id is not None else None
        twin = parse_session_id(twin_id, field="--twin-id") if twin_id is not None else None
        if self._running_container(session_id) is not None:
            raise WorkerError(ErrorCode.WORKER_STILL_RUNNING, meeting_id=session_id)
        if self._running_container(session_id, role="ingest") is not None:
            raise WorkerError(ErrorCode.WORKER_INGEST_ALREADY_RUNNING, meeting_id=session_id)
        if describe_session(self._mounts.host_journal_dir, session_id) != "finished":
            raise WorkerError(ErrorCode.WORKER_INGEST_NO_FINISHED_SESSION, meeting_id=session_id)
        record = meeting_record_path(self._mounts.host_record_dir, session_id)
        lease = describe_lease(lock_path(record))
        if lease == "live":
            raise WorkerError(ErrorCode.RECORD_LOCKED, path=lock_path(record))
        if lease == "corrupt":
            raise WorkerError(ErrorCode.RECORD_LOCK_CORRUPT, path=lock_path(record))
        ingest_args = memory_ingest_args(
            meeting_id=session_id,
            persona=self._mounts.container_path(persona_path),
            owner_id=owner,
            twin_id=twin,
        )
        name = f"{INGEST_CONTAINER_PREFIX}{session_id}"
        status_file = status_path(record)
        before = self._status_fingerprint(status_file)
        override: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yml", prefix="fakeyou-ingest-", delete=False
            ) as handle:
                handle.write(compose_override(session_id, role="ingest"))
                override = handle.name
            try:
                completed = self._run(
                    [
                        resolve_docker_binary(),
                        "compose",
                        "-f",
                        str(self._mounts.project_dir / "compose.yaml"),
                        "-f",
                        override,
                        "run",
                        "--rm",
                        "--name",
                        name,
                        WORKER_SERVICE,
                        *ingest_args,
                    ]
                )
            except KeyboardInterrupt:
                raise WorkerError(
                    ErrorCode.WORKER_INGEST_INTERRUPTED, meeting_id=session_id
                ) from None
            except OSError as error:
                reason = trim_detail(f"{type(error).__name__}: {error}") or type(error).__name__
                raise WorkerError(ErrorCode.DOCKER_UNAVAILABLE, reason=reason) from None
        finally:
            if override is not None:
                Path(override).unlink(missing_ok=True)
        return WorkerIngest(
            meeting_id=session_id,
            status=self._ingest_outcome(status_file, before, succeeded=completed.returncode == 0),
        )

    def _ingest_outcome(
        self, status_file: Path, before: tuple[int, int] | None, *, succeeded: bool
    ) -> str:
        if not succeeded and self._status_fingerprint(status_file) == before:
            return "unknown"
        try:
            existing = read_status(status_file)
        except FakeYouError as error:
            if error.code is not ErrorCode.INGESTION_STATUS_CORRUPT:
                raise
            return "unknown"
        if existing is None:
            return "completed" if succeeded else "unknown"
        return existing.status

    def _status_fingerprint(self, status_file: Path) -> tuple[int, int] | None:
        try:
            stat = status_file.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _run_docker(
        self,
        args: list[str],
        *,
        timeout_seconds: float | None = None,
        unbounded: bool = False,
    ) -> "subprocess.CompletedProcess[str]":
        resolved = [resolve_docker_binary(), *args[1:]]
        if unbounded:
            timeout: float | None = None
        elif timeout_seconds is None:
            timeout = self._timing.timeout_seconds
        else:
            timeout = timeout_seconds
        try:
            return self._run(
                resolved,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            reason = trim_detail(f"{type(error).__name__}: {error}") or type(error).__name__
            raise WorkerError(ErrorCode.DOCKER_UNAVAILABLE, reason=reason) from None
        except OSError as error:
            reason = trim_detail(f"{type(error).__name__}: {error}") or type(error).__name__
            raise WorkerError(ErrorCode.DOCKER_UNAVAILABLE, reason=reason) from None

    def _state(self, meeting_id: SessionId, found: tuple[str, str, str] | None) -> WorkerState:
        summary = summarize_session(self._read_session(meeting_id))
        record = meeting_record_path(self._mounts.host_record_dir, meeting_id)
        status_file = status_path(record)
        try:
            existing = read_status(status_file)
        except FakeYouError as error:
            if error.code is not ErrorCode.INGESTION_STATUS_CORRUPT:
                raise
            existing = None
        if existing is not None:
            ingestion: str | None = existing.status
        else:
            ingestion = "invalid" if status_file.exists() else None
        debrief = debrief_report_status(record)
        if found is None:
            return WorkerState(
                meeting_id=meeting_id,
                container_id=None,
                container_name=None,
                container_state="absent",
                phase=summary["phase"],
                terminal_reason=summary["terminal_reason"],
                last_fault=summary["last_fault"],
                ingestion=ingestion,
                debrief=debrief,
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
            ingestion=ingestion,
            debrief=debrief,
        )

    def _read_session(self, meeting_id: SessionId) -> list[dict[str, Any]]:
        return read_journal(journal_path(self._mounts.host_journal_dir, meeting_id))

    def _journal_meeting_ids(self) -> list[SessionId]:
        directory = self._mounts.host_journal_dir
        if not directory.is_dir():
            return []
        meeting_ids: list[SessionId] = []
        for path in directory.glob("*.jsonl"):
            try:
                meeting_ids.append(parse_session_id(path.stem, field="meeting_id"))
            except FakeYouError:
                continue
        return meeting_ids

    def _running_container(
        self, meeting_id: SessionId, *, role: Literal["worker", "ingest"] = "worker"
    ) -> str | None:
        completed = self._run_docker(
            [
                "docker",
                "ps",
                "--filter",
                f"label={MEETING_LABEL}={meeting_id}",
                "--filter",
                f"label={ROLE_LABEL}={role}",
                "--format",
                "{{.ID}}",
            ]
        )
        if completed.returncode != 0:
            raise WorkerError(ErrorCode.DOCKER_PS_FAILED, stderr=completed.stderr.strip())
        for line in completed.stdout.splitlines():
            if line.strip():
                return line.strip()
        return None

    def _find_container(self, meeting_id: SessionId) -> tuple[str, str, str] | None:
        completed = self._run_docker(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={MEETING_LABEL}={meeting_id}",
                "--filter",
                f"label={ROLE_LABEL}=worker",
                "--format",
                "{{.ID}}\t{{.State}}\t{{.Names}}",
            ]
        )
        if completed.returncode != 0:
            raise WorkerError(ErrorCode.DOCKER_PS_FAILED, stderr=completed.stderr.strip())
        for line in completed.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0]:
                return parts[0], parts[1], parts[2]
        return None

    def _path_flag(
        self, flag: str, path: Path | str | None, *, must_exist: bool = True
    ) -> list[str]:
        if path is None:
            return []
        resolved = path if isinstance(path, Path) else Path(path)
        return [flag, self._mounts.container_path(resolved, must_exist=must_exist)]

    def _join_args(self, request: WorkerStartRequest) -> list[str]:
        return [
            "join",
            request.link,
            "--meeting-id",
            request.meeting_id,
            *_valued_flag("--twin-name", request.twin_name),
            *_valued_flag("--owner-id", request.owner_id),
            *_valued_flag("--twin-id", request.twin_id),
            *_valued_flag("--lobby-timeout", request.lobby_timeout),
            *_valued_flag("--duration", request.duration),
            *self._path_flag("--profile-dir", request.profile_dir, must_exist=False),
            *_bool_flag("--headless", enabled=request.headless),
            *_bool_flag("--audio", enabled=request.audio),
            *_valued_flag("--speak", request.initial_speech),
            *_bool_flag("--converse", enabled=request.conversation),
            *self._path_flag("--persona", request.persona_path),
            *self._path_flag("--brief", request.brief_path),
            *_bool_flag("--memory", enabled=request.memory),
            *_valued_flag("--wake-phrase", request.wake_phrase),
            *_bool_flag("--barge-in", enabled=request.barge_in),
        ]
