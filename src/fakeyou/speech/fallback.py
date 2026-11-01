from collections.abc import AsyncIterator, Sequence

from fakeyou.foundation.taxonomy import ProviderFaultKind
from fakeyou.observability.cleanup import close_safely
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.speech.contract import (
    SpeechSegment,
    SpeechSynthesizer,
    SpeechTurn,
    SupportsWarmup,
    WordTiming,
)

LASTING_FAULTS: frozenset[ProviderFaultKind] = frozenset({"payment_required", "unauthorized"})


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

    def warm(self) -> None:
        if isinstance(self._primary, SupportsWarmup):
            self._primary.warm()

    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        return _FallbackTurn(self, transcript_id=transcript_id, language=language)

    def primary_turn(self, transcript_id: str | None, language: str | None) -> SpeechTurn | None:
        if self._primary_lost:
            return None
        return self._primary.open_turn(transcript_id=transcript_id, language=language)

    def fallback_turn(self, transcript_id: str | None, language: str | None) -> SpeechTurn:
        return self._fallback.open_turn(transcript_id=transcript_id, language=language)

    def record_switch(
        self, fault: ProviderFault, sentence_index: int | None, *, mid_sentence: bool
    ) -> None:
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
                mid_sentence=mid_sentence,
            )

    async def aclose(self) -> None:
        try:
            await self._primary.aclose()
        finally:
            await self._fallback.aclose()


class _FallbackTurn:
    def __init__(
        self,
        owner: SpeechWithFallback,
        *,
        transcript_id: str | None,
        language: str | None,
    ) -> None:
        self._owner = owner
        self._transcript_id = transcript_id
        self._language = language
        self._primary = owner.primary_turn(transcript_id, language)
        self._fallback: SpeechTurn | None = None
        self._abandoned: list[SpeechTurn] = []
        self._late: list[SpeechTurn] = []
        self._ending: bool | None = None

    @property
    def on_primary(self) -> bool:
        return self._primary is not None

    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        if self._primary is None:
            return _FallbackSegment(
                self, text, sentence_index, self.backup().speak(text, sentence_index=sentence_index)
            )
        return _FallbackSegment(
            self,
            text,
            sentence_index,
            self._primary.speak(text, sentence_index=sentence_index),
            on_primary=True,
        )

    def backup(self) -> SpeechTurn:
        if self._fallback is None:
            self._fallback = self._owner.fallback_turn(self._transcript_id, self._language)
            if self._ending is not None:
                self._late.append(self._fallback)
        return self._fallback

    def abandon(
        self, fault: ProviderFault, sentence_index: int | None, *, mid_sentence: bool
    ) -> None:
        self._owner.record_switch(fault, sentence_index, mid_sentence=mid_sentence)
        if self._primary is not None:
            self._abandoned.append(self._primary)
            self._primary = None

    async def finish(self) -> None:
        await self._close(cancel=False)

    async def cancel(self) -> None:
        await self._close(cancel=True)

    async def settle(self) -> None:
        if self._ending is None:
            return
        late, self._late = self._late, []
        for turn in late:
            await _end(turn, cancel=self._ending)

    async def _close(self, *, cancel: bool) -> None:
        self._ending = cancel
        abandoned, self._abandoned, self._late = self._abandoned, [], []
        for dropped in abandoned:
            await _end(dropped, cancel=True)
        for live in (self._primary, self._fallback):
            if live is not None:
                await _end(live, cancel=cancel)


async def _end(turn: SpeechTurn, *, cancel: bool) -> None:
    await close_safely(turn.cancel if cancel else turn.finish, source="tts", operation="end_turn")


class _FallbackSegment:
    def __init__(
        self,
        turn: _FallbackTurn,
        text: str,
        sentence_index: int | None,
        segment: SpeechSegment,
        *,
        on_primary: bool = False,
    ) -> None:
        self._turn = turn
        self._text = text
        self._sentence_index = sentence_index
        self._current = segment
        self._on_primary = on_primary
        self._emitted = False

    @property
    def words(self) -> Sequence[WordTiming]:
        return self._current.words

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        while True:
            if self._on_primary and not self._emitted and not self._turn.on_primary:
                await self._reroute()
            try:
                chunk = await anext(self._current)
            except StopAsyncIteration:
                await self._turn.settle()
                raise
            except ProviderFault as fault:
                if not self._on_primary:
                    raise
                self._turn.abandon(fault, self._sentence_index, mid_sentence=self._emitted)
                if self._emitted:
                    await self.aclose()
                    raise StopAsyncIteration from None
                await self._reroute()
                continue
            self._emitted = self._emitted or bool(chunk)
            return chunk

    async def _reroute(self) -> None:
        failed = self._current
        self._on_primary = False
        await close_safely(failed.aclose, source="tts", operation="close_segment")
        self._current = self._turn.backup().speak(self._text, sentence_index=self._sentence_index)

    async def aclose(self) -> None:
        self._on_primary = False
        await close_safely(self._current.aclose, source="tts", operation="close_segment")
        await self._turn.settle()
