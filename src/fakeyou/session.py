import dataclasses
from dataclasses import field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from fakeyou.invariants import PositiveSeconds
from fakeyou.invariants import SessionId as SessionId
from fakeyou.invariants import TwinName as TwinName
from fakeyou.lifecycle import MeetingPhase, TerminalReason
from fakeyou.settings import DEFAULTS


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
    owner_id: SessionId
    twin_id: SessionId
    meeting_id: SessionId
    link: str
    twin_name: TwinName
    lobby_timeout_seconds: PositiveSeconds = DEFAULTS.meeting.lobby_timeout_seconds
    join_poll_interval_seconds: PositiveSeconds = DEFAULTS.meeting.join_poll_seconds
    session_duration_seconds: PositiveSeconds = DEFAULTS.meeting.session_duration_seconds
    monitor_interval_seconds: PositiveSeconds = DEFAULTS.meeting.monitor_poll_seconds
    leave_timeout_seconds: PositiveSeconds = DEFAULTS.meeting.leave_timeout_seconds
    deaf_window_seconds: PositiveSeconds = DEFAULTS.audio_health.deaf_window_seconds
    deaf_grace_seconds: PositiveSeconds = DEFAULTS.audio_health.deaf_grace_seconds
    preflight_budget_seconds: PositiveSeconds = DEFAULTS.preflight.budget_seconds
    checkpoint_path: Path = DEFAULTS.paths.checkpoint_path
    browser_profile_dir: Path | None = None
    warmup_browser: bool = DEFAULTS.browser.warmup
    headless: bool = DEFAULTS.browser.headless
    license_key: str | None = field(default=None, repr=False)


@dataclasses.dataclass(frozen=True, slots=True)
class SessionResult:
    phase: MeetingPhase
    terminal_reason: TerminalReason | None
    detail: str
    faults: dict[str, Any] | None = None
