from dataclasses import field
from pathlib import Path
from typing import Annotated, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, StringConstraints
from pydantic.dataclasses import dataclass

DEFAULT_CHECKPOINT_PATH = Path(".runtime/checkpoints.sqlite")

SessionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
TwinName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class MeetingAudio(Protocol):
    async def speak(self, text: str) -> None: ...

    async def stop_playback(self) -> None: ...

    @property
    def is_speaking(self) -> bool: ...

    async def stop(self) -> None: ...

    def raise_if_failed(self) -> None: ...

    async def wait_failed(self) -> None: ...


@runtime_checkable
class AudioMaintenance(Protocol):
    async def maintenance_tick(
        self, *, is_busy: bool, remote_participants: int | None = None
    ) -> str: ...


@runtime_checkable
class AdmissionAware(Protocol):
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
    lobby_timeout_seconds: PositiveSeconds = 300.0
    join_poll_interval_seconds: PositiveSeconds = 2.0
    session_duration_seconds: PositiveSeconds = 7200.0
    monitor_interval_seconds: PositiveSeconds = 5.0
    leave_timeout_seconds: PositiveSeconds = 30.0
    deaf_window_seconds: PositiveSeconds = 120.0
    deaf_grace_seconds: PositiveSeconds = 120.0
    preflight_budget_seconds: PositiveSeconds = 5.0
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH
    browser_profile_dir: Path | None = None
    warmup_browser: bool = True
    headless: bool = False
    license_key: str | None = field(default=None, repr=False)
