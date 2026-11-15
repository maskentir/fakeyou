from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from fakeyou.foundation.publication import PublicationContext, SentenceBinding


@dataclass(frozen=True, slots=True)
class WordTiming:
    text: str
    start: float
    end: float


class SpeechSegment(Protocol):
    @property
    def words(self) -> Sequence[WordTiming] | None: ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def __anext__(self) -> bytes: ...

    async def aclose(self) -> None: ...


class SpeechTurn(Protocol):
    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment: ...

    async def finish(self) -> None: ...

    async def cancel(self) -> None: ...


class SpeechSynthesizer(Protocol):
    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class SupportsWarmup(Protocol):
    def warm(self) -> None: ...


class PreparedSpeech(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def language(self) -> str | None: ...

    @property
    def sentence_index(self) -> int | None: ...

    @property
    def transcript_id(self) -> str | None: ...

    @property
    def words(self) -> Sequence[WordTiming] | None: ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def __anext__(self) -> bytes: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class PublicationSpeaker(Protocol):
    @property
    def publication_context(self) -> PublicationContext | None: ...

    def bind_publication(self, prepared: PreparedSpeech, binding: SentenceBinding) -> None: ...


PlaybackOutcome = Literal[
    "attempted",
    "prevented",
    "dropped_before_audio",
    "audio_started",
    "interrupted_after_audio",
    "completed",
]
_AUDIO_WRITTEN: frozenset[PlaybackOutcome] = frozenset(
    {"audio_started", "interrupted_after_audio", "completed"}
)
_SETTLED: frozenset[PlaybackOutcome] = frozenset(
    {"prevented", "dropped_before_audio", "interrupted_after_audio", "completed"}
)


@dataclass(frozen=True, slots=True)
class PlaybackReceipt:
    attempt_id: str
    outcome: PlaybackOutcome
    text: str
    cue: bool = False
    detail: str = ""
    local_audio_seconds: float = 0.0
    publication_id: str | None = None
    transcript_id: str | None = None
    sentence_index: int | None = None

    @property
    def local_audio_written(self) -> bool:
        return self.outcome in _AUDIO_WRITTEN

    @property
    def settled(self) -> bool:
        return self.outcome in _SETTLED


ReceiptObserver = Callable[[PlaybackReceipt], None]


@runtime_checkable
class ReceiptSource(Protocol):
    def observe_receipts(self, observer: ReceiptObserver) -> None: ...


@runtime_checkable
class NoVisualSpeaker(Protocol):
    async def speak_no_visual(
        self,
        text: str,
        *,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None: ...


class DirectSpeaker(Protocol):
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None: ...


async def speak_no_visual(
    speaker: DirectSpeaker,
    text: str,
    *,
    transcript_id: str | None = None,
    language: str | None = None,
) -> None:
    if isinstance(speaker, NoVisualSpeaker):
        await speaker.speak_no_visual(text, transcript_id=transcript_id, language=language)
        return
    await speaker.speak(text, transcript_id=transcript_id, language=language)
