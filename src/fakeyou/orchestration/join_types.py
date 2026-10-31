from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from graphiti_core import Graphiti

from fakeyou.catalog.errors import ErrorCode
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig, SessionResult
from fakeyou.foundation.taxonomy import PublicPhase
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.orchestration.runtime import SessionWiring


@dataclass(frozen=True, slots=True)
class JoinRequest:
    link: str
    settings: Settings
    identity: OperatorIdentity
    audio_enabled: bool
    twin_name: str | None = None
    meeting_id: str | None = None
    lobby_timeout: float | None = None
    duration: float | None = None
    profile_dir: str | None = None
    headless: bool | None = None
    initial_speech: str | None = None
    memory: bool = False
    conversation_enabled: bool = False
    wake_phrase: str | None = None
    barge_in: bool = False
    brief_path: Path | None = None
    resume: bool = False
    on_phase: Callable[[PublicPhase], None] | None = None
    attempt_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class JoinPorts:
    run_session: Callable[[SessionConfig, SessionWiring], Awaitable[SessionResult]]
    open_memory: Callable[[Settings], AbstractAsyncContextManager[Graphiti]]


@dataclass(frozen=True, slots=True)
class IngestResume:
    meeting_id: SessionId
    persona: Path
    owner_id: SessionId
    twin_id: SessionId


@dataclass(frozen=True, slots=True)
class JoinOutcome:
    meeting_id: SessionId
    session: SessionResult
    error_code: ErrorCode | None
    session_completed: bool
    ingestion_outcome: str
    record_error: str | None
    brief_sha256: str | None
    context_path: Path | None
    ingest_resume: IngestResume | None
    exit_code: ExitCode
