import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES, VoicedClip, voiced_clip
from fakeyou.foundation.protocols import SupportsAclose
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.observability.cleanup import close_safely
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import ConsumerSink, FaultReporter, guard_observer
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.speech.contract import PreparedSpeech, SpeechSynthesizer

PlaybackEventKind = Literal[
    "playback_started", "playback_finished", "playback_failed", "playback_dropped"
]


@dataclass(frozen=True, slots=True)
class Utterance:
    transcript_id: str | None = None
    sentence_index: int | None = None


NO_UTTERANCE = Utterance()


@dataclass(frozen=True, slots=True)
class PlaybackEvent:
    kind: PlaybackEventKind
    text: str = ""
    detail: str = ""
    cue: bool = False
    utterance: Utterance = NO_UTTERANCE


GAIN_STEP = 1.2
MAX_GAIN = 2.4
FRAME_SECONDS = 0.02
FADE_SECONDS = 0.01
HALT_SECONDS = 0.1
FRAME_BYTES = round(PLAYBACK_SAMPLE_RATE * FRAME_SECONDS) * PCM16_SAMPLE_BYTES
FADE_BYTES = round(PLAYBACK_SAMPLE_RATE * FADE_SECONDS) * PCM16_SAMPLE_BYTES
FADE_SAMPLE_COUNT = round(PLAYBACK_SAMPLE_RATE * FADE_SECONDS)


SYNTH_TIMEOUT_SECONDS = 45.0
CUE_CHUNK_BYTES = 4800
CUE_MAX_SECONDS = 1.2
CUE_MAX_PAUSE_SECONDS = 0.3
CUE_RENDER_ATTEMPTS = 3
PLAYBACK_TIMEOUT_SECONDS = 120.0
CLEANUP_TIMEOUT_SECONDS = 5.0


class PlaybackError(RuntimeError):
    pass


def _is_timeout(error: BaseException) -> bool:
    return isinstance(error, (TimeoutError, ProviderFault)) and (
        not isinstance(error, ProviderFault) or error.kind == "timeout"
    )


@dataclass(frozen=True, slots=True)
class PlaybackTiming:
    synth_timeout_seconds: float = SYNTH_TIMEOUT_SECONDS
    playback_timeout_seconds: float = PLAYBACK_TIMEOUT_SECONDS
    cleanup_timeout_seconds: float = CLEANUP_TIMEOUT_SECONDS


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
class CuePlayback(Protocol):
    async def prepare_cue(self, text: str) -> None: ...

    async def play_cue(self, text: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class _Clip:
    audio: AsyncIterator[bytes]
    text: str
    done: asyncio.Future[None]
    cue: bool = False
    utterance: Utterance = NO_UTTERANCE
    epoch: int = 0


class _PreparedSpeech:
    def __init__(
        self,
        source: AsyncIterator[bytes],
        text: str,
        language: str | None,
        utterance: Utterance,
        faults: FaultReporter | None,
    ) -> None:
        self._source = source
        self._text = text
        self._language = language
        self._utterance = utterance
        self._faults = faults
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._failure: BaseException | None = None
        self._closed = False
        self._fill = asyncio.create_task(self._pull())
        self._fill.add_done_callback(self._capture)

    @property
    def text(self) -> str:
        return self._text

    @property
    def language(self) -> str | None:
        return self._language

    @property
    def sentence_index(self) -> int | None:
        return self._utterance.sentence_index

    @property
    def transcript_id(self) -> str | None:
        return self._utterance.transcript_id

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

    def _stream_extra(
        self, sentence_index: int | None, transcript_id: str | None, language: str | None
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if sentence_index is not None:
            extra["sentence_index"] = sentence_index
        if transcript_id is not None:
            extra["transcript_id"] = transcript_id
        if language is not None:
            extra["language"] = language
        return extra

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

    def _apply_gain(self, chunk: bytes) -> bytes:
        if self._burst_gain == 1.0 or not chunk:
            return chunk
        usable = len(chunk) - (len(chunk) % PCM16_SAMPLE_BYTES)
        samples = np.frombuffer(chunk[:usable], dtype="<i2").astype(np.float64)
        scaled = np.clip(samples * self._burst_gain, -32768, 32767).astype("<i2").tobytes()
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
        self,
        text: str,
        sentence_index: int | None,
        transcript_id: str | None,
        language: str | None,
        generation: int,
    ) -> AsyncIterator[bytes]:
        for attempt in range(2):
            emitted = False
            chunks: AsyncIterator[bytes] | None = None
            try:
                chunks = self._synthesizer.stream(
                    text, **self._stream_extra(sentence_index, transcript_id, language)
                )
                if not hasattr(chunks, "__aiter__"):
                    raise TypeError("stream() must return an async iterator")
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
        if self._closed:
            return
        if not any(char.isalnum() for char in text):
            self._emit("playback_dropped", text, "nothing_to_say")
            return
        generation = self._generation
        await self._play_queued(
            self._retryable_stream(text, sentence_index, transcript_id, language, generation),
            text,
            utterance=Utterance(transcript_id, sentence_index),
        )
        return

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
        generation = self._generation
        source = self._retryable_stream(text, sentence_index, transcript_id, language, generation)
        prepared = _PreparedSpeech(
            source,
            text,
            language,
            Utterance(transcript_id, sentence_index),
            self._faults,
        )
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
            utterance=Utterance(prepared.transcript_id, prepared.sentence_index),
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
        chunks = self._synthesizer.stream(text)
        try:
            async with asyncio.timeout(self._synth_timeout_seconds):
                return b"".join([chunk async for chunk in chunks])
        finally:
            await self._close_stream(chunks)

    async def play_cue(self, text: str) -> bool:
        pcm = self._cues.get(text)
        if pcm is None or self._closed or self._speaking or not self._queue.empty():
            return False
        await self._play_queued(_pcm_chunks(pcm), text, cue=True)
        return True

    async def _play_queued(
        self,
        audio: AsyncIterator[bytes],
        text: str,
        *,
        cue: bool = False,
        utterance: Utterance = NO_UTTERANCE,
    ) -> None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        await self._queue.put(_Clip(audio, text, done, cue, utterance, self._stop_epoch))
        self._ensure_worker()
        try:
            await done
        except asyncio.CancelledError:
            done.cancel()
            if self._active_done is done and self._active_play_task is not None:
                self._active_play_task.cancel()
            raise

    async def stop(self) -> None:
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
            self._emit(
                "playback_dropped",
                clip.text,
                "interrupted",
                cue=clip.cue,
                utterance=clip.utterance,
            )

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

    async def _consume_audio(
        self, audio: AsyncIterator[bytes], generation: int, *, gain: bool = True
    ) -> bool:
        pending = b""
        try:
            async for chunk in audio:
                pending += chunk
                while len(pending) >= FRAME_BYTES:
                    frame, pending = pending[:FRAME_BYTES], pending[FRAME_BYTES:]
                    if generation != self._generation:
                        await self._writer.write(_faded(frame))
                        return False
                    await self._write_frame(frame, gain=gain)
            rest = _whole_samples(pending)
            if not rest:
                return generation == self._generation
            if generation != self._generation:
                await self._writer.write(_faded(rest))
                return False
            await self._write_frame(rest, gain=gain)
        except asyncio.CancelledError:
            await self._fade_out()
            raise
        else:
            return True
        finally:
            await self._close_audio(audio)

    async def _write_frame(self, frame: bytes, *, gain: bool) -> None:
        pcm = self._apply_gain(frame) if gain else frame
        self._last_sample = _pcm_last_sample(pcm)
        await self._writer.write(pcm)

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
        if self._on_event is None:
            return
        guard_observer(
            partial(self._on_event, PlaybackEvent(kind, text, detail, cue, utterance)),
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
        self._emit(
            "playback_dropped",
            clip.text,
            "cancelled" if clip.done.cancelled() else "interrupted",
            cue=clip.cue,
            utterance=clip.utterance,
        )

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
            self._emit("playback_dropped", text, "interrupted", cue=cue, utterance=utterance)
        except (OSError, PlaybackError, ProviderFault) as error:
            self._emit(
                "playback_failed",
                text,
                f"{type(error).__name__}: {error}",
                cue=cue,
                utterance=utterance,
            )
            self._record_fault("playback", error)
        else:
            if played:
                self._emit("playback_finished", text, cue=cue, utterance=utterance)
            else:
                self._emit("playback_dropped", text, "interrupted", cue=cue, utterance=utterance)
        finally:
            self._play_tasks.discard(play_task)
            self._active_done = None
            self._active_play_task = None
            self._settle_done(clip.done)

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
            play_task = asyncio.create_task(
                self._consume_audio(clip.audio, generation, gain=not clip.cue)
            )
            self._play_tasks.add(play_task)
            self._active_done = clip.done
            self._active_play_task = play_task
            await self._play_one(play_task, clip)
