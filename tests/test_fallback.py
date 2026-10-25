from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

from fakeyou.config.settings import CartesiaTTS, OpenAITTS, Settings
from fakeyou.foundation.taxonomy import ProviderFaultKind
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault
from fakeyou.speech.fallback import SpeechWithFallback
from fakeyou.speech.registry import build_speech_synthesizer
from tests._persistence_fakes import RecordingJournal

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}


class ScriptedSynth:
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
    return b"".join([chunk async for chunk in speech.stream(text, sentence_index=index)])


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


async def test_a_passing_outage_falls_back_for_one_sentence_and_tries_the_primary_again() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", status=503)
    fallback = ScriptedSynth(b"fallback")
    journal = RecordingJournal()
    speech = SpeechWithFallback(primary, fallback, journal=journal)

    await _spoken(speech, "First sentence.", 0)
    await _spoken(speech, "Second sentence.", 1)

    assert primary.texts == ["First sentence.", "Second sentence."]
    assert [event["lasting"] for event in journal.events("tts_fallback_started")] == [False, False]


async def test_a_fault_after_audio_was_played_is_not_spoken_twice() -> None:
    primary = ScriptedSynth(b"primary", fault="unavailable", fail_after_audio=True)
    fallback = ScriptedSynth(b"fallback")
    speech = SpeechWithFallback(primary, fallback)

    with pytest.raises(ProviderFault):
        await _spoken(speech, "First sentence.", 0)

    assert fallback.texts == []


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
