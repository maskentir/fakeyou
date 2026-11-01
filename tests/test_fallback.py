from collections.abc import AsyncIterator, Sequence
from typing import Any, override

import pytest
from pydantic import SecretStr

from fakeyou.config.settings import CartesiaTTS, OpenAITTS, Settings
from fakeyou.foundation.taxonomy import ProviderFaultKind
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault
from fakeyou.speech.contract import SpeechSegment, SpeechTurn, WordTiming
from fakeyou.speech.fallback import SpeechWithFallback
from fakeyou.speech.registry import build_speech_synthesizer
from tests._persistence_fakes import RecordingJournal
from tests._prepared_support import SentenceSynth

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}


class ScriptedSynth(SentenceSynth):
    def __init__(
        self,
        audio: bytes,
        *,
        fault: ProviderFaultKind | None = None,
        status: int | None = None,
        fail_after_audio: bool = False,
    ) -> None:
        self.audio = audio
        self.fault = (
            None
            if fault is None
            else ProviderFault("tts", fault, evidence=FaultEvidence(status=status))
        )
        self.fail_after_audio = fail_after_audio
        self.texts: list[str] = []
        self.closed = False

    @override
    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del sentence_index, transcript_id, language
        self.texts.append(text)
        if self.fault is not None and not self.fail_after_audio:
            raise self.fault
        yield self.audio
        if self.fault is not None:
            raise self.fault

    async def aclose(self) -> None:
        self.closed = True


async def _spoken(speech: SpeechWithFallback, text: str, index: int) -> bytes:
    return await _said(speech.open_turn(), text, index)


async def _said(turn: SpeechTurn, text: str, index: int) -> bytes:
    return b"".join([chunk async for chunk in turn.speak(text, sentence_index=index)])


async def test_spent_credit_moves_the_rest_of_the_session_to_the_fallback() -> None:
    primary = ScriptedSynth(b"primary", fault="payment_required", status=402)
    fallback = ScriptedSynth(b"fallback")
    journal = RecordingJournal()
    speech = SpeechWithFallback(primary, fallback, journal=journal)

    first = await _spoken(speech, "First sentence.", 0)
    second = await _spoken(speech, "Second sentence.", 1)

    assert (first, second) == (b"fallback", b"fallback")
    assert primary.texts == ["First sentence."]
    assert fallback.texts == ["First sentence.", "Second sentence."]
    [used] = journal.events("tts_fallback_started")
    assert used["kind"] == "payment_required"
    assert used["status"] == 402
    assert used["lasting"] is True
    assert used["sentence_index"] == 0


async def test_a_passing_outage_falls_back_for_one_turn_and_tries_the_primary_again() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", status=503)
    fallback = ScriptedSynth(b"fallback")
    journal = RecordingJournal()
    speech = SpeechWithFallback(primary, fallback, journal=journal)

    await _spoken(speech, "First sentence.", 0)
    await _spoken(speech, "Second sentence.", 1)

    assert primary.texts == ["First sentence.", "Second sentence."]
    assert [event["lasting"] for event in journal.events("tts_fallback_started")] == [False, False]


async def test_the_rest_of_a_turn_stays_on_the_fallback_after_an_outage() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", status=503)
    fallback = ScriptedSynth(b"fallback")
    speech = SpeechWithFallback(primary, fallback)
    turn = speech.open_turn()

    first = await _said(turn, "First sentence.", 0)
    second = await _said(turn, "Second sentence.", 1)
    await turn.finish()

    assert (first, second) == (b"fallback", b"fallback")
    assert primary.texts == ["First sentence."]
    assert fallback.texts == ["First sentence.", "Second sentence."]


async def test_a_fault_after_audio_ends_the_sentence_and_moves_the_turn() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", fail_after_audio=True)
    fallback = ScriptedSynth(b"fallback")
    journal = RecordingJournal()
    speech = SpeechWithFallback(primary, fallback, journal=journal)
    turn = speech.open_turn()

    first = await _said(turn, "First sentence.", 0)
    second = await _said(turn, "Second sentence.", 1)

    assert (first, second) == (b"primary", b"fallback")
    assert fallback.texts == ["Second sentence."]
    [switch] = journal.events("tts_fallback_started")
    assert switch["mid_sentence"] is True
    assert switch["sentence_index"] == 0


async def test_a_fault_on_the_fallback_itself_is_raised() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", status=503)
    fallback = ScriptedSynth(b"fallback", fault="unavailable", status=503)
    speech = SpeechWithFallback(primary, fallback)

    with pytest.raises(ProviderFault):
        await _spoken(speech, "First sentence.", 0)


async def test_a_healthy_primary_never_touches_the_fallback() -> None:
    primary = ScriptedSynth(b"primary")
    fallback = ScriptedSynth(b"fallback")
    speech = SpeechWithFallback(primary, fallback)

    assert await _spoken(speech, "First sentence.", 0) == b"primary"
    await speech.aclose()

    assert fallback.texts == []
    assert primary.closed
    assert fallback.closed


async def test_the_registry_wraps_the_primary_only_when_a_fallback_is_configured() -> None:
    primary = OpenAITTS(api_key=SecretStr("k"))
    fallback = CartesiaTTS(api_key=SecretStr("k"))

    alone = build_speech_synthesizer(primary, language="id")
    wrapped = build_speech_synthesizer(primary, language="id", fallback=fallback)

    assert not isinstance(alone, SpeechWithFallback)
    assert isinstance(wrapped, SpeechWithFallback)
    await alone.aclose()
    await wrapped.aclose()


def test_the_fallback_provider_is_selected_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKEYOU_TTS_FALLBACK__PROVIDER", "cartesia")
    monkeypatch.setenv("FAKEYOU_TTS_FALLBACK__API_KEY", "cartesia-key")

    settings = Settings(**NO_ENV_FILE)

    assert isinstance(settings.tts_fallback, CartesiaTTS)


def test_there_is_no_fallback_unless_one_is_configured() -> None:
    assert Settings(**NO_ENV_FILE).tts_fallback is None


class EndingTurn:
    def __init__(self, inner: SpeechTurn) -> None:
        self._inner = inner
        self.ended: str | None = None

    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        return self._inner.speak(text, sentence_index=sentence_index)

    async def finish(self) -> None:
        self.ended = "finished"

    async def cancel(self) -> None:
        self.ended = "cancelled"


class EndingSynth(ScriptedSynth):
    def __init__(self, audio: bytes, **kwargs: Any) -> None:
        super().__init__(audio, **kwargs)
        self.turns: list[EndingTurn] = []

    @override
    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        turn = EndingTurn(super().open_turn(transcript_id=transcript_id, language=language))
        self.turns.append(turn)
        return turn


async def _drained(segment: SpeechSegment) -> bytes:
    return b"".join([chunk async for chunk in segment])


async def test_a_sentence_prepared_before_the_switch_moves_to_the_fallback_too() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", status=503)
    fallback = ScriptedSynth(b"fallback")
    journal = RecordingJournal()
    speech = SpeechWithFallback(primary, fallback, journal=journal)
    turn = speech.open_turn()

    first = turn.speak("First sentence.", sentence_index=0)
    second = turn.speak("Second sentence.", sentence_index=1)

    assert (await _drained(first), await _drained(second)) == (b"fallback", b"fallback")
    assert len(journal.events("tts_fallback_started")) == 1


async def test_a_sentence_already_playing_on_the_primary_keeps_its_voice() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", fail_after_audio=True)
    fallback = ScriptedSynth(b"fallback")
    speech = SpeechWithFallback(primary, fallback)
    turn = speech.open_turn()
    first = turn.speak("First sentence.", sentence_index=0)
    second = turn.speak("Second sentence.", sentence_index=1)

    assert await anext(second) == b"primary"
    assert await _drained(first) == b"primary"
    assert await _drained(second) == b""


async def test_a_fallback_opened_after_the_turn_finished_is_finished_too() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", status=503)
    fallback = EndingSynth(b"fallback")
    speech = SpeechWithFallback(primary, fallback)
    turn = speech.open_turn()
    segment = turn.speak("Hello everyone.")

    await turn.finish()
    assert await _drained(segment) == b"fallback"

    [late] = fallback.turns
    assert late.ended == "finished"


async def test_finishing_a_turn_ends_the_abandoned_primary_and_the_fallback() -> None:
    primary = EndingSynth(b"primary", fault="unavailable", status=503)
    fallback = EndingSynth(b"fallback")
    speech = SpeechWithFallback(primary, fallback)
    turn = speech.open_turn()

    await _said(turn, "First sentence.", 0)
    await turn.finish()

    assert [t.ended for t in primary.turns] == ["cancelled"]
    assert [t.ended for t in fallback.turns] == ["finished"]


class BrokenCloseSegment:
    def __init__(self, inner: SpeechSegment) -> None:
        self._inner = inner

    @property
    def words(self) -> Sequence[WordTiming] | None:
        return self._inner.words

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        return await anext(self._inner)

    async def aclose(self) -> None:
        raise RuntimeError("close failed")


class BrokenCloseTurn(EndingTurn):
    @override
    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        return BrokenCloseSegment(super().speak(text, sentence_index=sentence_index))


class BrokenCloseSynth(ScriptedSynth):
    @override
    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        return BrokenCloseTurn(super().open_turn(transcript_id=transcript_id, language=language))


async def test_a_primary_that_fails_to_close_still_falls_back() -> None:
    primary = BrokenCloseSynth(b"primary", fault="unavailable", status=503)
    fallback = ScriptedSynth(b"fallback")
    speech = SpeechWithFallback(primary, fallback)

    assert await _spoken(speech, "First sentence.", 0) == b"fallback"
