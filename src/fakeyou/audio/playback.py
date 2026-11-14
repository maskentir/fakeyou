import asyncio
import hashlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES, VoicedClip, voiced_clip
from fakeyou.foundation.protocols import SupportsAclose
from fakeyou.foundation.publication import (
    PublicationContext,
    PublicationDecision,
    SentenceBinding,
)
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.observability.cleanup import close_safely
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import ConsumerSink, FaultReporter, guard_observer
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.speech.contract import (
    PreparedSpeech,
    SpeechSegment,
    SpeechSynthesizer,
    SpeechTurn,
    WordTiming,
)

PlaybackEventKind = Literal[
    "playback_started",
    "playback_audio_started",
    "playback_finished",
    "playback_failed",
    "playback_dropped",
]


@dataclass(frozen=True, slots=True)
class Utterance:
    transcript_id: str | None = None
    sentence_index: int | None = None
    publication: SentenceBinding | None = None
    no_visual: bool = False


NO_UTTERANCE = Utterance()


@dataclass(frozen=True, slots=True)
class PlaybackEvent:
    kind: PlaybackEventKind
    text: str = ""
    detail: str = ""
    cue: bool = False
    utterance: Utterance = NO_UTTERANCE
    heard: str | None = None


GAIN_STEP = 1.2
MAX_GAIN = 2.4
DUCK_GAIN = 0.15
FREEZE_MAX_HOLD_SECONDS = 10.0
FRAME_SECONDS = 0.02
FADE_SECONDS = 0.01
HALT_SECONDS = 0.1
FRAME_BYTES = round(PLAYBACK_SAMPLE_RATE * FRAME_SECONDS) * PCM16_SAMPLE_BYTES
FADE_BYTES = round(PLAYBACK_SAMPLE_RATE * FADE_SECONDS) * PCM16_SAMPLE_BYTES
FADE_SAMPLE_COUNT = round(PLAYBACK_SAMPLE_RATE * FADE_SECONDS)
BYTES_PER_SECOND = PLAYBACK_SAMPLE_RATE * PCM16_SAMPLE_BYTES


SYNTH_TIMEOUT_SECONDS = 45.0
CUE_CHUNK_BYTES = 4800
CUE_MAX_SECONDS = 1.2
CUE_MAX_PAUSE_SECONDS = 0.3
CUE_RENDER_ATTEMPTS = 3
PLAYBACK_TIMEOUT_SECONDS = 120.0
CLEANUP_TIMEOUT_SECONDS = 5.0


class PlaybackError(RuntimeError):
    pass


class _PublicationPrevented(RuntimeError):
    def __init__(self, validity: str, reason: str) -> None:
        super().__init__(f"publication_prevented:{validity}:{reason}")
        self.validity = validity
        self.reason = reason


def _is_timeout(error: BaseException) -> bool:
    return isinstance(error, (TimeoutError, ProviderFault)) and (
        not isinstance(error, ProviderFault) or error.kind == "timeout"
    )


@dataclass(frozen=True, slots=True)
class PlaybackTiming:
    synth_timeout_seconds: float = SYNTH_TIMEOUT_SECONDS
    playback_timeout_seconds: float = PLAYBACK_TIMEOUT_SECONDS
    cleanup_timeout_seconds: float = CLEANUP_TIMEOUT_SECONDS
    freeze_max_hold_seconds: float = FREEZE_MAX_HOLD_SECONDS


DEFAULT_PLAYBACK_TIMING = PlaybackTiming()


class VoiceWriter(Protocol):
    @property
    def buffer_seconds(self) -> float: ...

    async def write(self, pcm: bytes) -> None: ...

    async def drain(self) -> None: ...

    async def close(self) -> None: ...


class PlaybackPort(Protocol):
    @property
    def is_speaking(self) -> bool: ...

    def louder(self) -> None: ...

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None: ...

    async def prepare(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech | None: ...

    async def speak_prepared(self, prepared: PreparedSpeech) -> None: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class DuckingPlayback(Protocol):
    def duck(self) -> None: ...

    def unduck(self) -> None: ...


@runtime_checkable
class FreezingPlayback(Protocol):
    def freeze(self) -> None: ...

    def thaw(self) -> None: ...


@runtime_checkable
class CuePlayback(Protocol):
    async def prepare_cue(self, text: str) -> None: ...

    async def play_cue(self, text: str, *, transcript_id: str | None = None) -> bool: ...


class SpokenWords(Protocol):
    @property
    def words(self) -> Sequence[WordTiming] | None: ...


@dataclass(slots=True)
class _Voicing:
    segment: SpeechSegment | None = None

    @property
    def words(self) -> Sequence[WordTiming] | None:
        return None if self.segment is None else self.segment.words


def heard_text(words: Sequence[WordTiming] | None, seconds: float) -> str | None:
    if seconds <= 0:
        return ""
    if words is None:
        return None
    return " ".join(word.text for word in words if word.end <= seconds)


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    utterance: Utterance
    language: str | None


@dataclass(frozen=True, slots=True)
class _OpenTurn:
    transcript_id: str
    language: str | None
    turn: SpeechTurn


@dataclass(frozen=True, slots=True)
class _Clip:
    audio: AsyncIterator[bytes]
    text: str
    done: asyncio.Future[None]
    cue: bool = False
    utterance: Utterance = NO_UTTERANCE
    epoch: int = 0
    spoken: SpokenWords = field(default_factory=_Voicing)
    publication: SentenceBinding | None = None


class _PreparedSpeech:
    def __init__(
        self,
        source: AsyncIterator[bytes],
        line: _Line,
        faults: FaultReporter | None,
        voicing: _Voicing,
    ) -> None:
        self._source = source
        self._voicing = voicing
        self._line = line
        self._faults = faults
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._failure: BaseException | None = None
        self._closed = False
        self._fill = asyncio.create_task(self._pull())
        self._fill.add_done_callback(self._capture)

    @property
    def text(self) -> str:
        return self._line.text

    @property
    def language(self) -> str | None:
        return self._line.language

    @property
    def sentence_index(self) -> int | None:
        return self._line.utterance.sentence_index

    @property
    def transcript_id(self) -> str | None:
        return self._line.utterance.transcript_id

    @property
    def words(self) -> Sequence[WordTiming] | None:
        return self._voicing.words

    async def _pull(self) -> None:
        async for chunk in self._source:
            self._chunks.put_nowait(chunk)

    def _capture(self, fill: asyncio.Task[None]) -> None:
        self._chunks.put_nowait(None)
        if fill.cancelled():
            return
        failure = fill.exception()
        if failure is not None:
            self._failure = failure

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._chunks.get()
        if chunk is None:
            failure = self._failure
            if failure is not None:
                raise failure
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fill.cancel()

        async def drain() -> None:
            with suppress(asyncio.CancelledError):
                await self._fill

        await close_safely(
            drain,
            source="playback",
            operation="close_prepared",
            faults=self._faults,
        )
        source = self._source
        if isinstance(source, SupportsAclose):
            await close_safely(
                source.aclose,
                source="playback",
                operation="close_prepared",
                faults=self._faults,
            )


def _fits_cue(clip: VoicedClip) -> bool:
    seconds = len(clip.pcm) / (PLAYBACK_SAMPLE_RATE * PCM16_SAMPLE_BYTES)
    return seconds <= CUE_MAX_SECONDS and clip.longest_pause_seconds <= CUE_MAX_PAUSE_SECONDS


def _faded(frame: bytes) -> bytes:
    samples = np.frombuffer(frame[:FADE_BYTES], dtype="<i2").astype(np.float64)
    ramp = np.linspace(1.0, 0.0, num=len(samples), endpoint=False)
    return (samples * ramp).astype("<i2").tobytes()


def _whole_samples(pcm: bytes) -> bytes:
    return pcm[: len(pcm) - len(pcm) % PCM16_SAMPLE_BYTES]


def _pcm_last_sample(pcm: bytes) -> int:
    if len(pcm) < PCM16_SAMPLE_BYTES:
        return 0
    return int.from_bytes(pcm[-PCM16_SAMPLE_BYTES:], "little", signed=True)


def _fresh_publication_decision(
    decision: PublicationDecision,
    binding: SentenceBinding,
    context: PublicationContext,
) -> PublicationDecision:
    if not decision.allows:
        return decision
    if binding.scope != "current":
        return decision
    checked_at = decision.checked_at
    now = context.now()
    stale = (
        checked_at is None
        or decision.clock_domain != context.clock_domain
        or not math.isfinite(checked_at)
        or not math.isfinite(now)
        or not math.isfinite(decision.uncertainty_seconds)
        or not 0 <= decision.uncertainty_seconds <= 0.05
        or checked_at > now
        or now - checked_at + decision.uncertainty_seconds > 0.25
    )
    if stale:
        return PublicationDecision("unknown", "validation_stale", now, context.clock_domain)
    return decision


async def _pcm_chunks(pcm: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(pcm), CUE_CHUNK_BYTES):
        yield pcm[start : start + CUE_CHUNK_BYTES]


class PlaybackEngine:
    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        writer: VoiceWriter,
        *,
        on_event: Callable[[PlaybackEvent], None] | None = None,
        timing: PlaybackTiming = DEFAULT_PLAYBACK_TIMING,
        diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
    ) -> None:
        self._synthesizer = synthesizer
        self._writer = writer
        self._on_event = on_event
        self._synth_timeout_seconds = timing.synth_timeout_seconds
        self._playback_timeout_seconds = timing.playback_timeout_seconds
        self._cleanup_timeout_seconds = timing.cleanup_timeout_seconds
        self._faults = diagnostics.faults
        self._journal = diagnostics.journal
        self._queue: asyncio.Queue[_Clip] = asyncio.Queue()
        self._cues: dict[str, bytes] = {}
        self._worker: asyncio.Task[None] | None = None
        self._play_tasks: set[asyncio.Task[Any]] = set()
        self._gain = 1.0
        self._burst_gain = 1.0
        self._duck_gain = 1.0
        self._thawed = asyncio.Event()
        self._thawed.set()
        self._freeze_timer: asyncio.TimerHandle | None = None
        self._freeze_max_hold_seconds = timing.freeze_max_hold_seconds
        self._last_sample = 0
        self._speaking = False
        self._generation = 0
        self._closed = False
        self._active_done: asyncio.Future[None] | None = None
        self._active_play_task: asyncio.Task[Any] | None = None
        self._stopping = 0
        self._stop_epoch = 0
        self._stop_lock = asyncio.Lock()
        self._prepared: set[PreparedSpeech] = set()
        self._turn: _OpenTurn | None = None
        self._turn_endings: set[asyncio.Task[None]] = set()
        self._clip_bytes = 0
        self._publication_context: PublicationContext | None = None
        self._publication_bindings: dict[PreparedSpeech, SentenceBinding] = {}
        self._publication_ids: set[str] = set()
        self._publication_checked: set[int] = set()

    def configure_publication(self, context: PublicationContext | None) -> None:
        self._publication_context = context

    @property
    def publication_context(self) -> PublicationContext | None:
        return self._publication_context

    def bind_publication(self, prepared: PreparedSpeech, binding: SentenceBinding) -> None:
        normalized = " ".join(prepared.text.split())
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        if binding.sentence_hash != digest:
            raise ValueError("publication sentence hash does not match prepared speech")
        if binding.sentence_index != prepared.sentence_index:
            raise ValueError("publication sentence index does not match prepared speech")
        if binding.transcript_id != prepared.transcript_id:
            raise ValueError("publication transcript identity does not match prepared speech")
        if (
            self._publication_context is not None
            and binding.meeting_id != self._publication_context.meeting_id
        ):
            raise ValueError("publication meeting identity does not match context")
        if binding.publication_id in self._publication_ids:
            raise ValueError("publication identity must be unique")
        if prepared in self._publication_bindings:
            raise ValueError("prepared speech already has a publication binding")
        self._publication_ids.add(binding.publication_id)
        self._publication_bindings[prepared] = binding

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def synthesizer(self) -> SpeechSynthesizer:
        return self._synthesizer

    @property
    def faults(self) -> FaultReporter | None:
        return self._faults

    @property
    def journal(self) -> Journal | None:
        return self._journal

    def _turn_for(self, transcript_id: str | None, language: str | None) -> SpeechTurn:
        current = self._turn
        if (
            transcript_id is not None
            and current is not None
            and current.transcript_id == transcript_id
            and current.language == language
        ):
            return current.turn
        turn = self._synthesizer.open_turn(transcript_id=transcript_id, language=language)
        if transcript_id is None:
            self._end_later(turn.finish)
            return turn
        self._end_turn_later()
        self._turn = _OpenTurn(transcript_id, language, turn)
        return turn

    def _end_turn_later(self) -> None:
        current, self._turn = self._turn, None
        if current is not None:
            self._end_later(current.turn.finish)

    def _end_later(self, end: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(
            close_safely(end, source="tts", operation="end_turn", faults=self._faults)
        )
        self._turn_endings.add(task)
        task.add_done_callback(self._turn_endings.discard)

    async def _end_turn(self, *, cancel: bool) -> None:
        current, self._turn = self._turn, None
        if current is not None:
            end = current.turn.cancel if cancel else current.turn.finish
            await close_safely(end, source="tts", operation="end_turn", faults=self._faults)
        endings = list(self._turn_endings)
        if endings:
            await asyncio.gather(*endings, return_exceptions=True)

    def _should_retry(
        self, error: BaseException, attempt: int, *, emitted: bool, generation: int
    ) -> bool:
        return (
            attempt == 0
            and not emitted
            and _is_timeout(error)
            and generation == self._generation
            and not self._closed
        )

    async def _close_stream(self, chunks: AsyncIterator[bytes] | None) -> None:
        if isinstance(chunks, SupportsAclose):
            await close_safely(
                chunks.aclose, source="playback", operation="close_stream", faults=self._faults
            )

    def louder(self) -> None:
        self._gain = min(MAX_GAIN, self._gain * GAIN_STEP)
        self._burst_gain = self._gain

    def duck(self) -> None:
        self._duck_gain = DUCK_GAIN

    def unduck(self) -> None:
        self._duck_gain = 1.0

    def freeze(self) -> None:
        if not self._thawed.is_set():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._thawed.clear()
        self._freeze_timer = loop.call_later(self._freeze_max_hold_seconds, self._release_freeze)

    def _release_freeze(self) -> None:
        self.thaw()
        if self._journal is not None:
            self._journal.record("playback_thawed", source="playback", reason="max_hold")

    def thaw(self) -> None:
        if self._freeze_timer is not None:
            self._freeze_timer.cancel()
            self._freeze_timer = None
        self._thawed.set()

    def _apply_gain(self, chunk: bytes) -> bytes:
        gain = self._burst_gain * self._duck_gain
        if gain == 1.0 or not chunk:
            return chunk
        usable = len(chunk) - (len(chunk) % PCM16_SAMPLE_BYTES)
        samples = np.frombuffer(chunk[:usable], dtype="<i2").astype(np.float64)
        scaled = np.clip(samples * gain, -32768, 32767).astype("<i2").tobytes()
        return scaled + chunk[usable:]

    async def _paced_chunks(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        iterator = chunks.__aiter__()
        while True:
            try:
                async with asyncio.timeout(self._synth_timeout_seconds):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            yield chunk

    async def _retryable_stream(
        self, line: _Line, generation: int, voicing: _Voicing
    ) -> AsyncIterator[bytes]:
        turn_id = line.utterance.transcript_id
        for attempt in range(2):
            emitted = False
            chunks: AsyncIterator[bytes] | None = None
            try:
                chunks = voicing.segment = self._turn_for(turn_id, line.language).speak(
                    line.text, sentence_index=line.utterance.sentence_index
                )
                async for chunk in self._paced_chunks(chunks):
                    if chunk:
                        emitted = True
                    yield chunk
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._should_retry(error, attempt, emitted=emitted, generation=generation):
                    self._record_retry(streaming=True)
                    continue
                fault = classify_exception("tts", error)
                if fault is error:
                    raise
                raise fault from error
            else:
                return
            finally:
                await self._close_stream(chunks)

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        await self._speak_direct(text, sentence_index, transcript_id, language, no_visual=False)

    async def speak_no_visual(
        self,
        text: str,
        *,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        await self._speak_direct(text, None, transcript_id, language, no_visual=True)

    async def _speak_direct(
        self,
        text: str,
        sentence_index: int | None,
        transcript_id: str | None,
        language: str | None,
        *,
        no_visual: bool,
    ) -> None:
        if self._closed:
            return
        if not any(char.isalnum() for char in text):
            self._emit("playback_dropped", text, "nothing_to_say")
            return
        utterance = Utterance(transcript_id, sentence_index, no_visual=no_visual)
        line = _Line(text, utterance, language)
        voicing = _Voicing()
        await self._play_queued(
            self._retryable_stream(line, self._generation, voicing),
            text,
            utterance=utterance,
            spoken=voicing,
        )

    async def prepare(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech | None:
        if self._closed:
            return None
        if not any(char.isalnum() for char in text):
            self._emit("playback_dropped", text, "nothing_to_say")
            return None
        line = _Line(text, Utterance(transcript_id, sentence_index), language)
        voicing = _Voicing()
        source = self._retryable_stream(line, self._generation, voicing)
        prepared = _PreparedSpeech(source, line, self._faults, voicing)
        self._prepared.add(prepared)
        return prepared

    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        self._prepared.discard(prepared)
        if self._closed:
            await prepared.aclose()
            return
        await self._play_queued(
            prepared,
            prepared.text,
            utterance=Utterance(
                prepared.transcript_id,
                prepared.sentence_index,
                self._publication_bindings.pop(prepared, None),
            ),
            spoken=prepared,
        )

    async def prepare_cue(self, text: str) -> None:
        if self._closed or text in self._cues:
            return
        for _attempt in range(CUE_RENDER_ATTEMPTS):
            clip = voiced_clip(await self._render(text), PLAYBACK_SAMPLE_RATE)
            if clip is not None and _fits_cue(clip):
                self._cues[text] = clip.pcm
                return
        if self._journal is not None:
            self._journal.record(
                "thinking_cue_rejected",
                source="playback",
                chars=len(text),
                attempts=CUE_RENDER_ATTEMPTS,
            )

    async def _render(self, text: str) -> bytes:
        turn = self._synthesizer.open_turn()
        chunks = turn.speak(text)
        try:
            async with asyncio.timeout(self._synth_timeout_seconds):
                return b"".join([chunk async for chunk in chunks])
        finally:
            await self._close_stream(chunks)
            self._end_later(turn.finish)

    async def play_cue(self, text: str, *, transcript_id: str | None = None) -> bool:
        pcm = self._cues.get(text)
        if pcm is None or self._closed or self._speaking or not self._queue.empty():
            return False
        await self._play_queued(
            _pcm_chunks(pcm), text, cue=True, utterance=Utterance(transcript_id)
        )
        return True

    async def _play_queued(
        self,
        audio: AsyncIterator[bytes],
        text: str,
        *,
        cue: bool = False,
        utterance: Utterance = NO_UTTERANCE,
        spoken: SpokenWords | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        clip = _Clip(
            audio,
            text,
            done,
            cue,
            utterance,
            self._stop_epoch,
            spoken or _Voicing(),
            utterance.publication,
        )
        await self._queue.put(clip)
        self._ensure_worker()
        try:
            await done
        except asyncio.CancelledError:
            done.cancel()
            if self._active_done is done and self._active_play_task is not None:
                self._active_play_task.cancel()
            raise

    async def stop(self) -> None:
        self._duck_gain = 1.0
        self.thaw()
        self._stopping += 1
        try:
            async with self._stop_lock:
                await self._stop_active()
        finally:
            self._stop_epoch += 1
            self._stopping -= 1

    async def _stop_active(self) -> None:
        self._generation += 1
        active = self._active_play_task
        dropped, closing = self._take_queued()
        try:
            if active is not None:
                await self._halt(active)
            await self._cancelled_tasks_cleanup()
        finally:
            self._play_tasks.difference_update(closing)
            self._report_dropped(dropped)
        await self._drop_queued()
        await self._end_turn(cancel=True)
        await self._settle_voice()

    def _take_queued(self) -> tuple[list[_Clip], list[asyncio.Task[Any]]]:
        dropped: list[_Clip] = []
        closing: list[asyncio.Task[Any]] = []
        while True:
            try:
                clip = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped.append(clip)
            task = asyncio.create_task(self._close_audio(clip.audio))
            closing.append(task)
            self._play_tasks.add(task)
        return dropped, closing

    def _report_dropped(self, dropped: list[_Clip]) -> None:
        for clip in dropped:
            self._settle_done(clip.done)
            self._emit_dropped(clip, "interrupted", heard="")

    async def _drop_queued(self) -> None:
        dropped, closing = self._take_queued()
        self._report_dropped(dropped)
        await self._cancelled_tasks_cleanup()
        self._play_tasks.difference_update(closing)

    async def _halt(self, active: asyncio.Task[Any]) -> None:
        _, pending = await asyncio.wait({active}, timeout=HALT_SECONDS)
        if pending:
            active.cancel()

    async def _close_audio(self, audio: AsyncIterator[bytes]) -> None:
        if isinstance(audio, SupportsAclose):
            await close_safely(
                audio.aclose,
                source="playback",
                operation="close_audio",
                faults=self._faults,
                timeout_seconds=self._cleanup_timeout_seconds,
            )

    async def _consume_audio(self, clip: _Clip, generation: int, *, gain: bool = True) -> bool:
        audio = clip.audio
        pending = b""
        try:
            async for chunk in audio:
                pending += chunk
                while len(pending) >= FRAME_BYTES:
                    frame, pending = pending[:FRAME_BYTES], pending[FRAME_BYTES:]
                    if generation != self._generation:
                        if self._publication_context is not None and not clip.cue:
                            return False
                        await self._writer.write(_faded(frame))
                        return False
                    await self._write_frame(frame, clip, generation, gain=gain)
            rest = _whole_samples(pending)
            if not rest:
                return generation == self._generation
            if generation != self._generation:
                if self._publication_context is not None and not clip.cue:
                    return False
                await self._writer.write(_faded(rest))
                return False
            await self._write_frame(rest, clip, generation, gain=gain)
        except asyncio.CancelledError:
            await self._fade_out()
            raise
        else:
            return True
        finally:
            await self._close_audio(audio)

    async def _write_frame(self, frame: bytes, clip: _Clip, generation: int, *, gain: bool) -> None:
        await self._thawed.wait()
        should_check = (
            self._publication_context is not None
            and not clip.cue
            and id(clip) not in self._publication_checked
        )
        if should_check:
            self._publication_checked.add(id(clip))
            decision = await self._publication_decision(clip)
            if generation != self._generation:
                raise _PublicationPrevented("unknown", "cancelled")
            if not self._thawed.is_set():
                raise _PublicationPrevented("unknown", "frozen")
            if self._closed:
                raise _PublicationPrevented("unknown", "closed")
            if decision is None:
                raise _PublicationPrevented("unknown", "unresolved")
            if not decision.allows:
                raise _PublicationPrevented(decision.validity, decision.reason)
        pcm = self._apply_gain(frame) if gain else frame
        self._last_sample = _pcm_last_sample(pcm)
        await self._writer.write(pcm)
        if self._clip_bytes == 0:
            self._emit("playback_audio_started", clip.text, cue=clip.cue, utterance=clip.utterance)
        self._clip_bytes += len(pcm)

    async def _publication_decision(self, clip: _Clip) -> PublicationDecision | None:
        context = self._publication_context
        binding = clip.publication
        if context is None:
            return None
        if binding is None:
            if clip.utterance.no_visual:
                return PublicationDecision(
                    "valid", "declared_no_visual", context.now(), context.clock_domain
                )
            return None
        gate = context.gate
        if gate is None:
            if binding.scope in ("historical", "no_visual"):
                return PublicationDecision(
                    "valid", "safe_scope_without_gate", context.now(), context.clock_domain
                )
            return None
        return await self._run_publication_gate(context, binding, gate)

    async def _run_publication_gate(
        self,
        context: PublicationContext,
        binding: SentenceBinding,
        gate: Callable[[SentenceBinding], Awaitable[PublicationDecision | None]],
    ) -> PublicationDecision | None:
        if not math.isfinite(context.timeout_seconds) or context.timeout_seconds <= 0:
            return None
        try:
            async with asyncio.timeout(context.timeout_seconds):
                decision = await gate(binding)
                effective = (
                    None
                    if decision is None
                    else _fresh_publication_decision(decision, binding, context)
                )
                if self._journal is not None:
                    self._journal.record(
                        "publication_gate_decided",
                        source="playback",
                        publication_id=binding.publication_id,
                        validity=None if effective is None else effective.validity,
                        reason="unknown" if effective is None else effective.reason,
                        checked_at=None if effective is None else effective.checked_at,
                        clock_domain=None if effective is None else effective.clock_domain,
                    )
                return effective
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if self._journal is not None:
                self._journal.record(
                    "publication_gate_expired",
                    source="playback",
                    publication_id=binding.publication_id,
                )
            return None
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._record_fault("playback", error)
            if self._journal is not None:
                self._journal.record(
                    "publication_gate_failed",
                    source="playback",
                    publication_id=binding.publication_id,
                    error=type(error).__name__,
                )
            return None

    async def _fade_out(self) -> None:
        if self._last_sample == 0:
            return
        ramp = np.linspace(self._last_sample, 0.0, num=FADE_SAMPLE_COUNT)
        self._last_sample = 0
        await self._writer.write(ramp.astype("<i2").tobytes())

    async def _cancelled_tasks_cleanup(self) -> None:
        tasks = [task for task in self._play_tasks if not task.done()]
        if not tasks:
            return
        with suppress(TimeoutError):
            async with asyncio.timeout(self._cleanup_timeout_seconds):
                await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.stop()
        worker, self._worker = self._worker, None
        prepared, self._prepared = self._prepared, set()
        self._publication_bindings.clear()
        try:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            for speech in prepared:
                await close_safely(
                    speech.aclose,
                    source="playback",
                    operation="close_prepared",
                    faults=self._faults,
                )
        finally:
            await self._end_turn(cancel=True)
            await close_safely(self._writer.close, source="playback", faults=self._faults)
            await close_safely(self._synthesizer.aclose, source="tts", faults=self._faults)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
            self._worker.add_done_callback(self._check_worker)

    def _check_worker(self, worker: asyncio.Task[None]) -> None:
        if worker.cancelled():
            return
        error = worker.exception()
        if error is not None:
            self._record_fault("playback", error)
            self._emit("playback_failed", "", f"{type(error).__name__}: {error}")
        if worker is self._worker:
            self._speaking = False

    def _emit(
        self,
        kind: PlaybackEventKind,
        text: str,
        detail: str = "",
        *,
        cue: bool = False,
        utterance: Utterance = NO_UTTERANCE,
    ) -> None:
        self._publish(PlaybackEvent(kind, text, detail, cue, utterance))

    def _emit_dropped(self, clip: _Clip, detail: str, *, heard: str | None) -> None:
        self._publish(
            PlaybackEvent("playback_dropped", clip.text, detail, clip.cue, clip.utterance, heard)
        )

    def _publish(self, event: PlaybackEvent) -> None:
        if self._on_event is None:
            return
        guard_observer(
            partial(self._on_event, event),
            sink=ConsumerSink(faults=self._faults, journal=self._journal),
            source="playback",
            detail="playback_event",
        )

    def _record_fault(self, source: Subsystem, error: BaseException) -> None:
        if self._faults is None:
            return
        self._faults.record(classify_exception(source, error))

    def _record_retry(self, *, streaming: bool) -> None:
        if self._journal is not None:
            self._journal.record(
                "synthesis_retried",
                source="playback",
                attempt=2,
                streaming=streaming,
                reason="timeout",
            )

    def _settle_done(self, done: asyncio.Future[None]) -> None:
        if not done.cancelled() and not done.done():
            done.set_result(None)

    async def _settle_voice(self) -> None:
        if not self._speaking:
            return
        scope = asyncio.timeout(self._cleanup_timeout_seconds)
        try:
            async with scope:
                await self._writer.drain()
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            self._record_fault("playback", error)
            if scope.expired():
                return
        except (OSError, PlaybackError) as error:
            self._record_fault("playback", error)
        active = self._active_play_task
        if (active is None or active.done()) and self._queue.empty():
            self._speaking = False

    async def _drop_stale(self, clip: _Clip) -> None:
        await self._close_audio(clip.audio)
        self._settle_done(clip.done)
        self._emit_dropped(clip, "cancelled" if clip.done.cancelled() else "interrupted", heard="")

    def _must_raise_cancel(self, play_task: asyncio.Task[Any]) -> bool:
        current = asyncio.current_task()
        return current is None or current.cancelling() > 0 or not play_task.cancelled()

    async def _play_one(self, play_task: asyncio.Task[bool], clip: _Clip) -> None:
        text, cue, utterance = clip.text, clip.cue, clip.utterance
        try:
            async with asyncio.timeout(self._playback_timeout_seconds):
                played = await play_task
        except TimeoutError:
            play_task.cancel()
            with suppress(TimeoutError):
                async with asyncio.timeout(self._cleanup_timeout_seconds):
                    await play_task
            self._emit("playback_failed", text, "playback timed out", cue=cue, utterance=utterance)
            self._record_fault("playback", TimeoutError("playback timed out"))
        except asyncio.CancelledError:
            if self._must_raise_cancel(play_task):
                raise
            self._emit_interrupted(clip)
        except (OSError, PlaybackError, ProviderFault) as error:
            self._emit(
                "playback_failed",
                text,
                f"{type(error).__name__}: {error}",
                cue=cue,
                utterance=utterance,
            )
            self._record_fault("playback", error)
        except _PublicationPrevented as prevented:
            self._emit_dropped(clip, str(prevented), heard="")
        else:
            if played:
                self._emit("playback_finished", text, cue=cue, utterance=utterance)
            else:
                self._emit_interrupted(clip)
        finally:
            self._publication_checked.discard(id(clip))
            self._play_tasks.discard(play_task)
            self._active_done = None
            self._active_play_task = None
            self._settle_done(clip.done)

    def _emit_interrupted(self, clip: _Clip) -> None:
        seconds = self._clip_bytes / BYTES_PER_SECOND
        self._emit_dropped(clip, "interrupted", heard=heard_text(clip.spoken.words, seconds))

    async def _next_clip(self) -> _Clip:
        if self._speaking and self._queue.empty():
            try:
                async with asyncio.timeout(self._writer.buffer_seconds):
                    return await self._queue.get()
            except TimeoutError:
                await self._settle_voice()
        return await self._queue.get()

    async def _run(self) -> None:
        while True:
            clip = await self._next_clip()
            generation = self._generation
            if clip.done.cancelled():
                await self._close_audio(clip.audio)
                continue
            if self._stopping or clip.epoch != self._stop_epoch:
                await self._drop_stale(clip)
                continue
            if self._closed:
                await self._drop_stale(clip)
                continue
            was_speaking = self._speaking
            self._speaking = True
            if not was_speaking and not clip.cue:
                self._burst_gain = self._gain
                self._gain = 1.0
            self._emit("playback_started", clip.text, cue=clip.cue, utterance=clip.utterance)
            self._clip_bytes = 0
            play_task = asyncio.create_task(
                self._consume_audio(clip, generation, gain=not clip.cue)
            )
            self._play_tasks.add(play_task)
            self._active_done = clip.done
            self._active_play_task = play_task
            await self._play_one(play_task, clip)
