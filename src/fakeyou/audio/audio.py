import asyncio
import random
import sys
import time
from collections.abc import (
    AsyncIterator,
    Callable,
    Coroutine,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Protocol, runtime_checkable

from fakeyou.audio.capture import CaptureError, CaptureSource, ParecCapture
from fakeyou.audio.deaf import (
    DEFAULT_DEAF_TIMING,
    CaptureActivity,
    DeafMonitor,
    DeafTiming,
    DeafVerdict,
    verdict_fault,
)
from fakeyou.audio.graph import AudioGraph, GraphParts, OnVad, TurnAudioTap
from fakeyou.audio.own_speech import FinalGate, OwnSpeech
from fakeyou.audio.playback import (
    CuePlayback,
    PlaybackBackend,
    PlaybackEngine,
    PlaybackError,
    PlaybackEvent,
    PlaybackPort,
    create_pactl_muter,
    create_paplay_stream_runner,
)
from fakeyou.audio.stream_input import StreamAudioInput
from fakeyou.audio.vad import VadAnalyzer, VadController, VadParams
from fakeyou.audio.voice_activity import VoiceActivityTracker
from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import DEFAULTS, STTSettings, TTSSettings, VADSettings, require_secret
from fakeyou.foundation.invariants import (
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    PLAYBACK_SAMPLE_RATE,
)
from fakeyou.foundation.protocols import (
    SupportsIdleSeconds,
    SupportsReconnectWhenIdle,
    SupportsSessionExpiry,
)
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import (
    ConsumerSink,
    Fault,
    FaultReporter,
    record_consumer_fault,
)
from fakeyou.observability.provider_faults import (
    ProviderFault,
    classify_exception,
)
from fakeyou.speech.registry import build_speech_synthesizer
from fakeyou.transcription.contract import (
    SpeechSinks,
    SttConnectionLost,
    TranscriptionError,
    TranscriptSink,
)
from fakeyou.transcription.muse import MuseClient, MuseDeps, MuseOptions

STOP_DEADLINE_SECONDS = 8.0
STT_START_TIMEOUT_SECONDS = 25.0
RECONNECT_DEADLINE_SECONDS = 120.0
STT_ROTATION_LEAD_SECONDS = 600.0


class MeetingAudioError(RuntimeError):
    pass


def _consume_task(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _bounded_call(call: Coroutine[Any, Any, Any], timeout_seconds: float) -> None:
    task = asyncio.create_task(call)
    done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout_seconds))
    if not done:
        task.add_done_callback(_consume_task)
        task.cancel()
        raise TimeoutError
    task.result()


def stt_reconnect_supported(stt: object) -> bool:
    return isinstance(stt, SupportsReconnectWhenIdle) and asyncio.iscoroutinefunction(
        stt.reconnect_when_idle
    )


@runtime_checkable
class SocketHealth(Protocol):
    @property
    def socket_dead(self) -> bool: ...


@runtime_checkable
class SessionReadiness(Protocol):
    @property
    def session_ready(self) -> asyncio.Event: ...


@runtime_checkable
class ClosableSession(Protocol):
    async def close(self) -> None: ...


class SpeechSession(Protocol):
    def send_audio(self, pcm: bytes) -> None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


SpeechSessionFactory = Callable[[SpeechSinks], SpeechSession]


def stt_socket_dead(stt: object) -> bool:
    return isinstance(stt, SocketHealth) and stt.socket_dead


async def stt_ready(stt: object, *, timeout_seconds: float) -> bool:
    if not isinstance(stt, SessionReadiness):
        return True
    try:
        async with asyncio.timeout(timeout_seconds):
            await stt.session_ready.wait()
    except TimeoutError:
        return False
    return True


async def run_stt_reconnect(stt: object) -> None:
    if not isinstance(stt, SupportsReconnectWhenIdle) or not asyncio.iscoroutinefunction(
        stt.reconnect_when_idle
    ):
        raise MeetingAudioError("STT service does not support policy-controlled reconnect")
    async with asyncio.timeout(20):
        await stt.reconnect_when_idle()


@dataclass(frozen=True, slots=True)
class StreamCallbacks:
    on_transcript: TranscriptSink
    on_interim: TranscriptSink | None = None
    on_vad: OnVad | None = None
    on_playback_event: Callable[[PlaybackEvent], None] | None = None


@dataclass(frozen=True, slots=True)
class SpeechPipelineSettings:
    stt_settings: STTSettings = field(default_factory=lambda: DEFAULTS.stt)
    stt_keywords: Sequence[str] | None = None
    tts_settings: TTSSettings = field(default_factory=lambda: DEFAULTS.tts)
    speech_language: str = DEFAULTS.speech.language
    vad_settings: VADSettings = field(default_factory=lambda: DEFAULTS.vad)
    pactl_timeout_seconds: float = DEFAULTS.audio.pactl_timeout_seconds


@dataclass(frozen=True, slots=True)
class StreamPolicy:
    timing: DeafTiming = DeafTiming()
    speech: SpeechPipelineSettings = SpeechPipelineSettings()


DEFAULT_STREAM_POLICY = StreamPolicy()


@dataclass(frozen=True, slots=True)
class StreamSource:
    stream_id: str
    input: StreamAudioInput


@dataclass(frozen=True, slots=True)
class SessionRuntime:
    now: Callable[[], float] = time.monotonic
    stream_id: str | None = None
    stt_idle_reconnect_seconds: float = 0.0


DEFAULT_SESSION_RUNTIME = SessionRuntime()


@dataclass(frozen=True, slots=True)
class StreamComponents:
    stt: SpeechSessionFactory | None = None
    playback: PlaybackPort | None = None
    turn_tap: TurnAudioTap | None = None
    activity: CaptureActivity | None = None
    voice_activity: VoiceActivityTracker | None = None
    own_speech: OwnSpeech | None = None
    runtime: SessionRuntime | None = None


DEFAULT_STREAM_COMPONENTS = StreamComponents()


@dataclass(frozen=True, slots=True)
class SessionRun:
    graph: AudioGraph
    task: asyncio.Task[None]
    source: CaptureSource
    engine: PlaybackPort


@dataclass(frozen=True, slots=True)
class SessionCapture:
    stt: object = None
    activity: CaptureActivity | None = None
    gate: FinalGate | None = None


DEFAULT_SESSION_CAPTURE = SessionCapture()


class MeetingAudioSession:
    def __init__(
        self,
        run: SessionRun,
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
        self._gate = capture.gate or FinalGate(OwnSpeech(now=runtime.now))
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
        self._reconnect_delay = 0.0
        self._maintenance_lock = asyncio.Lock()
        self._engine = run.engine
        self._source = run.source
        self._graph_task = run.task
        self._failed = asyncio.Event()
        self._failure: MeetingAudioError | None = None
        self._stopping = False
        self._stop_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._cue_task: asyncio.Task[None] | None = None
        run.task.add_done_callback(self._graph_finished)

    def fail(self, message: str, *, cause: BaseException | None = None) -> None:
        if self._failure is None:
            error = MeetingAudioError(message)
            if cause is not None:
                error.__cause__ = cause
            self._failure = error
            self.activity.mark_unavailable()
            self._failed.set()

    def _graph_finished(self, task: asyncio.Task[None]) -> None:
        cause: BaseException | None = None
        if not task.cancelled():
            cause = task.exception()
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

    def stt_lost(self, fault: ProviderFault) -> None:
        if fault.retryable:
            self._gate.arm_replay()
            self.request_stt_reconnect("provider_fault")
            return
        self.fail("meeting audio provider failed", cause=fault)

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
            fault = verdict_fault(verdict)
            if self._faults is not None and fault is not None:
                self._faults.record(fault)
            self.fail(f"persistent {verdict}")
            self.raise_if_failed()
        if action == "repair":
            self.request_stt_reconnect(verdict)

    def _maybe_proactive_reconnect(self, stamp: float, *, is_busy: bool) -> None:
        if (
            self._reconnect_state != "idle"
            or stt_socket_dead(self._stt)
            or not self._backoff_elapsed(stamp)
            or not self._quiet_for_reconnect(stamp, is_busy=is_busy)
        ):
            return
        reason = self._proactive_reason(stamp)
        if reason is not None:
            self.request_stt_reconnect(reason)

    def _proactive_reason(self, stamp: float) -> str | None:
        stt = self._stt
        if isinstance(stt, SupportsSessionExpiry):
            remaining = stt.session_expires_in(stamp)
            if remaining is not None and remaining <= STT_ROTATION_LEAD_SECONDS:
                return "session_rotation"
        if self._stt_idle_reconnect_seconds > 0 and isinstance(stt, SupportsIdleSeconds):
            idle = stt.idle_seconds(stamp)
            if idle is not None and idle >= self._stt_idle_reconnect_seconds:
                return "proactive_idle"
        return None

    def _backoff_elapsed(self, stamp: float) -> bool:
        return (
            self._last_reconnect_at is None
            or stamp - self._last_reconnect_at >= self._reconnect_delay
        )

    def _reconnect_ready(self, stamp: float, *, is_busy: bool) -> bool:
        if self._reconnect_state != "queued":
            return False
        if self._reconnect_since is not None and stamp - self._reconnect_since >= (
            RECONNECT_DEADLINE_SECONDS
        ):
            self._reconnect_state = "exhausted"
            self.fail("STT reconnect deadline exceeded")
            self.raise_if_failed()
        if self._stopping:
            return False
        if not stt_socket_dead(self._stt):
            if not self._quiet_for_reconnect(stamp, is_busy=is_busy):
                return False
        return self._backoff_elapsed(stamp)

    def _note_reconnect_failure(self, error: BaseException) -> None:
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
        backoff = 5.0 * (2.0 ** (self._reconnect_attempts - 1))
        self._reconnect_delay = backoff + random.uniform(0, backoff)
        self._reconnect_state = "queued" if self._reconnect_attempts < 3 else "exhausted"
        if self._reconnect_state == "exhausted":
            self.fail("STT reconnect attempts exhausted", cause=error)

    def _note_reconnect_success(self) -> None:
        self._reconnect_state = "idle"
        if self._journal is not None:
            self._journal.record(
                "stt_reconnect_finished",
                source="stt",
                attempts=self._reconnect_attempts,
                **self._reconnect_tags(),
            )

    async def _run_reconnect(self) -> None:
        try:
            await run_stt_reconnect(self._stt)
        except asyncio.CancelledError:
            self._reconnect_state = "queued"
            raise
        except (MeetingAudioError, OSError, TimeoutError, TranscriptionError) as error:
            self._note_reconnect_failure(error)
        else:
            self._note_reconnect_success()

    def _reconnect_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None or isinstance(
            error, (MeetingAudioError, OSError, TimeoutError, TranscriptionError)
        ):
            return
        self._note_reconnect_failure(error)

    def _drive_reconnect(self, stamp: float, verdict: DeafVerdict, *, is_busy: bool) -> DeafVerdict:
        if not self._reconnect_ready(stamp, is_busy=is_busy):
            return verdict
        self._reconnect_state = "running"
        self._last_reconnect_at = stamp
        self._reconnect_attempts += 1
        self._reconnect_task = asyncio.create_task(self._run_reconnect())
        self._reconnect_task.add_done_callback(self._reconnect_finished)
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
            return self._drive_reconnect(stamp, verdict, is_busy=is_busy)

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
        with self._gate.own_speech.speaking(text):
            try:
                await self._engine.speak(text, **extra)
            except (PlaybackError, OSError) as error:
                if self._faults is not None:
                    self._faults.record(classify_exception("tts", error, operation="speak"))
                raise
        self.raise_if_failed()

    async def stop_playback(self) -> None:
        await self._engine.stop()

    def prepare_cues(self, texts: Sequence[str]) -> None:
        engine = self._engine
        if not isinstance(engine, CuePlayback) or self._cue_task is not None or self._stopping:
            return
        self._cue_task = asyncio.create_task(self._prepare_cues(engine, tuple(texts)))

    async def _prepare_cues(self, engine: CuePlayback, texts: tuple[str, ...]) -> None:
        for text in texts:
            try:
                await engine.prepare_cue(text)
            except (OSError, TimeoutError, ProviderFault, PlaybackError) as error:
                if self._faults is not None:
                    self._faults.record(classify_exception("tts", error, operation="cue"))
                return

    async def play_cue(self, text: str) -> bool:
        engine = self._engine
        if self._failure is not None or self._stopping or not isinstance(engine, CuePlayback):
            return False
        with self._gate.own_speech.speaking(text):
            return await engine.play_cue(text)

    async def _stop(self) -> None:
        self._stopping = True
        self.activity.mark_unavailable()
        failures: list[Exception] = []
        deadline = time.monotonic() + STOP_DEADLINE_SECONDS

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        reconnect = self._reconnect_task
        if reconnect is not None and not reconnect.done():
            reconnect.cancel()
            done, _ = await asyncio.wait({reconnect}, timeout=remaining())
            if not done:
                failures.append(TimeoutError("meeting audio reconnect shutdown timed out"))
        cue = self._cue_task
        if cue is not None and not cue.done():
            cue.cancel()
            await asyncio.wait({cue}, timeout=remaining())
        try:
            try:
                await _bounded_call(self._engine.stop(), min(3.0, remaining()))
            except (OSError, TimeoutError, PlaybackError) as error:
                failures.append(error)
        finally:
            for terminate_error in _terminate_source(self._source):
                record_consumer_fault(
                    ConsumerSink(self._faults, self._journal),
                    source="audio",
                    detail="audio_teardown",
                    error=terminate_error,
                )
            failures.extend(await self._cancel_graph(remaining))
            failures.extend(await self._close_stt(remaining))
        if failures:
            raise MeetingAudioError("meeting audio shutdown failed") from failures[0]

    async def _cancel_graph(self, remaining: Callable[[], float]) -> list[Exception]:
        if self._graph_task.done():
            return []
        self._graph_task.cancel()
        done, _ = await asyncio.wait({self._graph_task}, timeout=remaining())
        if not done:
            return [TimeoutError("meeting audio graph shutdown timed out")]
        return []

    async def _close_stt(self, remaining: Callable[[], float]) -> list[Exception]:
        if not isinstance(self._stt, ClosableSession):
            return []
        try:
            await _bounded_call(self._stt.close(), remaining())
        except (OSError, TimeoutError, TranscriptionError) as error:
            return [error]
        return []

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_task)
            raise


def vad_params(settings: VADSettings) -> VadParams:
    return VadParams(
        confidence=settings.confidence,
        start_secs=settings.start_secs,
        stop_secs=settings.stop_secs,
        min_volume=settings.min_volume,
    )


def create_playback_engine(
    *,
    tts_settings: TTSSettings = DEFAULTS.tts,
    speech_language: str = DEFAULTS.speech.language,
    pactl_timeout_seconds: float = DEFAULTS.audio.pactl_timeout_seconds,
    on_event: Callable[[PlaybackEvent], None] | None = None,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> PlaybackEngine:
    return PlaybackEngine(
        build_speech_synthesizer(
            settings=tts_settings,
            language=speech_language,
            journal=diagnostics.journal,
        ),
        PlaybackBackend(
            set_muted=create_pactl_muter(
                sink=AGENT_SINK_NAME, timeout_seconds=pactl_timeout_seconds
            ),
            stream_audio=create_paplay_stream_runner(
                sink=AGENT_SINK_NAME, rate=PLAYBACK_SAMPLE_RATE
            ),
        ),
        on_event=on_event,
        diagnostics=diagnostics,
    )


@dataclass(slots=True)
class _SessionSlot:
    session: MeetingAudioSession | None = None
    pending: list[ProviderFault] = field(default_factory=list)


def _adopt(slot: _SessionSlot, session: MeetingAudioSession) -> None:
    slot.session = session
    pending, slot.pending = slot.pending, []
    for fault in pending:
        session.stt_lost(fault)


@dataclass(frozen=True, slots=True)
class SessionSetup:
    stt: SpeechSession
    source: CaptureSource
    activity: CaptureActivity
    components: StreamComponents
    callbacks: StreamCallbacks
    diagnostics: Diagnostics
    speech: SpeechPipelineSettings
    engine: PlaybackPort
    timing: DeafTiming
    runtime: SessionRuntime
    stream_id: str | None
    gate: FinalGate


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
        pactl_timeout_seconds=speech.pactl_timeout_seconds,
        on_event=callbacks.on_playback_event,
        diagnostics=diagnostics,
    )


def _default_speech_session(
    speech: SpeechPipelineSettings, diagnostics: Diagnostics
) -> SpeechSessionFactory:
    def build(sinks: SpeechSinks) -> SpeechSession:
        return MuseClient(
            api_key=require_secret(speech.stt_settings.api_key, ConfigKey.STT_API_KEY),
            options=MuseOptions(
                model=speech.stt_settings.model,
                mode=speech.stt_settings.mode,
                url=speech.stt_settings.url,
                language=speech.speech_language,
                keywords=tuple(speech.stt_keywords or ()),
                sample_rate=CAPTURE_SAMPLE_RATE,
            ),
            sinks=sinks,
            deps=MuseDeps(journal=diagnostics.journal, faults=diagnostics.faults),
        )

    return build


def _on_stt_lost(
    slot: _SessionSlot, faults: FaultReporter | None, error: TranscriptionError
) -> None:
    fault = _stt_loss_fault(error)
    if faults is not None:
        faults.record(fault)
    session = slot.session
    if session is None:
        slot.pending.append(fault)
        return
    session.stt_lost(fault)


def _stt_loss_fault(error: TranscriptionError) -> ProviderFault:
    if isinstance(error, SttConnectionLost):
        return ProviderFault("stt", "unavailable", retryable=True)
    return classify_exception("stt", error)


def _on_capture_failed(slot: _SessionSlot, error: CaptureError) -> None:
    session = slot.session
    if session is not None:
        session.fail("meeting audio capture failed", cause=error)


def _observed(sink: TranscriptSink, activity: CaptureActivity) -> TranscriptSink:
    async def observed(update: TranscriptUpdate) -> None:
        activity.observe_transcript(finalized=update.final)
        await sink(update)

    return observed


def _gated(gate: FinalGate, sink: TranscriptSink) -> TranscriptSink:
    async def gated(update: TranscriptUpdate) -> None:
        if gate.admit(update):
            await sink(update)

    return gated


async def _silent(update: TranscriptUpdate) -> None:
    del update


def _terminate_source(source: CaptureSource | None) -> list[Exception]:
    if source is None:
        return []
    try:
        source.terminate()
    except (OSError, RuntimeError) as error:
        return [error]
    return []


def _tagged_optional(
    stream_id: str,
    callback: TranscriptSink | None,
    input: StreamAudioInput,
) -> TranscriptSink | None:
    if callback is None:
        return None
    return _tagged(stream_id, callback, clock=lambda: input.last_capture)


async def _record_stt_ready(
    stt: object,
    speech: SpeechPipelineSettings,
    diagnostics: Diagnostics,
    stream_id: str | None,
) -> None:
    tags: dict[str, Any] = {} if stream_id is None else {"stream_id": stream_id}
    if not await stt_ready(stt, timeout_seconds=speech.stt_settings.ready_timeout_seconds):
        if diagnostics.journal is not None:
            diagnostics.journal.record("stt_session_failed", source="stt", **tags)
        raise MeetingAudioError("STT session did not become ready")
    if diagnostics.journal is not None:
        diagnostics.journal.record("stt_session_started", source="stt", **tags)


async def _start_session(setup: SessionSetup) -> MeetingAudioSession:
    stt, source = setup.stt, setup.source
    async with asyncio.timeout(STT_START_TIMEOUT_SECONDS):
        await stt.start()
    await _record_stt_ready(stt, setup.speech, setup.diagnostics, setup.stream_id)
    await source.start()
    setup.activity.mark_ready()
    graph = AudioGraph(
        GraphParts(
            activity=setup.activity,
            vad=VadController(VadAnalyzer(vad_params(setup.speech.vad_settings))),
            stt=stt,
            turn_tap=setup.components.turn_tap,
            voice_activity=setup.components.voice_activity,
            on_vad=setup.callbacks.on_vad,
            journal=setup.diagnostics.journal,
        )
    )
    task = asyncio.create_task(graph.run(source.queue))
    return MeetingAudioSession(
        SessionRun(graph, task, source, setup.engine),
        capture=SessionCapture(stt, setup.activity, setup.gate),
        diagnostics=setup.diagnostics,
        timing=setup.timing,
        runtime=setup.runtime,
    )


@asynccontextmanager
async def open_meeting_audio(
    *,
    source: CaptureSource | None = None,
    callbacks: StreamCallbacks,
    components: StreamComponents = DEFAULT_STREAM_COMPONENTS,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
    policy: StreamPolicy = DEFAULT_STREAM_POLICY,
) -> AsyncIterator[MeetingAudioSession]:
    speech = policy.speech
    slot = _SessionSlot()
    engine = _resolve_engine(components.playback, speech, callbacks, diagnostics)
    capture: CaptureSource = source or ParecCapture(on_failure=partial(_on_capture_failed, slot))
    activity = components.activity or CaptureActivity()
    runtime = components.runtime or DEFAULT_SESSION_RUNTIME
    gate = FinalGate(
        components.own_speech or OwnSpeech(now=runtime.now),
        journal=diagnostics.journal,
        tags={},
    )
    stt: SpeechSession | None = None
    session: MeetingAudioSession | None = None
    try:
        try:
            sinks = SpeechSinks(
                on_update=_gated(gate, _observed(callbacks.on_transcript, activity)),
                on_interim=_observed(callbacks.on_interim or _silent, activity),
                on_lost=partial(_on_stt_lost, slot, diagnostics.faults),
            )
            factory = components.stt or _default_speech_session(speech, diagnostics)
            stt = factory(sinks)
            session = await _start_session(
                SessionSetup(
                    stt=stt,
                    source=capture,
                    activity=activity,
                    components=components,
                    callbacks=callbacks,
                    diagnostics=diagnostics,
                    speech=speech,
                    engine=engine,
                    timing=policy.timing,
                    runtime=runtime,
                    stream_id=None,
                    gate=gate,
                )
            )
            _adopt(slot, session)
        except (asyncio.CancelledError, FakeYouError):
            raise
        except (OSError, RuntimeError, ValueError, CaptureError, TranscriptionError) as error:
            raise MeetingAudioError("meeting audio initialization failed") from error
        except TimeoutError:
            raise MeetingAudioError("meeting audio startup timed out") from None
        session.raise_if_failed()
        yield session
    finally:
        if session is None and stt is not None:
            await _close_quietly(stt)
        await _cleanup_audio_session(
            session,
            engine,
            diagnostics,
            teardown=None if session is not None else partial(_terminate_source, capture),
        )


async def _close_quietly(stt: SpeechSession) -> None:
    try:
        await stt.close()
    except (OSError, TranscriptionError):
        pass


async def _cleanup_audio_session(
    session: MeetingAudioSession | None,
    engine: PlaybackPort,
    diagnostics: Diagnostics,
    teardown: Callable[[], list[Exception]] | None = None,
) -> None:
    original_error = sys.exc_info()[1]
    cleanup_errors: list[Exception] = []
    teardown_errors: list[Exception] = []
    try:
        if session is not None:
            try:
                await session.stop()
            except MeetingAudioError as error:
                cleanup_errors.append(error)
    finally:
        try:
            await _bounded_call(engine.close(), STOP_DEADLINE_SECONDS)
        except (OSError, TimeoutError, PlaybackError) as error:
            cleanup_errors.append(error)
        finally:
            if teardown is not None:
                teardown_errors.extend(teardown())
    for cleanup_error in cleanup_errors:
        record_consumer_fault(
            ConsumerSink(diagnostics.faults, diagnostics.journal),
            source="audio",
            detail="audio_cleanup",
            error=cleanup_error,
        )
    for teardown_error in teardown_errors:
        record_consumer_fault(
            ConsumerSink(diagnostics.faults, diagnostics.journal),
            source="audio",
            detail="audio_teardown",
            error=teardown_error,
        )
    if cleanup_errors and original_error is None:
        raise MeetingAudioError("meeting audio cleanup failed") from cleanup_errors[0]


def _tagged(
    stream_id: str,
    callback: TranscriptSink,
    clock: Callable[[], tuple[int, float] | None] | None = None,
) -> TranscriptSink:
    async def tagged(update: TranscriptUpdate) -> None:
        stamped = clock() if clock is not None else None
        if stamped is None:
            await callback(replace(update, stream_id=stream_id))
        else:
            await callback(
                replace(update, stream_id=stream_id, capture_seq=stamped[0], capture_ms=stamped[1])
            )

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
    slot = _SessionSlot()
    engine = _resolve_engine(components.playback, speech, callbacks, diagnostics)
    activity = components.activity or CaptureActivity()
    gate = FinalGate(
        components.own_speech or OwnSpeech(),
        journal=diagnostics.journal,
        tags={"stream_id": stream_id},
    )
    stt: SpeechSession | None = None
    session: MeetingAudioSession | None = None
    try:
        try:
            tagged_final = _tagged(
                stream_id, callbacks.on_transcript, clock=lambda: input.last_capture
            )
            tagged_interim = _tagged_optional(stream_id, callbacks.on_interim, input) or _silent
            sinks = SpeechSinks(
                on_update=_gated(gate, _observed(tagged_final, activity)),
                on_interim=_observed(tagged_interim, activity),
                on_lost=partial(_on_stt_lost, slot, diagnostics.faults),
            )
            factory = components.stt or _default_speech_session(speech, diagnostics)
            stt = factory(sinks)
            session = await _start_session(
                SessionSetup(
                    stt=stt,
                    source=input,
                    activity=activity,
                    components=components,
                    callbacks=callbacks,
                    diagnostics=diagnostics,
                    speech=speech,
                    engine=engine,
                    timing=policy.timing,
                    runtime=SessionRuntime(
                        stream_id=stream_id,
                        stt_idle_reconnect_seconds=speech.stt_settings.idle_reconnect_seconds,
                    ),
                    stream_id=stream_id,
                    gate=gate,
                )
            )
            _adopt(slot, session)
        except (asyncio.CancelledError, FakeYouError):
            raise
        except (OSError, RuntimeError, ValueError, CaptureError, TranscriptionError) as error:
            raise MeetingAudioError("meeting audio initialization failed") from error
        except TimeoutError:
            raise MeetingAudioError("meeting audio startup timed out") from None
        session.raise_if_failed()
        yield session
    finally:
        if session is None and stt is not None:
            await _close_quietly(stt)
        await _cleanup_audio_session(
            session,
            engine,
            diagnostics,
            teardown=None if session is not None else partial(_terminate_source, input),
        )
