import asyncio
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    AudioRawFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.meta.stt import MetaSTTService
from pipecat.workers.runner import WorkerRunner

from fakeyou.callbacks import record_consumer_fault
from fakeyou.deaf import (
    CaptureActivity,
    CaptureFault,
    DeafMonitor,
    DeafVerdict,
    audio_duration_ms,
    rms_level,
)
from fakeyou.faults import FaultReporter
from fakeyou.journal import ProviderJournal
from fakeyou.playback import (
    PlaybackEngine,
    PlaybackEvent,
    create_pactl_muter,
    create_paplay_runner,
    create_paplay_stream_runner,
)
from fakeyou.provider_faults import ERROR_CATEGORY_KINDS, ProviderFault, classify_exception
from fakeyou.voice import OpenAISpeechSynthesizer, create_muse_stt

MEETING_SINK_NAME = "meeting_audio"
MEETING_MONITOR_SOURCE_NAME = "meeting_audio.monitor"
AGENT_SINK_NAME = "agent_output"
AGENT_MIC_SOURCE_NAME = "agent_mic"
CAPTURE_SAMPLE_RATE = 16_000
PLAYBACK_SAMPLE_RATE = 24_000
STOP_DEADLINE_SECONDS = 8.0


class MeetingAudioError(RuntimeError):
    pass


def _consume_task(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        with suppress(BaseException):
            task.exception()


async def _bounded_call(call: Coroutine[Any, Any, Any], timeout_seconds: float) -> None:
    task = asyncio.create_task(call)
    done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout_seconds))
    if not done:
        task.add_done_callback(_consume_task)
        task.cancel()
        raise TimeoutError
    task.result()


class ManagedMuseSTT(MetaSTTService):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reconnect_requested: Callable[[str], None] | None = None

    async def _request_reconnect(self) -> None:
        if self.reconnect_requested is not None:
            self.reconnect_requested("native_request")

    async def _reconnect(self) -> None:
        await self._request_reconnect()

    async def _try_reconnect(self, max_retries: int = 3, report_error: Any = None) -> bool:
        await self._request_reconnect()
        return False

    async def reconnect_when_idle(self) -> None:
        async with asyncio.timeout(20):
            await cast(Callable[[], Awaitable[None]], super()._reconnect)()
        if not self._session_ready.is_set():
            raise MeetingAudioError("STT reconnect did not establish a session")

    async def _connect(self) -> None:
        async with asyncio.timeout(20):
            await cast(Callable[[], Awaitable[None]], super()._connect)()

    async def _disconnect(self) -> None:
        async with asyncio.timeout(5):
            await cast(Callable[[], Awaitable[None]], super()._disconnect)()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        async with asyncio.timeout(10):
            async for frame in super().run_stt(audio):
                yield frame


def stt_reconnect_supported(stt: object) -> bool:
    return asyncio.iscoroutinefunction(getattr(stt, "reconnect_when_idle", None))


async def run_stt_reconnect(stt: object) -> None:
    reconnect = getattr(stt, "reconnect_when_idle", None)
    if reconnect is None or not asyncio.iscoroutinefunction(reconnect):
        raise AttributeError("STT service does not support policy-controlled reconnect")
    async with asyncio.timeout(20):
        await reconnect()


class _CaptureObserver(FrameProcessor):
    def __init__(self, activity: CaptureActivity) -> None:
        super().__init__()
        self._activity = activity

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, AudioRawFrame):
                self._activity.observe_audio(
                    energy=rms_level(frame.audio, channels=frame.num_channels),
                    duration_ms=audio_duration_ms(
                        len(frame.audio), frame.sample_rate, frame.num_channels
                    ),
                )
            elif isinstance(frame, (VADUserStartedSpeakingFrame, UserSpeakingFrame)):
                self._activity.observe_speech(True)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._activity.observe_speech(False)
        await self.push_frame(frame, direction)


class _TranscriptObserver(FrameProcessor):
    def __init__(
        self,
        callback: Callable[[TranscriptionFrame], Awaitable[None]],
        activity: CaptureActivity,
        reporter: FaultReporter | None,
        on_interim: Callable[[TranscriptionFrame], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._activity = activity
        self._reporter = reporter
        self._on_interim = on_interim

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (TranscriptionFrame, InterimTranscriptionFrame)
        ):
            transcript = (
                frame
                if isinstance(frame, TranscriptionFrame)
                else TranscriptionFrame(
                    frame.text,
                    frame.user_id,
                    frame.timestamp,
                    language=frame.language,
                    finalized=False,
                )
            )
            self._activity.observe_transcript(transcript.finalized)
            callback = self._callback if transcript.finalized else self._on_interim
            if callback is not None:
                try:
                    async with asyncio.timeout(10):
                        await callback(transcript)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    record_consumer_fault(self._reporter, None, "transcript_consumer", error)
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
            audio_out_enabled=False,
        )
    )


class MeetingAudioSession:
    def __init__(
        self,
        worker: PipelineWorker,
        runner: WorkerRunner,
        runner_task: asyncio.Task[None],
        engine: PlaybackEngine,
        *,
        stt: object = None,
        activity: CaptureActivity | None = None,
        fault_reporter: FaultReporter | None = None,
        journal: ProviderJournal | None = None,
        deaf_window_seconds: float = 120.0,
        deaf_grace_seconds: float = 120.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.activity = activity or CaptureActivity(now=now)
        self._now = now
        self._stt = stt
        self._reporter = fault_reporter
        self._journal = journal
        self._monitor = DeafMonitor(
            activity=self.activity,
            reporter=fault_reporter,
            deaf_window_seconds=deaf_window_seconds,
            deaf_grace_seconds=deaf_grace_seconds,
            journal=journal,
        )
        self._reconnect_state = "idle"
        self._reconnect_since: float | None = None
        self._reconnect_attempts = 0
        self._last_reconnect_at: float | None = None
        self._maintenance_lock = asyncio.Lock()
        if isinstance(stt, ManagedMuseSTT):
            stt.reconnect_requested = self.request_stt_reconnect
        self.worker = worker
        self._engine = engine
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
            self.activity.mark_unavailable()
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

    def reconnect_state(self) -> str:
        return self._reconnect_state

    def request_stt_reconnect(self, reason: str) -> None:
        if self._stopping or self._reconnect_state in (
            "queued",
            "running",
            "exhausted",
            "unsupported",
        ):
            return
        if not stt_reconnect_supported(self._stt):
            self._reconnect_state = "unsupported"
            if self._reporter is not None:
                self._reporter.record(CaptureFault("reconnect_unsupported"))
            if self._journal is not None:
                self._journal.record("stt_reconnect", outcome="unsupported", reason=reason)
            return
        self._reconnect_state = "queued"
        self._reconnect_since = self._now()
        self._reconnect_attempts = 0
        if self._journal is not None:
            self._journal.record("stt_reconnect", outcome="queued", reason=reason)

    async def maintenance_tick(
        self, *, is_busy: bool, remote_participants: int | None = None
    ) -> DeafVerdict:
        async with self._maintenance_lock:
            self.raise_if_failed()
            stamp = self._now()
            self.activity.observe_presence(remote_participants)
            verdict = self._monitor.verdict(stamp)
            action = self._monitor.note_tick(stamp, verdict)
            if action == "terminal":
                self._fail(f"persistent {verdict}")
                self.raise_if_failed()
            if action == "repair":
                self.request_stt_reconnect(verdict)
            if self._reconnect_state != "queued":
                return verdict
            if self._reconnect_since is not None and stamp - self._reconnect_since >= 120:
                self._reconnect_state = "exhausted"
                self._fail("STT reconnect deadline exceeded")
                self.raise_if_failed()
            snapshot = self.activity.snapshot()
            quiet_since = (
                snapshot.last_speech_at
                if snapshot.last_speech_at is not None
                else snapshot.ready_at
            )
            if (
                self._stopping
                or is_busy
                or self.is_speaking
                or snapshot.speech_active
                or quiet_since is None
                or stamp - quiet_since < 3
            ):
                return verdict
            if self._last_reconnect_at is not None and stamp - self._last_reconnect_at < 5:
                return verdict
            self._reconnect_state = "running"
            self._last_reconnect_at = stamp
            self._reconnect_attempts += 1
            try:
                await run_stt_reconnect(self._stt)
            except asyncio.CancelledError:
                self._reconnect_state = "queued"
                raise
            except Exception as error:
                fault = classify_exception("stt", error)
                if self._reporter is not None:
                    self._reporter.record(fault)
                if self._journal is not None:
                    self._journal.record(
                        "stt_reconnect",
                        outcome="failed",
                        kind=fault.kind,
                        attempts=self._reconnect_attempts,
                    )
                self._reconnect_state = "queued" if self._reconnect_attempts < 3 else "exhausted"
                if self._reconnect_state == "exhausted":
                    self._fail("STT reconnect attempts exhausted", cause=error)
                    self.raise_if_failed()
            else:
                self._reconnect_state = "idle"
                if self._journal is not None:
                    self._journal.record(
                        "stt_reconnect",
                        outcome="ok",
                        attempts=self._reconnect_attempts,
                    )
            return verdict

    @property
    def is_speaking(self) -> bool:
        return self._engine.is_speaking

    async def speak(self, text: str) -> None:
        self.raise_if_failed()
        if self._stopping:
            raise MeetingAudioError("meeting audio session is stopped")
        await self._engine.speak(text)
        self.raise_if_failed()

    async def stop_playback(self) -> None:
        await self._engine.stop()

    async def _stop(self) -> None:
        self._stopping = True
        self.activity.mark_unavailable()
        failures: list[Exception] = []
        deadline = time.monotonic() + STOP_DEADLINE_SECONDS

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        try:
            await _bounded_call(self._engine.stop(), min(3.0, remaining()))
        except Exception as error:
            failures.append(error)
        try:
            await _bounded_call(self._runner.cancel("meeting audio stopped"), remaining())
            if not self._runner_task.done():
                done, _ = await asyncio.wait({self._runner_task}, timeout=remaining())
                if not done:
                    failures.append(TimeoutError("meeting audio runner shutdown timed out"))
        except Exception as error:
            failures.append(error)
        if not self._runner_task.done():
            self._runner_task.cancel()
        if failures:
            raise MeetingAudioError("meeting audio shutdown failed") from failures[0]

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_task)
            raise


def default_playback_engine(
    *,
    openai_api_key: str,
    on_event: Callable[[PlaybackEvent], None] | None = None,
    fault_reporter: FaultReporter | None = None,
    journal: ProviderJournal | None = None,
) -> PlaybackEngine:
    return PlaybackEngine(
        OpenAISpeechSynthesizer(api_key=openai_api_key, journal=journal),
        play_audio=create_paplay_runner(sink=AGENT_SINK_NAME, rate=PLAYBACK_SAMPLE_RATE),
        stream_audio=create_paplay_stream_runner(sink=AGENT_SINK_NAME, rate=PLAYBACK_SAMPLE_RATE),
        streaming=True,
        set_muted=create_pactl_muter(sink=AGENT_SINK_NAME),
        on_event=on_event,
        fault_reporter=fault_reporter,
        journal=journal,
    )


@asynccontextmanager
async def open_meeting_audio(
    *,
    muse_api_key: str,
    openai_api_key: str,
    on_transcript: Callable[[TranscriptionFrame], Awaitable[None]],
    transport: Any | None = None,
    stt: FrameProcessor | None = None,
    playback: PlaybackEngine | None = None,
    on_playback_event: Callable[[PlaybackEvent], None] | None = None,
    on_interim: Callable[[TranscriptionFrame], Awaitable[None]] | None = None,
    activity: CaptureActivity | None = None,
    fault_reporter: FaultReporter | None = None,
    journal: ProviderJournal | None = None,
    deaf_window_seconds: float = 120.0,
    deaf_grace_seconds: float = 120.0,
) -> AsyncIterator[MeetingAudioSession]:
    activity = activity or CaptureActivity()
    native_transport = transport is None
    session: MeetingAudioSession | None = None
    engine = (
        playback
        if playback is not None
        else default_playback_engine(
            openai_api_key=openai_api_key,
            on_event=on_playback_event,
            fault_reporter=fault_reporter,
            journal=journal,
        )
    )
    try:
        try:
            stt = (
                stt
                if stt is not None
                else create_muse_stt(
                    api_key=muse_api_key,
                    sample_rate=CAPTURE_SAMPLE_RATE,
                    service_class=ManagedMuseSTT,
                )
            )
            transport = transport if transport is not None else _native_transport()
            worker = PipelineWorker(
                Pipeline(
                    [
                        transport.input(),
                        VADProcessor(
                            vad_analyzer=SileroVADAnalyzer(sample_rate=CAPTURE_SAMPLE_RATE)
                        ),
                        _CaptureObserver(activity),
                        stt,
                        _TranscriptObserver(on_transcript, activity, fault_reporter, on_interim),
                    ]
                ),
                params=PipelineParams(audio_in_sample_rate=CAPTURE_SAMPLE_RATE),
                idle_timeout_secs=None,
            )
            started = asyncio.Event()
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            early_failures: list[BaseException] = []

            async def on_pipeline_started(_: PipelineWorker, __: Any) -> None:
                activity.mark_ready()
                started.set()

            async def on_pipeline_error(_: PipelineWorker, frame: ErrorFrame) -> None:
                if frame.category is not None:
                    kind, retryable = ERROR_CATEGORY_KINDS.get(
                        frame.category.name, ("unknown", False)
                    )
                    detail = ProviderFault("stt", kind, retryable=retryable)
                else:
                    detail = classify_exception(
                        "stt", frame.exception or RuntimeError("provider failure")
                    )
                if fault_reporter is not None:
                    fault_reporter.record(detail)
                if frame.category is not None and not frame.category.is_permanent:
                    if session is not None and detail.retryable:
                        session.request_stt_reconnect("provider_fault")
                    return
                if session is not None:
                    session._fail("meeting audio provider failed", cause=detail)
                else:
                    early_failures.append(detail)

            worker.event_handler("on_pipeline_started")(on_pipeline_started)
            worker.event_handler("on_pipeline_error")(on_pipeline_error)
            async with asyncio.timeout(25):
                await runner.add_workers(worker)
            runner_task = asyncio.create_task(runner.run())
            session = MeetingAudioSession(
                worker,
                runner,
                runner_task,
                engine,
                stt=stt,
                activity=activity,
                fault_reporter=fault_reporter,
                journal=journal,
                deaf_window_seconds=deaf_window_seconds,
                deaf_grace_seconds=deaf_grace_seconds,
            )
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
        original_error = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        if session is not None:
            try:
                await session.stop()
            except Exception as error:
                cleanup_errors.append(error)
        try:
            await _bounded_call(engine.close(), STOP_DEADLINE_SECONDS)
        except Exception as error:
            cleanup_errors.append(error)
        finally:
            if native_transport and transport is not None:
                _terminate_native_audio(transport)
        for cleanup_error in cleanup_errors:
            record_consumer_fault(fault_reporter, None, "audio_cleanup", cleanup_error)
        if cleanup_errors and original_error is None:
            raise MeetingAudioError("meeting audio cleanup failed") from cleanup_errors[0]
