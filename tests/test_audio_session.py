import asyncio
from collections.abc import Callable
from typing import Any, override

import pytest
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, TranscriptionFrame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr

import fakeyou.audio.audio as audio
from fakeyou.audio.audio import (
    MeetingAudioError,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakePlayback, FakeTransport
from tests._persistence_fakes import RecordingJournal


async def test_session_transcribes_final_only_and_speaks_explicitly() -> None:
    engine = FakePlayback()
    heard: list[str] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        heard.append(frame.text)
        received.set()

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.wait_for(received.wait(), timeout=2)
        assert heard == ["heard"]
        assert engine.spoken == []
        assert session.is_speaking is False
        await session.speak("reply")
        assert engine.spoken == ["reply"]
        await session.stop_playback()
        assert engine.stops == 1
        await session.stop()
        await session.stop()
    assert engine.closed


async def test_session_threads_sentence_index_to_playback_engine() -> None:
    engine = FakePlayback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.speak("First sentence.", sentence_index=0)
        await session.speak("Second sentence.", sentence_index=1)
        await session.speak("Legacy reply.")
        assert engine.spoken == ["First sentence.", "Second sentence.", "Legacy reply."]
        assert engine.indices == [0, 1, None]
        await session.stop_playback()
        assert engine.stops == 1
        await session.stop()
        await session.stop()
    assert engine.closed


async def test_upstream_error_unblocks_failure_waiter_without_secret() -> None:
    transport = FakeTransport()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=transport,
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="secret-provider-key", finalized=()), playback=FakePlayback()
        ),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)
        with pytest.raises(MeetingAudioError):
            session.raise_if_failed()


async def test_unexpected_worker_exit_is_reported() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(EndFrame())
        with pytest.raises(MeetingAudioError, match="stopped unexpectedly"):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_callback_failure_degrades_without_killing_session(
    journal: RecordingJournal,
) -> None:
    from fakeyou.audio.deaf import CaptureActivity
    from fakeyou.observability.faults import FaultReporter

    activity = CaptureActivity()
    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        del frame
        raise ValueError("secret-callback-data")

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback(), activity=activity),
        diagnostics=Diagnostics(faults=reporter),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        await session.speak("still alive")
        snapshot = activity.snapshot()
        assert snapshot.frames_delivered >= 1
        assert snapshot.finals_delivered >= 1
        report = reporter.report()
        assert report is not None
        kinds = {entry["kind"]: entry["count"] for entry in report["faults"]["kinds"]}
        assert kinds.get("callback_failed", 0) >= 1


async def test_startup_provider_error_fails_before_timeout() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with asyncio.timeout(2):
        with pytest.raises(MeetingAudioError) as caught:
            async with open_meeting_audio(
                transport=FakeTransport(),
                callbacks=StreamCallbacks(on_transcript),
                components=StreamComponents(
                    stt=FakeSTT(error="startup-secret", finalized=(), startup=True),
                    playback=FakePlayback(),
                ),
                policy=StreamPolicy(
                    speech=SpeechPipelineSettings(
                        stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                        tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
                    )
                ),
            ):
                pass
    assert "startup-secret" not in str(caught.value)


async def test_native_transport_is_terminated_when_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated = False

    class _PyAudio:
        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

    class _BrokenTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.backend = _PyAudio()

        @override
        def input(self) -> FrameProcessor:
            raise ValueError("secret-construction-error")

    monkeypatch.setattr(audio, "native_transport", _BrokenTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError) as caught:
        async with open_meeting_audio(
            callbacks=StreamCallbacks(on_transcript),
            components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
            policy=StreamPolicy(
                speech=SpeechPipelineSettings(
                    stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                    tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
                )
            ),
        ):
            pass
    assert terminated
    assert "secret" not in str(caught.value)


async def test_native_transport_without_backend_still_reports_root_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BackendlessTransport(FakeTransport):
        @override
        def input(self) -> FrameProcessor:
            raise ValueError("construction-error")

    monkeypatch.setattr(audio, "native_transport", _BackendlessTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError, match="initialization failed") as caught:
        async with open_meeting_audio(
            callbacks=StreamCallbacks(on_transcript),
            components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
            policy=StreamPolicy(
                speech=SpeechPipelineSettings(
                    stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                    tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
                )
            ),
        ):
            pass
    assert isinstance(caught.value.__cause__, ValueError)


async def test_error_handler_registered_before_add_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    original_add_handler = PipelineWorker.add_event_handler

    def recording_add_handler(
        self: PipelineWorker, event_name: str, handler: Callable[..., Any]
    ) -> None:
        order.append(event_name)
        original_add_handler(self, event_name, handler)

    monkeypatch.setattr(PipelineWorker, "add_event_handler", recording_add_handler)
    original_add_workers = WorkerRunner.add_workers

    async def recording_add_workers(self: WorkerRunner, *workers: BaseWorker) -> None:
        order.append("add_workers")
        await original_add_workers(self, *workers)

    monkeypatch.setattr(WorkerRunner, "add_workers", recording_add_workers)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ):
        pass
    assert "on_pipeline_error" in order
    assert order.index("on_pipeline_error") < order.index("add_workers")
    started_before = [
        name for name in order[: order.index("add_workers")] if name == "on_pipeline_started"
    ]
    assert len(started_before) >= 2


async def test_interim_frames_route_to_on_interim_and_tap() -> None:
    from fakeyou.audio.deaf import CaptureActivity

    activity = CaptureActivity()
    interims: list[str] = []
    finals: list[str] = []
    interim_seen = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        finals.append(frame.text)

    async def on_interim(frame: TranscriptionFrame) -> None:
        interims.append(frame.text)
        interim_seen.set()

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript, on_interim=on_interim),
        components=StreamComponents(
            stt=FakeSTT("partial", finalized=(False,)), playback=FakePlayback(), activity=activity
        ),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.wait_for(interim_seen.wait(), timeout=2)
        await asyncio.sleep(0.2)
        assert interims == ["partial"]
        assert finals == []
        snapshot = activity.snapshot()
        assert snapshot.frames_delivered >= 1
        assert snapshot.finals_delivered == 0
        assert snapshot.interim_count >= 1


async def test_pipeline_marks_activity_ready_and_unavailable_on_failure() -> None:
    from fakeyou.audio.deaf import CaptureActivity

    activity = CaptureActivity()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="secret-provider-key", finalized=()),
            playback=FakePlayback(),
            activity=activity,
        ),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        assert activity.snapshot().available is True
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError):
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert activity.snapshot().available is False


async def test_session_threads_transcript_id_to_playback_engine() -> None:
    engine = FakePlayback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.speak("Hello.", sentence_index=0, transcript_id="t9")
        await session.speak("Legacy reply.")
        assert engine.spoken == ["Hello.", "Legacy reply."]
        assert engine.transcripts == ["t9", None]
        await session.stop()
