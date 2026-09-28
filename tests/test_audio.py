import asyncio

import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

import fakeyou.audio as audio
from fakeyou.audio import MeetingAudioError, MeetingAudioSession, open_meeting_audio


class _Transport:
    def __init__(self) -> None:
        self.source = _Input()
        self.sink = _Output()

    def input(self) -> FrameProcessor:
        return self.source

    def output(self) -> FrameProcessor:
        return self.sink


class _Output(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.audio: asyncio.Queue[OutputAudioRawFrame] = asyncio.Queue()

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            await self.audio.put(frame)
        await self.push_frame(frame, direction)


class _Input(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class _STT(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                TranscriptionFrame("heard", "guest", "", finalized=False), direction
            )
            await self.push_frame(
                TranscriptionFrame("heard", "guest", "", finalized=True), direction
            )
        await self.push_frame(frame, direction)


class _TTS(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.spoken: list[str] = []

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            self.spoken.append(frame.text)
            await self.push_frame(OutputAudioRawFrame(b"\0\0" * 240, 24_000, 1), direction)
        await self.push_frame(frame, direction)


class _FailingSTT(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(ErrorFrame("secret-provider-key"), FrameDirection.UPSTREAM)
        await self.push_frame(frame, direction)


class _StartupFailSTT(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(ErrorFrame("startup-secret"), FrameDirection.UPSTREAM)
        await self.push_frame(frame, direction)


async def test_session_transcribes_final_only_and_speaks_explicitly() -> None:
    transport = _Transport()
    tts = _TTS()
    heard: list[str] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        heard.append(frame.text)
        received.set()

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=transport,
        stt=_STT(),
        tts=tts,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.wait_for(received.wait(), timeout=2)
        assert heard == ["heard"]
        assert tts.spoken == []
        assert transport.sink.audio.empty()
        await session.speak("reply")
        frame = await asyncio.wait_for(transport.sink.audio.get(), timeout=2)
        assert frame.sample_rate == 24_000
        assert tts.spoken == ["reply"]
        await session.stop()
        await session.stop()


async def test_upstream_error_unblocks_failure_waiter_without_secret() -> None:
    transport = _Transport()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=transport,
        stt=_FailingSTT(),
        tts=_TTS(),
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
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        tts=_TTS(),
    ) as session:
        await session.worker.queue_frame(EndFrame())
        with pytest.raises(MeetingAudioError, match="stopped unexpectedly"):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_callback_failure_becomes_pipeline_failure() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        raise ValueError("secret-callback-data")

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        tts=_TTS(),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)


async def test_startup_provider_error_fails_before_timeout() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError) as caught:
        async with asyncio.timeout(2):
            async with open_meeting_audio(
                muse_api_key="muse-secret",
                openai_api_key="openai-secret",
                on_transcript=on_transcript,
                transport=_Transport(),
                stt=_StartupFailSTT(),
                tts=_TTS(),
            ):
                pass
    assert "startup-secret" not in str(caught.value)


async def test_native_transport_is_terminated_when_construction_fails(monkeypatch) -> None:
    terminated = False

    class _PyAudio:
        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

    class _BrokenTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self._pyaudio = _PyAudio()

        def output(self) -> FrameProcessor:
            raise ValueError("secret-construction-error")

    monkeypatch.setattr(audio, "_native_transport", _BrokenTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError) as caught:
        async with open_meeting_audio(
            muse_api_key="muse-secret",
            openai_api_key="openai-secret",
            on_transcript=on_transcript,
            stt=_STT(),
            tts=_TTS(),
        ):
            pass
    assert terminated
    assert "secret" not in str(caught.value)


async def test_native_transport_without_backend_still_reports_root_error(monkeypatch) -> None:
    class _BackendlessTransport(_Transport):
        def output(self) -> FrameProcessor:
            raise ValueError("construction-error")

    monkeypatch.setattr(audio, "_native_transport", _BackendlessTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError, match="initialization failed") as caught:
        async with open_meeting_audio(
            muse_api_key="muse-secret",
            openai_api_key="openai-secret",
            on_transcript=on_transcript,
            stt=_STT(),
            tts=_TTS(),
        ):
            pass
    assert isinstance(caught.value.__cause__, ValueError)


async def test_cancellation_during_startup_stops_runner(monkeypatch) -> None:
    running = asyncio.Event()
    released = asyncio.Event()
    cancelled = False

    class _Runner:
        def __init__(self, **kwargs) -> None:
            pass

        async def add_workers(self, worker) -> None:
            pass

        async def run(self) -> None:
            running.set()
            await released.wait()

        async def cancel(self, reason: str) -> None:
            nonlocal cancelled
            cancelled = True
            released.set()

    monkeypatch.setattr(audio, "WorkerRunner", _Runner)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    context = open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        tts=_TTS(),
    )
    entering = asyncio.create_task(context.__aenter__())
    await asyncio.wait_for(running.wait(), timeout=2)
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert cancelled


async def test_error_handler_registered_before_add_workers(monkeypatch) -> None:
    order: list[str] = []
    original_add_handler = PipelineWorker.add_event_handler

    def recording_add_handler(self, event_name: str, handler) -> None:
        order.append(event_name)
        original_add_handler(self, event_name, handler)

    monkeypatch.setattr(PipelineWorker, "add_event_handler", recording_add_handler)
    original_add_workers = WorkerRunner.add_workers

    async def recording_add_workers(self, *workers) -> None:
        order.append("add_workers")
        await original_add_workers(self, *workers)

    monkeypatch.setattr(WorkerRunner, "add_workers", recording_add_workers)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        tts=_TTS(),
    ):
        pass
    assert "on_pipeline_error" in order
    assert order.index("on_pipeline_error") < order.index("add_workers")
    started_before = [
        name for name in order[: order.index("add_workers")] if name == "on_pipeline_started"
    ]
    assert len(started_before) >= 2


async def test_pipeline_error_chains_frame_detail() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_FailingSTT(),
        tts=_TTS(),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is not None
        assert "secret-provider-key" in str(caught.value.__cause__)


async def test_runner_failure_chains_original_exception() -> None:
    async def boom() -> None:
        raise ValueError("root-boom")

    task = asyncio.create_task(boom())
    session = MeetingAudioSession(object(), object(), task)
    with pytest.raises(MeetingAudioError) as caught:
        await asyncio.wait_for(session.wait_failed(), timeout=2)
    assert isinstance(caught.value.__cause__, ValueError)
    assert "root-boom" in str(caught.value.__cause__)


async def test_terminate_failure_does_not_mask_initialization_error(monkeypatch) -> None:
    class _RaisingPyAudio:
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    class _BrokenTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self._pyaudio = _RaisingPyAudio()

        def output(self) -> FrameProcessor:
            raise ValueError("construction-error")

    monkeypatch.setattr(audio, "_native_transport", _BrokenTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError, match="initialization failed"):
        async with open_meeting_audio(
            muse_api_key="muse-secret",
            openai_api_key="openai-secret",
            on_transcript=on_transcript,
            stt=_STT(),
            tts=_TTS(),
        ):
            pass


async def test_terminate_failure_does_not_fail_clean_shutdown(monkeypatch) -> None:
    class _RaisingPyAudio:
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    class _NativeTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self._pyaudio = _RaisingPyAudio()

    monkeypatch.setattr(audio, "_native_transport", _NativeTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        stt=_STT(),
        tts=_TTS(),
    ):
        pass
