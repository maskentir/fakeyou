import dataclasses
from dataclasses import field
from pathlib import Path
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass

from fakeyou.config.identifiers import SessionIdField
from fakeyou.config.settings import DEFAULTS
from fakeyou.foundation.invariants import PositiveSeconds, TwinName
from fakeyou.foundation.taxonomy import MeetingPhase, TerminalReason


class MeetingAudio(Protocol):
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None: ...

    async def stop_playback(self) -> None: ...

    @property
    def is_speaking(self) -> bool: ...

    async def stop(self) -> None: ...

    def raise_if_failed(self) -> None: ...

    async def wait_failed(self) -> None: ...


@runtime_checkable
class SupportsMaintenance(Protocol):
    async def maintenance_tick(
        self, *, is_busy: bool, remote_participants: int | None = None
    ) -> str: ...


@runtime_checkable
class SupportsAdmission(Protocol):
    def mark_admitted(self) -> None: ...


class ConversationError(RuntimeError):
    pass


class MeetingConversation(Protocol):
    @property
    def is_busy(self) -> bool: ...

    def bind(self, session: MeetingAudio) -> None: ...

    async def close(self) -> None: ...

    def raise_if_failed(self) -> None: ...


@dataclass(frozen=True, slots=True, config=ConfigDict(hide_input_in_errors=True))
class SessionConfig:
    owner_id: SessionIdField
    twin_id: SessionIdField
    meeting_id: SessionIdField
    link: str
    twin_name: TwinName
    lobby_timeout_seconds: PositiveSeconds = DEFAULTS.meeting.lobby_timeout_seconds
    join_poll_interval_seconds: PositiveSeconds = DEFAULTS.meeting.join_poll_seconds
    join_max_attempts: Annotated[int, Field(ge=1)] = DEFAULTS.meeting.join_max_attempts
    join_retry_base_seconds: PositiveSeconds = DEFAULTS.meeting.join_retry_base_seconds
    join_retry_cap_seconds: PositiveSeconds = DEFAULTS.meeting.join_retry_cap_seconds
    session_duration_seconds: PositiveSeconds = DEFAULTS.meeting.session_duration_seconds
    monitor_interval_seconds: PositiveSeconds = DEFAULTS.meeting.monitor_poll_seconds
    leave_timeout_seconds: PositiveSeconds = DEFAULTS.meeting.leave_timeout_seconds
    deaf_window_seconds: PositiveSeconds = DEFAULTS.audio_health.deaf_window_seconds
    deaf_grace_seconds: PositiveSeconds = DEFAULTS.audio_health.deaf_grace_seconds
    preflight_budget_seconds: PositiveSeconds = DEFAULTS.preflight.budget_seconds
    database_url: str | None = field(default=None, repr=False)
    database_connect_timeout_seconds: PositiveSeconds = DEFAULTS.database.connect_timeout_seconds
    database_statement_timeout_seconds: PositiveSeconds = (
        DEFAULTS.database.statement_timeout_seconds
    )
    database_keepalive_idle_seconds: PositiveSeconds = DEFAULTS.database.keepalive_idle_seconds
    lifecycle_send_timeout_seconds: PositiveSeconds = DEFAULTS.database.send_timeout_seconds
    drain_max_consecutive_failures: Annotated[int, Field(ge=1)] = (
        DEFAULTS.audio.drain_max_consecutive_failures
    )
    drain_failure_journal_every: Annotated[int, Field(ge=1)] = (
        DEFAULTS.audio.drain_failure_journal_every
    )
    participant_evidence_max_bytes: Annotated[int, Field(ge=1)] = (
        DEFAULTS.conversation.participant_evidence_max_bytes
    )
    browser_profile_dir: Path | None = None
    warmup_browser: bool = DEFAULTS.browser.warmup
    headless: bool = DEFAULTS.browser.headless
    license_key: str | None = field(default=None, repr=False)
    participant_policy_enabled: bool = False
    participant_evidence_path: Path | None = None
    attribution_probe_path: Path | None = None
    per_stream_enabled: bool = False
    meet_actions_enabled: bool = False
    visual_enabled: bool = False
    resume: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class SessionResult:
    phase: MeetingPhase
    terminal_reason: TerminalReason | None
    detail: str
    faults: dict[str, Any] | None = None
