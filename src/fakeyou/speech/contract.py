from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class WordTiming:
    text: str
    start: float
    end: float


class SpeechSegment(Protocol):
    @property
    def words(self) -> Sequence[WordTiming]: ...

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

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def __anext__(self) -> bytes: ...

    async def aclose(self) -> None: ...
