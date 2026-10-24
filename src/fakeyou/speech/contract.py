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
