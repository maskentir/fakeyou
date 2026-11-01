from collections.abc import AsyncIterator
from typing import Protocol


class SpeechSynthesizer(Protocol):
    def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


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
