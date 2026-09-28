import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from pipecat.frames.frames import ErrorFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from fakeyou.voice import create_muse_stt, create_openai_tts

MEETING_SINK_NAME = "meeting_audio"
MEETING_MONITOR_SOURCE_NAME = "meeting_audio.monitor"
AGENT_SINK_NAME = "agent_output"
AGENT_MIC_SOURCE_NAME = "agent_mic"
CAPTURE_SAMPLE_RATE = 16_000
PLAYBACK_SAMPLE_RATE = 24_000


class MeetingAudioError(RuntimeError):
    pass


class _TranscriptObserver(FrameProcessor):
    def __init__(self, callback: Callable[[TranscriptionFrame], Awaitable[None]]) -> None:
        super().__init__()
        self._callback = callback

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TranscriptionFrame)
            and frame.finalized
        ):
            try:
                await self._callback(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.push_frame(
                    ErrorFrame("transcript callback failed"), FrameDirection.UPSTREAM
                )
        await self.push_frame(frame, direction)


def _terminate_native_audio(transport: Any) -> None:
    backend = getattr(transport, "_pyaudio", None)
    if backend is not None:
        with suppress(Exception):
            backend.terminate()


def _native_transport() -> Any:
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    return LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=CAPTURE_SAMPLE_RATE,
            audio_in_channels=1,
            audio_out_enabled=True,
            audio_out_sample_rate=PLAYBACK_SAMPLE_RATE,
            audio_out_channels=1,
        )
    )


class MeetingAudioSession:
    def __init__(
        self, worker: PipelineWorker, runner: WorkerRunner, runner_task: asyncio.Task[None]
    ) -> None:
        self.worker = worker
        self._runner = runner
        self._runner_task = runner_task
        self._failed = asyncio.Event()
        self._failure: MeetingAudioError | None = None
        self._stopping = False
        self._stop_task: asyncio.Task[None] | None = None
        runner_task.add_done_callback(self._runner_finished)

    def _fail(self, message: str, *, cause: BaseException | None = None) -> None:
        if self._failure is None:
            error = MeetingAudioError(message)
            if cause is not None:
                error.__cause__ = cause
            self._failure = error
            self._failed.set()

    def _runner_finished(self, task: asyncio.Task[None]) -> None:
        cause: BaseException | None = None
        if not task.cancelled():
            try:
                task.result()
            except Exception as error:
                cause = error
        if not self._stopping:
            self._fail("meeting audio pipeline stopped unexpectedly", cause=cause)
        elif cause is not None:
            self._fail("meeting audio pipeline failed", cause=cause)

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def wait_failed(self) -> None:
        await self._failed.wait()
        self.raise_if_failed()

    async def speak(self, text: str) -> None:
        self.raise_if_failed()
        if self._stopping:
            raise MeetingAudioError("meeting audio session is stopped")
        await self.worker.queue_frame(TTSSpeakFrame(text))
        self.raise_if_failed()

    async def _stop(self) -> None:
        self._stopping = True
        try:
            await asyncio.wait_for(self._runner.cancel("meeting audio stopped"), timeout=10)
            await asyncio.wait_for(self._runner_task, timeout=15)
        except TimeoutError:
            self._runner_task.cancel()
            try:
                await asyncio.wait_for(self._runner_task, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                pass
            raise MeetingAudioError("meeting audio shutdown timed out") from None
        except Exception as error:
            raise MeetingAudioError("meeting audio shutdown failed") from error

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_task)
            raise


@asynccontextmanager
async def open_meeting_audio(
    *,
    muse_api_key: str,
    openai_api_key: str,
    on_transcript: Callable[[TranscriptionFrame], Awaitable[None]],
    transport: Any | None = None,
    stt: FrameProcessor | None = None,
    tts: FrameProcessor | None = None,
) -> AsyncIterator[MeetingAudioSession]:
    native_transport = transport is None
    session: MeetingAudioSession | None = None
    try:
        try:
            stt = (
                stt
                if stt is not None
                else create_muse_stt(api_key=muse_api_key, sample_rate=CAPTURE_SAMPLE_RATE)
            )
            tts = (
                tts
                if tts is not None
                else create_openai_tts(api_key=openai_api_key, sample_rate=PLAYBACK_SAMPLE_RATE)
            )
            transport = transport if transport is not None else _native_transport()
            worker = PipelineWorker(
                Pipeline(
                    [
                        transport.input(),
                        stt,
                        _TranscriptObserver(on_transcript),
                        tts,
                        transport.output(),
                    ]
                ),
                params=PipelineParams(
                    audio_in_sample_rate=CAPTURE_SAMPLE_RATE,
                    audio_out_sample_rate=PLAYBACK_SAMPLE_RATE,
                ),
                idle_timeout_secs=None,
            )
            started = asyncio.Event()
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            early_failures: list[BaseException] = []

            async def on_pipeline_started(_: PipelineWorker, __: Any) -> None:
                started.set()

            async def on_pipeline_error(_: PipelineWorker, frame: ErrorFrame) -> None:
                detail: BaseException
                if frame.exception is not None:
                    detail = frame.exception
                else:
                    detail = RuntimeError(frame.error)
                if session is not None:
                    session._fail("meeting audio provider failed", cause=detail)
                else:
                    early_failures.append(detail)

            worker.event_handler("on_pipeline_started")(on_pipeline_started)
            worker.event_handler("on_pipeline_error")(on_pipeline_error)
            await runner.add_workers(worker)
            runner_task = asyncio.create_task(runner.run())
            session = MeetingAudioSession(worker, runner, runner_task)
            for cause in early_failures:
                session._fail("meeting audio provider failed", cause=cause)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise MeetingAudioError("meeting audio initialization failed") from error

        try:
            async with asyncio.timeout(25):
                started_task = asyncio.create_task(started.wait())
                failed_task = asyncio.create_task(session._failed.wait())
                try:
                    done, _ = await asyncio.wait(
                        {started_task, failed_task, runner_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    session.raise_if_failed()
                    if runner_task in done and not started.is_set():
                        raise MeetingAudioError("meeting audio startup failed")
                finally:
                    for task in (started_task, failed_task):
                        task.cancel()
                    await asyncio.gather(started_task, failed_task, return_exceptions=True)
        except TimeoutError:
            raise MeetingAudioError("meeting audio startup timed out") from None
        session.raise_if_failed()
        yield session
    finally:
        try:
            if session is not None:
                await session.stop()
        finally:
            if native_transport and transport is not None:
                _terminate_native_audio(transport)
