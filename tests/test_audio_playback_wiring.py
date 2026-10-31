from typing import Any, cast

import pytest
from pydantic import SecretStr

import fakeyou.audio.audio as audio
from fakeyou.audio.audio import (
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    create_playback_engine,
    open_meeting_audio,
)
from fakeyou.audio.playback import PlaybackEngine
from fakeyou.config.settings import MiniMaxTTS, OpenAITTS, STTSettings
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.speech.openai import OpenAISpeechSynthesizer
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakeCapture, FakePlayback


async def test_injected_playback_engine_is_closed_on_shutdown() -> None:
    engine = FakePlayback()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ):
        assert engine.closed is False
    assert engine.closed


async def test_default_playback_engine_closes_speech_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[PlaybackEngine] = []
    real_engine = audio.create_playback_engine

    def recording_engine(**kwargs: Any) -> PlaybackEngine:
        engine = real_engine(**kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr(audio, "create_playback_engine", recording_engine)

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT()),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ):
        [engine] = created
        synth = cast(OpenAISpeechSynthesizer, engine.synthesizer)
        owned = synth.client
        assert not owned.is_closed()
    assert owned.is_closed()


def test_default_playback_engine_streams_tts_to_the_agent_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    from fakeyou.audio.pulse_writer import PulseWriter
    from fakeyou.foundation.invariants import AGENT_SINK_NAME, PLAYBACK_SAMPLE_RATE

    writers: list[dict[str, Any]] = []

    def recording_writer(**kwargs: Any) -> PulseWriter:
        writers.append(kwargs)
        return PulseWriter(**kwargs)

    monkeypatch.setattr(audio, "PulseWriter", recording_writer)
    engine = create_playback_engine(
        speech=SpeechPipelineSettings(tts_settings=OpenAITTS(api_key=SecretStr("k")))
    )

    assert writers == [{"sink": AGENT_SINK_NAME, "rate": PLAYBACK_SAMPLE_RATE}]
    assert inspect.isasyncgen(engine.synthesizer.stream("hello"))


def test_default_playback_engine_uses_the_configured_tts_provider() -> None:
    from fakeyou.speech.minimax import MiniMaxSpeechSynthesizer

    engine = create_playback_engine(
        speech=SpeechPipelineSettings(
            tts_settings=MiniMaxTTS(api_key=SecretStr("k"), voice="Indonesian_CaringMan")
        )
    )

    assert isinstance(engine.synthesizer, MiniMaxSpeechSynthesizer)
    assert engine.synthesizer.voice == "Indonesian_CaringMan"


async def test_default_playback_engine_wires_faults_and_journal() -> None:
    from fakeyou.observability.faults import FaultReporter
    from fakeyou.observability.journal import Journal

    reporter = FaultReporter(journal=Journal("meeting-1"))
    journal = Journal("meeting-1", emit=lambda _: None, echo=False)
    engine = create_playback_engine(
        speech=SpeechPipelineSettings(tts_settings=OpenAITTS(api_key=SecretStr("k"))),
        diagnostics=Diagnostics(faults=reporter, journal=journal),
    )
    try:
        assert engine.faults is reporter
        assert engine.journal is journal
        synth = cast(OpenAISpeechSynthesizer, engine.synthesizer)
        assert synth.journal is journal
    finally:
        await engine.close()
