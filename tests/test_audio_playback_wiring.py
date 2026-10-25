from pathlib import Path
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


def test_default_playback_engine_streams_tts_to_paplay(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from fakeyou.audio.playback import create_paplay_stream_runner
    from fakeyou.foundation.invariants import AGENT_SINK_NAME, PLAYBACK_SAMPLE_RATE

    runners: list[dict[str, Any]] = []

    def recording_runner(**kwargs: Any) -> Any:
        runners.append(kwargs)
        return create_paplay_stream_runner(**kwargs)

    monkeypatch.setattr(audio, "create_paplay_stream_runner", recording_runner)
    engine = create_playback_engine(
        speech=SpeechPipelineSettings(tts_settings=OpenAITTS(api_key=SecretStr("k")))
    )

    assert runners == [{"sink": AGENT_SINK_NAME, "rate": PLAYBACK_SAMPLE_RATE}]
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


async def test_playback_engine_threads_pactl_timeout_to_the_muter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat
    from collections.abc import AsyncIterator

    from fakeyou.audio.playback import PlaybackEvent
    from fakeyou.config.settings import TTSSettings
    from fakeyou.speech.contract import SpeechSynthesizer

    pactl = tmp_path / "pactl"
    pactl.write_text("#!/bin/sh\nexec sleep 30\n")
    pactl.chmod(pactl.stat().st_mode | stat.S_IXUSR)
    paplay = tmp_path / "paplay"
    paplay.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
    paplay.chmod(paplay.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    class _StubSynth:
        async def stream(self, text: str, **_kwargs: Any) -> AsyncIterator[bytes]:
            del text
            yield b"\x00" * 480

        async def aclose(self) -> None:
            return None

    def _stub_synth(
        settings: TTSSettings,
        *,
        language: str = "id",
        journal: Any = None,
        fallback: TTSSettings | None = None,
    ) -> SpeechSynthesizer:
        del fallback
        del journal
        del language
        del settings
        return _StubSynth()

    monkeypatch.setattr(audio, "build_speech_synthesizer", _stub_synth)
    events: list[PlaybackEvent] = []
    engine = create_playback_engine(
        speech=SpeechPipelineSettings(
            tts_settings=OpenAITTS(api_key=SecretStr("k")), pactl_timeout_seconds=0.05
        ),
        on_event=events.append,
    )
    try:
        await engine.speak("hi")
    finally:
        await engine.close()

    assert any(
        event.kind == "mute_failed" and "timed out after 0.05s" in event.detail for event in events
    )
