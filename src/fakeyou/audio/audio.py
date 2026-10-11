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
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

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
    DEFAULT_DEAF_TIMING,
    CaptureActivity,
    DeafMonitor,
    DeafTiming,
    DeafVerdict,
    audio_duration_ms,
    rms_level,
)
from fakeyou.audio.playback import (
    PlaybackBackend,
    PlaybackEngine,
    PlaybackEvent,
    PlaybackPort,
    create_pactl_muter,
    create_paplay_stream_runner,
)
from fakeyou.audio.voice import (
    SPEECH_LANGUAGES,
    MuseSTTOptions,
    create_muse_stt,
    create_speech_synthesizer,
)
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
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import Fault, FaultReporter, record_consumer_fault
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import (
    ProviderFault,
    category_fault,
    classify_exception,
)

STOP_DEADLINE_SECONDS = 8.0
STT_CONTINUITY_BUFFER_SECONDS = 30
TURN_TAP_MAX_SECONDS = 30.0


class OnVad(Protocol):
    async def __call__(self, *, active: bool) -> None: ...


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
        on_vad: OnVad | None = None,
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
                self._activity.observe_speech(active=True)
                if isinstance(frame, VADUserStartedSpeakingFrame):
                    self._record("speech_started")
                    self._observe_vad(active=True)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._activity.observe_speech(active=False)
                self._record("speech_stopped")
                self._observe_vad(active=False)
            if self._on_vad is not None and isinstance(
                frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
            ):
                try:
                    async with asyncio.timeout(10):
                        await self._on_vad(active=isinstance(frame, VADUserStartedSpeakingFrame))
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

    def _observe_vad(self, *, active: bool) -> None:
        if self._voice_activity is not None:
            self._voice_activity.observe_vad(active=active)
        if self._probe is None:
            return
        try:
            self._probe.observe_vad(active=active)
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
            self._activity.observe_transcript(finalized=transcript.finalized)
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


@dataclass(frozen=True, slots=True)
class StreamCallbacks:
    on_transcript: Callable[[TranscriptionFrame], Awaitable[None]]
    on_interim: Callable[[TranscriptionFrame], Awaitable[None]] | None = None
    on_vad: OnVad | None = None
    on_playback_event: Callable[[PlaybackEvent], None] | None = None


@dataclass(frozen=True, slots=True)
class SpeechPipelineSettings:
    stt_settings: STTSettings = field(default_factory=lambda: DEFAULTS.stt)
    stt_keywords: Sequence[str] | None = None
    tts_settings: TTSSettings = field(default_factory=lambda: DEFAULTS.tts)
    speech_language: str = DEFAULTS.speech.language
    vad_settings: VADSettings = field(default_factory=lambda: DEFAULTS.vad)


@dataclass(frozen=True, slots=True)
class StreamPolicy:
    timing: DeafTiming = DeafTiming()
    speech: SpeechPipelineSettings = SpeechPipelineSettings()


DEFAULT_STREAM_POLICY = StreamPolicy()


@dataclass(frozen=True, slots=True)
class StreamSource:
    stream_id: str
    input: BaseInputTransport


@dataclass(frozen=True, slots=True)
class StreamComponents:
    stt: FrameProcessor | None = None
    playback: PlaybackPort | None = None
    turn_tap: TurnAudioTap | None = None
    activity: CaptureActivity | None = None
    probe: AttributionProbe | None = None
    voice_activity: VoiceActivityTracker | None = None


DEFAULT_STREAM_COMPONENTS = StreamComponents()


@dataclass(frozen=True, slots=True)
class SessionPipeline:
    worker: PipelineWorker
    runner: WorkerRunner
    runner_task: asyncio.Task[None]
    engine: PlaybackPort


@dataclass(frozen=True, slots=True)
class SessionCapture:
    stt: object = None
    activity: CaptureActivity | None = None


DEFAULT_SESSION_CAPTURE = SessionCapture()


@dataclass(frozen=True, slots=True)
class SessionRuntime:
    now: Callable[[], float] = time.monotonic
    stream_id: str | None = None
    stt_idle_reconnect_seconds: float = 0.0


DEFAULT_SESSION_RUNTIME = SessionRuntime()


class MeetingAudioSession:
    def __init__(
        self,
        pipeline: SessionPipeline,
        *,
        capture: SessionCapture = DEFAULT_SESSION_CAPTURE,
        diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
        timing: DeafTiming = DEFAULT_DEAF_TIMING,
        runtime: SessionRuntime = DEFAULT_SESSION_RUNTIME,
    ) -> None:
        self.activity = capture.activity or CaptureActivity(now=runtime.now)
        self._now = runtime.now
        self._stream_id = runtime.stream_id
        self._stt_idle_reconnect_seconds = runtime.stt_idle_reconnect_seconds
        self._stt = capture.stt
        self._faults = diagnostics.faults
        self._journal = diagnostics.journal
        self._monitor = DeafMonitor(
            activity=self.activity,
            faults=diagnostics.faults,
            timing=timing,
            journal=diagnostics.journal,
        )
        self._reconnect_state = "idle"
        self._reconnect_since: float | None = None
        self._reconnect_attempts = 0
        self._last_reconnect_at: float | None = None
        self._maintenance_lock = asyncio.Lock()
        if isinstance(capture.stt, ManagedMuseSTT):
            capture.stt.reconnect_requested = self.request_stt_reconnect
        self.worker = pipeline.worker
        self._engine = pipeline.engine
        self._runner = pipeline.runner
        self._runner_task = pipeline.runner_task
        self._failed = asyncio.Event()
        self._failure: MeetingAudioError | None = None
        self._stopping = False
        self._stop_task: asyncio.Task[None] | None = None
        pipeline.runner_task.add_done_callback(self._runner_finished)

    def fail(self, message: str, *, cause: BaseException | None = None) -> None:
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
            self.fail("meeting audio pipeline stopped unexpectedly", cause=cause)
        elif cause is not None:
            self.fail("meeting audio pipeline failed", cause=cause)

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

    def _quiet_for_reconnect(self, stamp: float, *, is_busy: bool) -> bool:
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

    def _apply_monitor_action(self, verdict: DeafVerdict, action: str | None) -> None:
        if action == "terminal":
            self.fail(f"persistent {verdict}")
            self.raise_if_failed()
        if action == "repair":
            self.request_stt_reconnect(verdict)

    def _maybe_proactive_reconnect(self, stamp: float, *, is_busy: bool) -> None:
        if (
            self._reconnect_state == "idle"
            and self._stt_idle_reconnect_seconds > 0
            and not stt_socket_dead(self._stt)
            and self._quiet_for_reconnect(stamp, is_busy=is_busy)
        ):
            idle_of = getattr(self._stt, "idle_seconds", None)
            idle = idle_of(stamp) if callable(idle_of) else None
            if idle is not None and idle >= self._stt_idle_reconnect_seconds:
                self.request_stt_reconnect("proactive_idle")

    def _reconnect_ready(self, stamp: float, *, is_busy: bool) -> bool:
        if self._reconnect_state != "queued":
            return False
        if self._reconnect_since is not None and stamp - self._reconnect_since >= 120:
            self._reconnect_state = "exhausted"
            self.fail("STT reconnect deadline exceeded")
            self.raise_if_failed()
        if self._stopping:
            return False
        if not stt_socket_dead(self._stt):
            if not self._quiet_for_reconnect(stamp, is_busy=is_busy):
                return False
        if self._last_reconnect_at is not None and stamp - self._last_reconnect_at < 5:
            return False
        return True

    def _note_reconnect_failure(self, error: Exception) -> None:
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
            self.fail("STT reconnect attempts exhausted", cause=error)
            self.raise_if_failed()

    def _note_reconnect_success(self) -> None:
        self._reconnect_state = "idle"
        if self._journal is not None:
            self._journal.record(
                "stt_reconnect_finished",
                source="stt",
                attempts=self._reconnect_attempts,
                **self._reconnect_tags(),
            )

    async def _drive_reconnect(
        self, stamp: float, verdict: DeafVerdict, *, is_busy: bool
    ) -> DeafVerdict:
        if not self._reconnect_ready(stamp, is_busy=is_busy):
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
            self._note_reconnect_failure(error)
        else:
            self._note_reconnect_success()
        return verdict

    async def maintenance_tick(
        self, *, is_busy: bool, remote_participants: int | None = None
    ) -> DeafVerdict:
        async with self._maintenance_lock:
            self.raise_if_failed()
            stamp = self._now()
            self.activity.observe_presence(remote_participants)
            verdict = self._monitor.verdict(stamp)
            self._apply_monitor_action(verdict, self._monitor.note_tick(stamp, verdict))
            self._maybe_proactive_reconnect(stamp, is_busy=is_busy)
            return await self._drive_reconnect(stamp, verdict, is_busy=is_busy)

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
        PlaybackBackend(
            set_muted=create_pactl_muter(sink=AGENT_SINK_NAME),
            stream_audio=create_paplay_stream_runner(
                sink=AGENT_SINK_NAME, rate=PLAYBACK_SAMPLE_RATE
            ),
        ),
        on_event=on_event,
        diagnostics=Diagnostics(faults=faults, journal=journal),
    )


@dataclass(slots=True)
class _SessionSlot:
    session: MeetingAudioSession | None = None


@dataclass(frozen=True, slots=True)
class _PipelineSpec:
    input: FrameProcessor
    stt: FrameProcessor
    transcript: Callable[[TranscriptionFrame], Awaitable[None]]
    interim: Callable[[TranscriptionFrame], Awaitable[None]] | None
    extra: tuple[FrameProcessor, ...] = ()


def _resolve_engine(
    playback: PlaybackPort | None,
    speech: SpeechPipelineSettings,
    callbacks: StreamCallbacks,
    diagnostics: Diagnostics,
) -> PlaybackPort:
    if playback is not None:
        return playback
    return create_playback_engine(
        tts_settings=speech.tts_settings,
        speech_language=speech.speech_language,
        on_event=callbacks.on_playback_event,
        faults=diagnostics.faults,
        journal=diagnostics.journal,
    )


def _resolve_stt(stt: FrameProcessor | None, speech: SpeechPipelineSettings) -> FrameProcessor:
    if stt is not None:
        return stt
    return create_muse_stt(
        api_key=require_secret(speech.stt_settings.api_key, ConfigKey.STT_API_KEY),
        options=MuseSTTOptions(
            model=speech.stt_settings.model,
            mode=speech.stt_settings.mode,
            language=SPEECH_LANGUAGES[speech.speech_language][0],
            keywords=speech.stt_keywords,
        ),
        sample_rate=CAPTURE_SAMPLE_RATE,
        url=speech.stt_settings.url,
        service_class=ManagedMuseSTT,
    )


def _resolve_transport(transport: Any | None) -> Any:
    return transport if transport is not None else _native_transport()


def _terminate_if_native(*, native: bool, transport: Any | None) -> None:
    if native and transport is not None:
        _terminate_native_audio(transport)


def _tagged_optional(
    stream_id: str,
    callback: Callable[[TranscriptionFrame], Awaitable[None]] | None,
    input: BaseInputTransport,
) -> Callable[[TranscriptionFrame], Awaitable[None]] | None:
    if callback is None:
        return None
    return _tagged(stream_id, callback, clock=lambda: getattr(input, "last_capture", None))


def _build_worker(
    spec: _PipelineSpec,
    components: StreamComponents,
    callbacks: StreamCallbacks,
    diagnostics: Diagnostics,
    speech: SpeechPipelineSettings,
) -> tuple[PipelineWorker, CaptureActivity]:
    activity = components.activity or CaptureActivity()
    worker = PipelineWorker(
        Pipeline(
            [
                spec.input,
                VADProcessor(vad_analyzer=vad_analyzer(CAPTURE_SAMPLE_RATE, speech.vad_settings)),
                *([components.turn_tap] if components.turn_tap is not None else []),
                _CaptureObserver(
                    activity,
                    diagnostics.journal,
                    components.probe,
                    components.voice_activity,
                    on_vad=callbacks.on_vad,
                ),
                *spec.extra,
                spec.stt,
                _TranscriptObserver(
                    spec.transcript,
                    activity,
                    diagnostics.faults,
                    spec.interim,
                    journal=diagnostics.journal,
                ),
            ]
        ),
        params=PipelineParams(audio_in_sample_rate=CAPTURE_SAMPLE_RATE),
        idle_timeout_secs=None,
    )
    return worker, activity


async def _record_pipeline_error(
    slot: _SessionSlot, faults: FaultReporter | None, frame: ErrorFrame
) -> ProviderFault | None:
    detail = category_fault("stt", frame) or classify_exception(
        "stt", frame.exception or RuntimeError("provider failure")
    )
    if faults is not None:
        faults.record(detail)
    session = slot.session
    if frame.category is not None and not frame.category.is_permanent:
        if session is not None and detail.retryable:
            session.request_stt_reconnect("provider_fault")
        return None
    if session is not None:
        session.fail("meeting audio provider failed", cause=detail)
        return None
    return detail


def _fail_early(session: MeetingAudioSession, early_failures: list[BaseException]) -> None:
    for cause in early_failures:
        session.fail("meeting audio provider failed", cause=cause)


async def _await_session_start(
    session: MeetingAudioSession,
    runner_task: asyncio.Task[None],
    started: asyncio.Event,
) -> None:
    async with asyncio.timeout(25):
        started_task = asyncio.create_task(started.wait())
        failed_task = asyncio.create_task(session.wait_failed())
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


async def _cleanup_audio_session(
    session: MeetingAudioSession | None,
    engine: PlaybackPort,
    diagnostics: Diagnostics,
    teardown: Callable[[], None] | None = None,
) -> None:
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
        if teardown is not None:
            teardown()
    for cleanup_error in cleanup_errors:
        record_consumer_fault(
            diagnostics.faults,
            diagnostics.journal,
            source="audio",
            detail="audio_cleanup",
            error=cleanup_error,
        )
    if cleanup_errors and original_error is None:
        raise MeetingAudioError("meeting audio cleanup failed") from cleanup_errors[0]


async def _confirm_stt_ready(
    stt: FrameProcessor,
    speech: SpeechPipelineSettings,
    diagnostics: Diagnostics,
    stream_id: str,
) -> None:
    if not await stt_ready(stt, timeout_seconds=speech.stt_settings.ready_timeout_seconds):
        if diagnostics.journal is not None:
            diagnostics.journal.record("stt_session_failed", source="stt", stream_id=stream_id)
        raise MeetingAudioError("STT session did not become ready")
    if diagnostics.journal is not None:
        diagnostics.journal.record("stt_session_started", source="stt", stream_id=stream_id)


@asynccontextmanager
async def open_meeting_audio(
    *,
    transport: Any | None = None,
    callbacks: StreamCallbacks,
    components: StreamComponents = DEFAULT_STREAM_COMPONENTS,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
    policy: StreamPolicy = DEFAULT_STREAM_POLICY,
) -> AsyncIterator[MeetingAudioSession]:
    speech = policy.speech
    native_transport = transport is None
    engine = _resolve_engine(components.playback, speech, callbacks, diagnostics)
    slot = _SessionSlot()
    session: MeetingAudioSession | None = None
    try:
        try:
            stt = _resolve_stt(components.stt, speech)
            transport = _resolve_transport(transport)
            worker, activity = _build_worker(
                _PipelineSpec(
                    transport.input(), stt, callbacks.on_transcript, callbacks.on_interim
                ),
                components,
                callbacks,
                diagnostics,
                speech,
            )
            started = asyncio.Event()
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            early_failures: list[BaseException] = []

            async def on_pipeline_started(_: PipelineWorker, __: Any) -> None:
                activity.mark_ready()
                started.set()

            async def on_pipeline_error(_: PipelineWorker, frame: ErrorFrame) -> None:
                detail = await _record_pipeline_error(slot, diagnostics.faults, frame)
                if detail is not None:
                    early_failures.append(detail)

            worker.event_handler("on_pipeline_started")(on_pipeline_started)
            worker.event_handler("on_pipeline_error")(on_pipeline_error)
            async with asyncio.timeout(25):
                await runner.add_workers(worker)
            runner_task = asyncio.create_task(runner.run())
            session = MeetingAudioSession(
                SessionPipeline(worker, runner, runner_task, engine),
                capture=SessionCapture(stt, activity),
                diagnostics=diagnostics,
                timing=policy.timing,
            )
            slot.session = session
            _fail_early(session, early_failures)
        except (asyncio.CancelledError, FakeYouError):
            raise
        except Exception as error:
            raise MeetingAudioError("meeting audio initialization failed") from error
        try:
            await _await_session_start(session, runner_task, started)
        except TimeoutError:
            raise MeetingAudioError("meeting audio startup timed out") from None
        session.raise_if_failed()
        yield session
    finally:
        await _cleanup_audio_session(
            session,
            engine,
            diagnostics,
            teardown=lambda: _terminate_if_native(native=native_transport, transport=transport),
        )


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
    source: StreamSource,
    callbacks: StreamCallbacks,
    components: StreamComponents = DEFAULT_STREAM_COMPONENTS,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
    policy: StreamPolicy = DEFAULT_STREAM_POLICY,
) -> AsyncIterator[MeetingAudioSession]:
    speech = policy.speech
    stream_id = source.stream_id
    input = source.input
    tagged_interim = _tagged_optional(stream_id, callbacks.on_interim, input)
    engine = _resolve_engine(components.playback, speech, callbacks, diagnostics)
    slot = _SessionSlot()
    session: MeetingAudioSession | None = None
    try:
        try:
            stt = _resolve_stt(components.stt, speech)
            worker, activity = _build_worker(
                _PipelineSpec(
                    input,
                    stt,
                    _tagged(
                        stream_id,
                        callbacks.on_transcript,
                        clock=lambda: getattr(input, "last_capture", None),
                    ),
                    tagged_interim,
                    extra=(
                        PreRollBuffer(
                            CAPTURE_SAMPLE_RATE, preroll_ms=speech.stt_settings.preroll_ms
                        ),
                    ),
                ),
                components,
                callbacks,
                diagnostics,
                speech,
            )
            started = asyncio.Event()
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            early_failures: list[BaseException] = []

            async def on_pipeline_started(_: PipelineWorker, __: Any) -> None:
                activity.mark_ready()
                started.set()

            async def on_pipeline_error(_: PipelineWorker, frame: ErrorFrame) -> None:
                detail = await _record_pipeline_error(slot, diagnostics.faults, frame)
                if detail is not None:
                    early_failures.append(detail)

            worker.event_handler("on_pipeline_started")(on_pipeline_started)
            worker.event_handler("on_pipeline_error")(on_pipeline_error)
            async with asyncio.timeout(25):
                await runner.add_workers(worker)
            runner_task = asyncio.create_task(runner.run())
            session = MeetingAudioSession(
                SessionPipeline(worker, runner, runner_task, engine),
                capture=SessionCapture(stt, activity),
                diagnostics=diagnostics,
                timing=policy.timing,
                runtime=SessionRuntime(
                    stream_id=stream_id,
                    stt_idle_reconnect_seconds=speech.stt_settings.idle_reconnect_seconds,
                ),
            )
            slot.session = session
            _fail_early(session, early_failures)
        except (asyncio.CancelledError, FakeYouError):
            raise
        except Exception as error:
            raise MeetingAudioError("meeting audio initialization failed") from error
        try:
            await _await_session_start(session, runner_task, started)
        except TimeoutError:
            raise MeetingAudioError("meeting audio startup timed out") from None
        session.raise_if_failed()
        await _confirm_stt_ready(stt, speech, diagnostics, stream_id)
        yield session
    finally:
        await _cleanup_audio_session(session, engine, diagnostics)
