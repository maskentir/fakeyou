import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from fakeyou.config.identifiers import SessionIdField
from fakeyou.config.settings import (
    AttentionSettings,
    AudioHealthSettings,
    AudioSettings,
    BrowserSettings,
    ConversationSettings,
    MeetingSettings,
    PreflightSettings,
)
from fakeyou.foundation.invariants import TwinName
from fakeyou.foundation.taxonomy import PublicPhase, TerminalReason
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.triggers import ReplyChannel
from fakeyou.speech.contract import PreparedSpeech


class MeetingAudio(Protocol):
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None: ...

    async def prepare(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech | None: ...

    async def speak_prepared(self, prepared: PreparedSpeech) -> None: ...

    async def stop_playback(self) -> None: ...

    @property
    def is_speaking(self) -> bool: ...

    async def stop(self) -> None: ...

    def raise_if_failed(self) -> None: ...

    async def wait_failed(self) -> None: ...


@runtime_checkable
class SupportsCues(Protocol):
    def prepare_cues(self, texts: Sequence[str]) -> None: ...

    async def play_cue(self, text: str) -> bool: ...


@runtime_checkable
class SupportsLouder(Protocol):
    def louder(self) -> None: ...


@runtime_checkable
class SupportsDucking(Protocol):
    def duck(self) -> None: ...

    def unduck(self) -> None: ...


@runtime_checkable
class SupportsFreezing(Protocol):
    def freeze(self) -> None: ...

    def thaw(self) -> None: ...


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

    def receive_chat(self, event: TranscriptEvent) -> bool: ...

    def receive_attention(self, event: TranscriptEvent, *, channel: ReplyChannel) -> bool: ...

    def observe_contribution_spoken(self, observer: Callable[[str], None]) -> None: ...

    def bind(self, session: MeetingAudio) -> None: ...

    async def close(self) -> None: ...

    def raise_if_failed(self) -> None: ...

    async def pause(self) -> None: ...

    def resume(self) -> None: ...


@dataclass(frozen=True, slots=True, config=ConfigDict(hide_input_in_errors=True))
class SessionConfig:
    owner_id: SessionIdField
    twin_id: SessionIdField
    meeting_id: SessionIdField
    link: str
    twin_name: TwinName
    meeting: MeetingSettings = field(default_factory=MeetingSettings)
    audio_health: AudioHealthSettings = field(default_factory=AudioHealthSettings)
    preflight: PreflightSettings = field(default_factory=PreflightSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    conversation: ConversationSettings = field(default_factory=ConversationSettings)
    browser_profile_dir: Path | None = None
    participant_policy_enabled: bool = True
    participant_evidence_enabled: bool = False
    participant_evidence_path: Path | None = None
    meet_actions_enabled: bool = False
    perception_enabled: bool = False
    attention: AttentionSettings = field(default_factory=lambda: AttentionSettings(enabled=False))
    resume: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class SessionResult:
    phase: PublicPhase
    terminal_reason: TerminalReason | None
    detail: str
    faults: dict[str, Any] | None = None
