import asyncio
import math
import sys
import time
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Sequence,
)
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    AudioRawFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
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
from pipecat.transports.base_input import BaseInputTransport
from pipecat.workers.runner import WorkerRunner

from fakeyou.audio.deaf import (
    CaptureActivity,
    DeafMonitor,
    DeafVerdict,
    audio_duration_ms,
    rms_level,
)
from fakeyou.audio.playback import (
    PlaybackEngine,
    PlaybackEvent,
    create_pactl_muter,
    create_paplay_stream_runner,
)
from fakeyou.audio.voice import SPEECH_LANGUAGES, create_muse_stt, create_speech_synthesizer
from fakeyou.audio.voice_activity import VoiceActivityTracker
from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import DEFAULTS, STTSettings, TTSSettings, VADSettings, require_secret
from fakeyou.foundation.attribution_probe import AttributionProbe
from fakeyou.foundation.invariants import (
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    PCM_CHANNELS,
    PLAYBACK_SAMPLE_RATE,
)
from fakeyou.identity.voiceprint_match import CLIP_GRID_MS, CLIP_PAD_MS, cut_turn_clip
from fakeyou.observability.faults import Fault, FaultReporter, record_consumer_fault
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import category_fault, classify_exception

STOP_DEADLINE_SECONDS = 8.0
STT_CONTINUITY_BUFFER_SECONDS = 30
TURN_TAP_MAX_SECONDS = 30.0


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
        self._continuity_buffer: deque[bytes] = deque()
        self._continuity_bytes = 0
        self._last_audio_at: float | None = None

    def _continuity_limit_bytes(self) -> int:
        rate = self.sample_rate or CAPTURE_SAMPLE_RATE
        return rate * 2 * STT_CONTINUITY_BUFFER_SECONDS

    def _socket_ready(self) -> bool:
        socket = getattr(self, "_websocket", None)
        if socket is None:
            return False
        if getattr(getattr(socket, "state", None), "name", None) != "OPEN":
            return False
        ready = getattr(self, "_session_ready", None)
        if ready is not None and not ready.is_set():
            return False
        return True

    def _buffer_continuity_audio(self, audio: bytes) -> None:
        if not audio:
            return
        self._continuity_buffer.append(audio)
        self._continuity_bytes += len(audio)
        limit = self._continuity_limit_bytes()
        while self._continuity_bytes > limit and len(self._continuity_buffer) > 1:
            self._continuity_bytes -= len(self._continuity_buffer.popleft())

    async def _drain_continuity_buffer(self) -> None:
        while self._continuity_buffer and self._socket_ready():
            chunk = self._continuity_buffer.popleft()
            self._continuity_bytes -= len(chunk)
            async for _ in super().run_stt(chunk):
                pass

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
        await self._drain_continuity_buffer()

    async def _connect(self) -> None:
        async with asyncio.timeout(20):
            await cast(Callable[[], Awaitable[None]], super()._connect)()
        await self._drain_continuity_buffer()

    async def _disconnect(self) -> None:
        async with asyncio.timeout(5):
            await cast(Callable[[], Awaitable[None]], super()._disconnect)()

    def idle_seconds(self, now: float) -> float | None:
        if self._last_audio_at is None:
            return None
        return max(now - self._last_audio_at, 0.0)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if audio:
            self._last_audio_at = time.monotonic()
        async with asyncio.timeout(10):
            if not self._socket_ready():
                self._buffer_continuity_audio(audio)
                yield None
                return
            while self._continuity_buffer:
                if not self._socket_ready():
                    self._buffer_continuity_audio(audio)
                    yield None
                    return
                chunk = self._continuity_buffer.popleft()
                self._continuity_bytes -= len(chunk)
                async for frame in super().run_stt(chunk):
                    yield frame
            async for frame in super().run_stt(audio):
                yield frame


def stt_reconnect_supported(stt: object) -> bool:
    return asyncio.iscoroutinefunction(getattr(stt, "reconnect_when_idle", None))


def stt_socket_dead(stt: object) -> bool:
    socket = getattr(stt, "_websocket", None)
    if socket is None:
        return False
    return getattr(getattr(socket, "state", None), "name", None) != "OPEN"


async def stt_ready(stt: object, *, timeout_seconds: float) -> bool:
    ready = getattr(stt, "_session_ready", None)
    if ready is None:
        return True
    try:
        async with asyncio.timeout(timeout_seconds):
            await ready.wait()
    except TimeoutError:
        return False
    return True


async def run_stt_reconnect(stt: object) -> None:
    reconnect = getattr(stt, "reconnect_when_idle", None)
    if reconnect is None or not asyncio.iscoroutinefunction(reconnect):
        raise AttributeError("STT service does not support policy-controlled reconnect")
    async with asyncio.timeout(20):
        await reconnect()


class _CaptureObserver(FrameProcessor):
    def __init__(
        self,
        activity: CaptureActivity,
        journal: Journal | None = None,
        probe: AttributionProbe | None = None,
        voice_activity: VoiceActivityTracker | None = None,
        on_vad: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._activity = activity
        self._journal = journal
        self._probe = probe
        self._voice_activity = voice_activity
        self._on_vad = on_vad
        self._vad_probe_failed = False

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
                if self._probe is not None:
                    channels = max(frame.num_channels, 1)
                    self._probe.observe_pcm(len(frame.audio) // 2 // channels)
            elif isinstance(frame, (VADUserStartedSpeakingFrame, UserSpeakingFrame)):
                self._activity.observe_speech(True)
                if isinstance(frame, VADUserStartedSpeakingFrame):
                    self._record("speech_started")
                    self._observe_vad(True)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._activity.observe_speech(False)
                self._record("speech_stopped")
                self._observe_vad(False)
            if self._on_vad is not None and isinstance(
                frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
            ):
                try:
                    async with asyncio.timeout(10):
                        await self._on_vad(isinstance(frame, VADUserStartedSpeakingFrame))
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    record_consumer_fault(
                        None,
                        self._journal,
                        source="audio",
                        detail="vad_consumer",
                        error=error,
                    )
        await self.push_frame(frame, direction)

    def _record(self, event: str) -> None:
        if self._journal is not None:
            self._journal.record(event, source="audio")

    def _observe_vad(self, active: bool) -> None:
        if self._voice_activity is not None:
            self._voice_activity.observe_vad(active)
        if self._probe is None:
            return
        try:
            self._probe.observe_vad(active)
        except Exception as error:
            if not self._vad_probe_failed:
                self._vad_probe_failed = True
                if self._journal is not None:
                    self._journal.record(
                        "vad_observe_failed",
                        source="audio",
                        error_type=type(error).__name__,
                    )


class PreRollBuffer(FrameProcessor):
    def __init__(self, sample_rate: int, *, preroll_ms: float = 500.0, channels: int = 1) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._channels = max(channels, 1)
        self._limit_bytes = max(int(sample_rate * 2 * self._channels * preroll_ms / 1000.0), 0)
        self._ring: deque[bytes] = deque()
        self._ring_bytes = 0
        self._speaking = False

    @property
    def buffered_ms(self) -> float:
        width = self._sample_rate * 2 * self._channels
        return (self._ring_bytes / width * 1000.0) if width else 0.0

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            if self._ring_bytes:
                await self.push_frame(
                    InputAudioRawFrame(
                        audio=b"".join(self._ring),
                        sample_rate=self._sample_rate,
                        num_channels=self._channels,
                    ),
                    direction,
                )
                self._ring.clear()
                self._ring_bytes = 0
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speaking = False
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, AudioRawFrame) and not self._speaking:
            if frame.audio:
                self._ring.append(frame.audio)
                self._ring_bytes += len(frame.audio)
                while self._ring_bytes > self._limit_bytes and self._ring:
                    self._ring_bytes -= len(self._ring.popleft())
            await self.push_frame(cast(Frame, frame), direction)
            return
        await self.push_frame(frame, direction)


class TurnAudioTap(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self._speaking = False
        self._clock_ms = 0.0
        self._buffer = bytearray()
        self._segments: list[list[float]] = []
        self._limit_bytes = max(int(CAPTURE_SAMPLE_RATE * 2 * 1 * TURN_TAP_MAX_SECONDS), 0)

    def take_clip(self) -> bytes:
        closed = [segment for segment in self._segments if len(segment) == 2]
        if not closed:
            return b""
        base = self._buffer_start_ms()
        start_ms = min(segment[0] for segment in closed) - base
        stop_ms = max(segment[1] for segment in closed) - base
        clip = cut_turn_clip(bytes(self._buffer), start_ms, stop_ms)
        keep = [segment for segment in self._segments if len(segment) == 1]
        if keep:
            edge_ms = max(keep[0][0] - CLIP_PAD_MS - CLIP_GRID_MS, 0.0)
            aligned_ms = math.floor(edge_ms / CLIP_GRID_MS) * CLIP_GRID_MS
            drop_ms = max(aligned_ms - base, 0.0)
            drop = min(
                round(drop_ms * CAPTURE_SAMPLE_RATE * 2 / 1000.0) // 2 * 2, len(self._buffer)
            )
            del self._buffer[:drop]
        else:
            self._buffer.clear()
        self._segments = keep
        return clip

    def _buffer_start_ms(self) -> float:
        return self._clock_ms - len(self._buffer) / (CAPTURE_SAMPLE_RATE * 2 / 1000.0)

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, VADUserStartedSpeakingFrame):
                self._speaking = True
                if self._segments and len(self._segments[-1]) == 1:
                    self._segments[-1][0] = self._clock_ms
                else:
                    self._segments.append([self._clock_ms])
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                if self._speaking:
                    self._speaking = False
                    self._segments[-1].append(self._clock_ms)
            elif isinstance(frame, AudioRawFrame):
                rate = frame.sample_rate or CAPTURE_SAMPLE_RATE
                channels = max(frame.num_channels, 1)
                self._clock_ms += len(frame.audio) / (rate * 2 * channels / 1000.0)
                if frame.audio:
                    self._buffer += frame.audio
                    excess = len(self._buffer) - self._limit_bytes
                    if excess > 0:
                        del self._buffer[:excess]
        await self.push_frame(frame, direction)


class _TranscriptObserver(FrameProcessor):
    def __init__(
        self,
        callback: Callable[[TranscriptionFrame], Awaitable[None]],
        activity: CaptureActivity,
        faults: FaultReporter | None,
        on_interim: Callable[[TranscriptionFrame], Awaitable[None]] | None = None,
        *,
        journal: Journal | None = None,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._activity = activity
        self._faults = faults
        self._on_interim = on_interim
        self._journal = journal
        self._interims = 0
        self._first_interim_at: float | None = None

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
            if transcript.finalized:
                if self._journal is not None:
                    first = self._first_interim_at
                    self._journal.record(
                        "stt_turn_recorded",
                        source="stt",
                        interims=self._interims,
                        first_interim_to_final_ms=(
                            round((time.monotonic() - first) * 1000.0, 1)
                            if first is not None
                            else None
                        ),
                    )
                self._interims = 0
                self._first_interim_at = None
            else:
                if self._first_interim_at is None:
                    self._first_interim_at = time.monotonic()
                self._interims += 1
            callback = self._callback if transcript.finalized else self._on_interim
            if callback is not None:
                try:
                    async with asyncio.timeout(10):
                        await callback(transcript)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    record_consumer_fault(
                        self._faults,
                        self._journal,
                        source="stt",
                        detail="transcript_consumer",
                        error=error,
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
            audio_in_channels=PCM_CHANNELS,
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
        faults: FaultReporter | None = None,
        journal: Journal | None = None,
        deaf_window_seconds: float = DEFAULTS.audio_health.deaf_window_seconds,
        deaf_grace_seconds: float = DEFAULTS.audio_health.deaf_grace_seconds,
        alone_leave_seconds: float = DEFAULTS.audio_health.alone_leave_seconds,
        now: Callable[[], float] = time.monotonic,
        stream_id: str | None = None,
        stt_idle_reconnect_seconds: float = 0.0,
    ) -> None:
        self.activity = activity or CaptureActivity(now=now)
        self._now = now
        self._stream_id = stream_id
        self._stt_idle_reconnect_seconds = stt_idle_reconnect_seconds
        self._stt = stt
        self._faults = faults
        self._journal = journal
        self._monitor = DeafMonitor(
            activity=self.activity,
            faults=faults,
            deaf_window_seconds=deaf_window_seconds,
            deaf_grace_seconds=deaf_grace_seconds,
            alone_leave_seconds=alone_leave_seconds,
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

    def mark_admitted(self) -> None:
        if self._failure is None:
            self.activity.mark_ready()
        if self._reconnect_state == "queued":
            self._reconnect_since = self._now()
            self._reconnect_attempts = 0

    async def wait_failed(self) -> None:
        await self._failed.wait()
        self.raise_if_failed()

    def reconnect_state(self) -> str:
        return self._reconnect_state

    def _reconnect_tags(self) -> dict[str, Any]:
        if self._stream_id is None:
            return {}
        return {"stream_id": self._stream_id}

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
            if self._faults is not None:
                self._faults.record(Fault(source="stt", kind="reconnect_unsupported"))
            if self._journal is not None:
                self._journal.record("stt_reconnect_skipped", source="stt", reason=reason)
            return
        self._reconnect_state = "queued"
        self._reconnect_since = self._now()
        self._reconnect_attempts = 0
        if self._journal is not None:
            self._journal.record(
                "stt_reconnect_queued", source="stt", reason=reason, **self._reconnect_tags()
            )

    def _quiet_for_reconnect(self, stamp: float, is_busy: bool) -> bool:
        snapshot = self.activity.snapshot()
        quiet_since = (
            snapshot.last_speech_at if snapshot.last_speech_at is not None else snapshot.ready_at
        )
        return not (
            is_busy
            or self.is_speaking
            or snapshot.speech_active
            or quiet_since is None
            or stamp - quiet_since < 3
        )

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
            if (
                self._reconnect_state == "idle"
                and self._stt_idle_reconnect_seconds > 0
                and not stt_socket_dead(self._stt)
                and self._quiet_for_reconnect(stamp, is_busy)
            ):
                idle_of = getattr(self._stt, "idle_seconds", None)
                idle = idle_of(stamp) if callable(idle_of) else None
                if idle is not None and idle >= self._stt_idle_reconnect_seconds:
                    self.request_stt_reconnect("proactive_idle")
            if self._reconnect_state != "queued":
                return verdict
            if self._reconnect_since is not None and stamp - self._reconnect_since >= 120:
                self._reconnect_state = "exhausted"
                self._fail("STT reconnect deadline exceeded")
                self.raise_if_failed()
            if self._stopping:
                return verdict
            if not stt_socket_dead(self._stt):
                if not self._quiet_for_reconnect(stamp, is_busy):
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
                if self._faults is not None:
                    self._faults.record(fault)
                if self._journal is not None:
                    self._journal.record(
                        "stt_reconnect_failed",
                        source="stt",
                        kind=fault.kind,
                        attempts=self._reconnect_attempts,
                        **self._reconnect_tags(),
                    )
                self._reconnect_state = "queued" if self._reconnect_attempts < 3 else "exhausted"
                if self._reconnect_state == "exhausted":
                    self._fail("STT reconnect attempts exhausted", cause=error)
                    self.raise_if_failed()
            else:
                self._reconnect_state = "idle"
                if self._journal is not None:
                    self._journal.record(
                        "stt_reconnect_finished",
                        source="stt",
                        attempts=self._reconnect_attempts,
                        **self._reconnect_tags(),
                    )
            return verdict

    @property
    def is_speaking(self) -> bool:
        return self._engine.is_speaking

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        self.raise_if_failed()
        if self._stopping:
            raise MeetingAudioError("meeting audio session is stopped")
        extra: dict[str, Any] = {}
        if sentence_index is not None:
            extra["sentence_index"] = sentence_index
        if transcript_id is not None:
            extra["transcript_id"] = transcript_id
        await self._engine.speak(text, **extra)
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


def vad_analyzer(sample_rate: int, settings: VADSettings) -> SileroVADAnalyzer:
    return SileroVADAnalyzer(
        sample_rate=sample_rate,
        params=VADParams(
            confidence=settings.confidence,
            start_secs=settings.start_secs,
            stop_secs=settings.stop_secs,
            min_volume=settings.min_volume,
        ),
    )


def create_playback_engine(
    *,
    tts_settings: TTSSettings = DEFAULTS.tts,
    speech_language: str = DEFAULTS.speech.language,
    on_event: Callable[[PlaybackEvent], None] | None = None,
    faults: FaultReporter | None = None,
    journal: Journal | None = None,
) -> PlaybackEngine:
    return PlaybackEngine(
        create_speech_synthesizer(
            settings=tts_settings,
            language=speech_language,
            journal=journal,
        ),
        stream_audio=create_paplay_stream_runner(sink=AGENT_SINK_NAME, rate=PLAYBACK_SAMPLE_RATE),
        set_muted=create_pactl_muter(sink=AGENT_SINK_NAME),
        on_event=on_event,
        faults=faults,
        journal=journal,
    )


@asynccontextmanager
async def open_meeting_audio(
    *,
    on_transcript: Callable[[TranscriptionFrame], Awaitable[None]],
    transport: Any | None = None,
    stt: FrameProcessor | None = None,
    playback: PlaybackEngine | None = None,
    on_playback_event: Callable[[PlaybackEvent], None] | None = None,
    on_interim: Callable[[TranscriptionFrame], Awaitable[None]] | None = None,
    on_vad: Callable[[bool], Awaitable[None]] | None = None,
    activity: CaptureActivity | None = None,
    faults: FaultReporter | None = None,
    journal: Journal | None = None,
    deaf_window_seconds: float = DEFAULTS.audio_health.deaf_window_seconds,
    deaf_grace_seconds: float = DEFAULTS.audio_health.deaf_grace_seconds,
    alone_leave_seconds: float = DEFAULTS.audio_health.alone_leave_seconds,
    stt_settings: STTSettings = DEFAULTS.stt,
    stt_keywords: Sequence[str] | None = None,
    tts_settings: TTSSettings = DEFAULTS.tts,
    speech_language: str = DEFAULTS.speech.language,
    vad_settings: VADSettings = DEFAULTS.vad,
    probe: AttributionProbe | None = None,
    voice_activity: VoiceActivityTracker | None = None,
    turn_tap: TurnAudioTap | None = None,
) -> AsyncIterator[MeetingAudioSession]:
    activity = activity or CaptureActivity()
    native_transport = transport is None
    session: MeetingAudioSession | None = None
    engine = (
        playback
        if playback is not None
        else create_playback_engine(
            tts_settings=tts_settings,
            speech_language=speech_language,
            on_event=on_playback_event,
            faults=faults,
            journal=journal,
        )
    )
    try:
        try:
            stt = (
                stt
                if stt is not None
                else create_muse_stt(
                    api_key=require_secret(stt_settings.api_key, ConfigKey.STT_API_KEY),
                    sample_rate=CAPTURE_SAMPLE_RATE,
                    model=stt_settings.model,
                    url=stt_settings.url,
                    mode=stt_settings.mode,
                    language=SPEECH_LANGUAGES[speech_language][0],
                    keywords=stt_keywords,
                    service_class=ManagedMuseSTT,
                )
            )
            transport = transport if transport is not None else _native_transport()
            worker = PipelineWorker(
                Pipeline(
                    [
                        transport.input(),
                        VADProcessor(vad_analyzer=vad_analyzer(CAPTURE_SAMPLE_RATE, vad_settings)),
                        *([turn_tap] if turn_tap is not None else []),
                        _CaptureObserver(activity, journal, probe, voice_activity, on_vad=on_vad),
                        stt,
                        _TranscriptObserver(
                            on_transcript, activity, faults, on_interim, journal=journal
                        ),
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
                detail = category_fault("stt", frame) or classify_exception(
                    "stt", frame.exception or RuntimeError("provider failure")
                )
                if faults is not None:
                    faults.record(detail)
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
                faults=faults,
                journal=journal,
                deaf_window_seconds=deaf_window_seconds,
                deaf_grace_seconds=deaf_grace_seconds,
                alone_leave_seconds=alone_leave_seconds,
            )
            for cause in early_failures:
                session._fail("meeting audio provider failed", cause=cause)
        except (asyncio.CancelledError, FakeYouError):
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
            record_consumer_fault(
                faults, journal, source="audio", detail="audio_cleanup", error=cleanup_error
            )
        if cleanup_errors and original_error is None:
            raise MeetingAudioError("meeting audio cleanup failed") from cleanup_errors[0]


def _tagged(
    stream_id: str,
    callback: Callable[[TranscriptionFrame], Awaitable[None]],
    clock: Callable[[], tuple[int, float] | None] | None = None,
) -> Callable[[TranscriptionFrame], Awaitable[None]]:
    async def tagged(frame: TranscriptionFrame) -> None:
        frame.transport_source = stream_id
        stamped = clock() if clock is not None else None
        if stamped is not None:
            mutable = cast(Any, frame)
            mutable.capture_seq = stamped[0]
            mutable.capture_ms = stamped[1]
        await callback(frame)

    return tagged


@asynccontextmanager
async def open_stream_audio(
    *,
    stream_id: str,
    input: BaseInputTransport,
    on_transcript: Callable[[TranscriptionFrame], Awaitable[None]],
    stt: FrameProcessor | None = None,
    playback: PlaybackEngine | None = None,
    on_playback_event: Callable[[PlaybackEvent], None] | None = None,
    on_interim: Callable[[TranscriptionFrame], Awaitable[None]] | None = None,
    on_vad: Callable[[bool], Awaitable[None]] | None = None,
    activity: CaptureActivity | None = None,
    faults: FaultReporter | None = None,
    journal: Journal | None = None,
    deaf_window_seconds: float = DEFAULTS.audio_health.deaf_window_seconds,
    deaf_grace_seconds: float = DEFAULTS.audio_health.deaf_grace_seconds,
    alone_leave_seconds: float = DEFAULTS.audio_health.alone_leave_seconds,
    stt_settings: STTSettings = DEFAULTS.stt,
    stt_keywords: Sequence[str] | None = None,
    tts_settings: TTSSettings = DEFAULTS.tts,
    speech_language: str = DEFAULTS.speech.language,
    vad_settings: VADSettings = DEFAULTS.vad,
    probe: AttributionProbe | None = None,
    voice_activity: VoiceActivityTracker | None = None,
    turn_tap: TurnAudioTap | None = None,
) -> AsyncIterator[MeetingAudioSession]:
    activity = activity or CaptureActivity()
    tagged_interim = (
        _tagged(stream_id, on_interim, clock=lambda: getattr(input, "last_capture", None))
        if on_interim is not None
        else None
    )
    session: MeetingAudioSession | None = None
    engine = (
        playback
        if playback is not None
        else create_playback_engine(
            tts_settings=tts_settings,
            speech_language=speech_language,
            on_event=on_playback_event,
            faults=faults,
            journal=journal,
        )
    )
    try:
        try:
            stt = (
                stt
                if stt is not None
                else create_muse_stt(
                    api_key=require_secret(stt_settings.api_key, ConfigKey.STT_API_KEY),
                    sample_rate=CAPTURE_SAMPLE_RATE,
                    model=stt_settings.model,
                    url=stt_settings.url,
                    mode=stt_settings.mode,
                    language=SPEECH_LANGUAGES[speech_language][0],
                    keywords=stt_keywords,
                    service_class=ManagedMuseSTT,
                )
            )
            worker = PipelineWorker(
                Pipeline(
                    [
                        input,
                        VADProcessor(vad_analyzer=vad_analyzer(CAPTURE_SAMPLE_RATE, vad_settings)),
                        *([turn_tap] if turn_tap is not None else []),
                        _CaptureObserver(activity, journal, probe, voice_activity, on_vad=on_vad),
                        PreRollBuffer(CAPTURE_SAMPLE_RATE, preroll_ms=stt_settings.preroll_ms),
                        stt,
                        _TranscriptObserver(
                            _tagged(
                                stream_id,
                                on_transcript,
                                clock=lambda: getattr(input, "last_capture", None),
                            ),
                            activity,
                            faults,
                            tagged_interim,
                            journal=journal,
                        ),
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
                detail = category_fault("stt", frame) or classify_exception(
                    "stt", frame.exception or RuntimeError("provider failure")
                )
                if faults is not None:
                    faults.record(detail)
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
                faults=faults,
                journal=journal,
                deaf_window_seconds=deaf_window_seconds,
                deaf_grace_seconds=deaf_grace_seconds,
                alone_leave_seconds=alone_leave_seconds,
                stream_id=stream_id,
                stt_idle_reconnect_seconds=stt_settings.idle_reconnect_seconds,
            )
            for cause in early_failures:
                session._fail("meeting audio provider failed", cause=cause)
        except (asyncio.CancelledError, FakeYouError):
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
        if not await stt_ready(stt, timeout_seconds=stt_settings.ready_timeout_seconds):
            if journal is not None:
                journal.record("stt_session_failed", source="stt", stream_id=stream_id)
            raise MeetingAudioError("STT session did not become ready")
        if journal is not None:
            journal.record("stt_session_started", source="stt", stream_id=stream_id)
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
        for cleanup_error in cleanup_errors:
            record_consumer_fault(
                faults, journal, source="audio", detail="audio_cleanup", error=cleanup_error
            )
        if cleanup_errors and original_error is None:
            raise MeetingAudioError("meeting audio cleanup failed") from cleanup_errors[0]
