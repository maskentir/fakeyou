from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing

from fakeyou.foundation.protocols import SupportsAclose
from fakeyou.foundation.taxonomy import ProviderFaultKind
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.speech.contract import SpeechSynthesizer

LASTING_FAULTS: frozenset[ProviderFaultKind] = frozenset({"payment_required", "unauthorized"})


async def _relay(chunks: AsyncIterator[bytes]) -> AsyncGenerator[bytes]:
    try:
        async for chunk in chunks:
            yield chunk
    finally:
        if isinstance(chunks, SupportsAclose):
            await chunks.aclose()


class SpeechWithFallback:
    def __init__(
        self,
        primary: SpeechSynthesizer,
        fallback: SpeechSynthesizer,
        *,
        journal: Journal | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._journal = journal
        self._primary_lost = False

    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        def opened(synthesizer: SpeechSynthesizer) -> AsyncGenerator[bytes]:
            return _relay(
                synthesizer.stream(
                    text,
                    sentence_index=sentence_index,
                    transcript_id=transcript_id,
                    language=language,
                )
            )

        if not self._primary_lost:
            emitted = False
            try:
                async with aclosing(opened(self._primary)) as chunks:
                    async for chunk in chunks:
                        emitted = emitted or bool(chunk)
                        yield chunk
            except ProviderFault as fault:
                if emitted:
                    raise
                self._switch(fault, sentence_index)
            else:
                return
        async with aclosing(opened(self._fallback)) as chunks:
            async for chunk in chunks:
                yield chunk

    def _switch(self, fault: ProviderFault, sentence_index: int | None) -> None:
        lasting = fault.kind in LASTING_FAULTS
        self._primary_lost = self._primary_lost or lasting
        if self._journal is not None:
            self._journal.record(
                "tts_fallback_started",
                source="tts",
                kind=fault.kind,
                status=fault.status,
                lasting=lasting,
                sentence_index=sentence_index,
            )

    async def aclose(self) -> None:
        try:
            await self._primary.aclose()
        finally:
            await self._fallback.aclose()
