import asyncio
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from fakeyou.config.settings import DEFAULTS
from fakeyou.foundation.invariants import PCM_CHANNELS, PCM_FORMAT, PLAYBACK_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES, VoicedClip, voiced_clip
from fakeyou.foundation.protocols import SupportsAclose
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.observability.cleanup import close_safely
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import ConsumerSink, FaultReporter, guard_observer
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.speech.contract import SpeechSynthesizer

PlaybackEventKind = Literal[
    "playback_started", "playback_finished", "playback_failed", "playback_dropped", "mute_failed"
]


@dataclass(frozen=True, slots=True)
class PlaybackEvent:
    kind: PlaybackEventKind
    text: str = ""
    detail: str = ""
    cue: bool = False


PlayStream = Callable[[AsyncIterator[bytes]], Coroutine[Any, Any, None]]
GAIN_STEP = 1.2
MAX_GAIN = 2.4


class SetMuted(Protocol):
    async def __call__(self, *, muted: bool) -> None: ...


SYNTH_TIMEOUT_SECONDS = 45.0
CUE_CHUNK_BYTES = 4800
CUE_MAX_SECONDS = 1.2
CUE_MAX_PAUSE_SECONDS = 0.3
CUE_RENDER_ATTEMPTS = 3
PLAYBACK_TIMEOUT_SECONDS = 120.0
CLEANUP_TIMEOUT_SECONDS = 5.0


class PlaybackError(RuntimeError):
    pass


async def _cleanup_paplay_stream(
    proc: asyncio.subprocess.Process, stderr_task: asyncio.Task[bytes] | None
) -> None:
    if proc.returncode is None:
        await _cleanup_process(proc)
    if stderr_task is not None and not stderr_task.done():
        stderr_task.cancel()
        with suppress(asyncio.CancelledError):
            await stderr_task


async def _cleanup_process(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None and not proc.stdin.transport.is_closing():
        proc.stdin.transport.abort()
    with suppress(ProcessLookupError):
        proc.kill()
    try:
        async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
            await proc.wait()
    except TimeoutError:
        pass


def _is_timeout(error: BaseException) -> bool:
    return isinstance(error, (TimeoutError, ProviderFault)) and (
        not isinstance(error, ProviderFault) or error.kind == "timeout"
    )


def create_paplay_stream_runner(*, sink: str, rate: int) -> PlayStream:
    async def play(chunks: AsyncIterator[bytes]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "paplay",
                f"--device={sink}",
                f"--format={PCM_FORMAT}",
                f"--rate={rate}",
                f"--channels={PCM_CHANNELS}",
                "--raw",
                stdin=PIPE,
                stdout=DEVNULL,
                stderr=PIPE,
            )
        except OSError as error:
            raise PlaybackError(f"paplay is unavailable: {error}") from error
        stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr is not None else None

        try:
            if proc.stdin is None:
                raise PlaybackError("paplay stdin is unavailable")
            async for chunk in chunks:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()
            returncode = await proc.wait()
            stderr = await stderr_task if stderr_task is not None else b""
        except (OSError, BrokenPipeError) as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise asyncio.CancelledError from error
            raise PlaybackError(f"paplay stream failed: {error}") from error
        finally:
            cleanup_task = asyncio.create_task(_cleanup_paplay_stream(proc, stderr_task))
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise
        if returncode != 0:
            raise PlaybackError(
                f"paplay exited with {returncode}: {stderr.decode(errors='replace')[:200]}"
            )

    return play


def create_pactl_muter(
    *, sink: str, timeout_seconds: float = DEFAULTS.audio.pactl_timeout_seconds
) -> SetMuted:
    async def apply(*, muted: bool) -> None:
        value = "1" if muted else "0"
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl",
                "set-sink-mute",
                sink,
                value,
                stdout=DEVNULL,
                stderr=PIPE,
            )
        except OSError as error:
            raise PlaybackError(f"pactl is unavailable: {error}") from error
        try:
            async with asyncio.timeout(timeout_seconds):
                _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise PlaybackError(
                    f"pactl sink mute failed: {stderr.decode(errors='replace')[:200]}"
                )
        except TimeoutError as error:
            raise PlaybackError(f"pactl sink mute timed out after {timeout_seconds}s") from error
        finally:
            if proc.returncode is None:
                await _cleanup_process(proc)

    return apply


@dataclass(frozen=True, slots=True)
class PlaybackTiming:
    tail_seconds: float = 0.5
    synth_timeout_seconds: float = SYNTH_TIMEOUT_SECONDS
    playback_timeout_seconds: float = PLAYBACK_TIMEOUT_SECONDS
    cleanup_timeout_seconds: float = CLEANUP_TIMEOUT_SECONDS


DEFAULT_PLAYBACK_TIMING = PlaybackTiming()


@dataclass(frozen=True, slots=True)
class PlaybackBackend:
    set_muted: SetMuted
    stream_audio: PlayStream


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
    ) -> None: ...

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


def _fits_cue(clip: VoicedClip) -> bool:
    seconds = len(clip.pcm) / (PLAYBACK_SAMPLE_RATE * PCM16_SAMPLE_BYTES)
    return seconds <= CUE_MAX_SECONDS and clip.longest_pause_seconds <= CUE_MAX_PAUSE_SECONDS


async def _pcm_chunks(pcm: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(pcm), CUE_CHUNK_BYTES):
        yield pcm[start : start + CUE_CHUNK_BYTES]


class PlaybackEngine:
    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        backend: PlaybackBackend,
        *,
        on_event: Callable[[PlaybackEvent], None] | None = None,
        timing: PlaybackTiming = DEFAULT_PLAYBACK_TIMING,
        diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
    ) -> None:
        self._synthesizer = synthesizer
        self._set_muted = backend.set_muted
        self._on_event = on_event
        self._tail_seconds = timing.tail_seconds
        self._synth_timeout_seconds = timing.synth_timeout_seconds
        self._stream_audio = backend.stream_audio
        self._playback_timeout_seconds = timing.playback_timeout_seconds
        self._cleanup_timeout_seconds = timing.cleanup_timeout_seconds
        self._faults = diagnostics.faults
        self._journal = diagnostics.journal
        self._queue: asyncio.Queue[_Clip] = asyncio.Queue()
        self._cues: dict[str, bytes] = {}
        self._worker: asyncio.Task[None] | None = None
        self._play_tasks: set[asyncio.Task[Any]] = set()
        self._muted = True
        self._gain = 1.0
        self._speaking = False
        self._generation = 0
        self._closed = False
        self._active_done: asyncio.Future[None] | None = None
        self._active_play_task: asyncio.Task[Any] | None = None

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
        self, sentence_index: int | None, transcript_id: str | None
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if sentence_index is not None:
            extra["sentence_index"] = sentence_index
        if transcript_id is not None:
            extra["transcript_id"] = transcript_id
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

    def _apply_gain(self, chunk: bytes) -> bytes:
        if self._gain == 1.0 or not chunk:
            return chunk
        usable = len(chunk) - (len(chunk) % PCM16_SAMPLE_BYTES)
        samples = np.frombuffer(chunk[:usable], dtype="<i2").astype(np.float64)
        scaled = np.clip(samples * self._gain, -32768, 32767).astype("<i2").tobytes()
        return scaled + chunk[usable:]

    async def _paced_chunks(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        iterator = chunks.__aiter__()
        while True:
            try:
                async with asyncio.timeout(self._synth_timeout_seconds):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            yield self._apply_gain(chunk)

    async def _retryable_stream(
        self,
        text: str,
        sentence_index: int | None,
        transcript_id: str | None,
        generation: int,
    ) -> AsyncIterator[bytes]:
        for attempt in range(2):
            emitted = False
            chunks: AsyncIterator[bytes] | None = None
            try:
                chunks = self._synthesizer.stream(
                    text, **self._stream_extra(sentence_index, transcript_id)
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
    ) -> None:
        if self._closed:
            return
        generation = self._generation
        if generation != self._generation or self._closed:
            self._emit("playback_dropped", text, "interrupted")
            return
        await self._play_queued(
            self._retryable_stream(text, sentence_index, transcript_id, generation), text
        )
        return

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
        self, audio: AsyncIterator[bytes], text: str, *, cue: bool = False
    ) -> None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        await self._queue.put(_Clip(audio, text, done, cue))
        self._ensure_worker()
        try:
            await done
        except asyncio.CancelledError:
            done.cancel()
            if self._active_done is done and self._active_play_task is not None:
                self._active_play_task.cancel()
            raise

    async def stop(self) -> None:
        self._generation += 1
        for task in self._play_tasks:
            task.cancel()
        dropped = []
        closing = []
        while True:
            try:
                clip = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped.append(clip)
            task = asyncio.create_task(self._close_audio(clip.audio))
            closing.append(task)
            self._play_tasks.add(task)
        try:
            await self._cancelled_tasks_cleanup()
        finally:
            self._play_tasks.difference_update(closing)
            for clip in dropped:
                self._settle_done(clip.done)
                self._emit("playback_dropped", clip.text, "interrupted", cue=clip.cue)
        await self._mute()

    async def _close_audio(self, audio: AsyncIterator[bytes]) -> None:
        if isinstance(audio, SupportsAclose):
            await close_safely(
                audio.aclose,
                source="playback",
                operation="close_audio",
                faults=self._faults,
                timeout_seconds=self._cleanup_timeout_seconds,
            )

    async def _consume_audio(self, audio: AsyncIterator[bytes]) -> None:
        try:
            await self._stream_audio(audio)
        finally:
            await self._close_audio(audio)

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
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
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

    def _emit(
        self, kind: PlaybackEventKind, text: str, detail: str = "", *, cue: bool = False
    ) -> None:
        if self._on_event is None:
            return
        guard_observer(
            partial(self._on_event, PlaybackEvent(kind, text, detail, cue)),
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

    async def _mute(self) -> None:
        if self._muted:
            return
        try:
            await self._set_muted(muted=True)
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, PlaybackError) as error:
            self._emit("mute_failed", "", f"{type(error).__name__}: {error}")
            self._record_fault("playback", error)
            return
        self._muted = True
        self._gain = 1.0
        self._speaking = False

    async def _unmute(self) -> None:
        if not self._muted:
            return
        try:
            await self._set_muted(muted=False)
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, PlaybackError) as error:
            self._emit("mute_failed", "", f"{type(error).__name__}: {error}")
            self._record_fault("playback", error)
        self._muted = False
        self._speaking = True

    def _settle_done(self, done: asyncio.Future[None]) -> None:
        if not done.cancelled() and not done.done():
            done.set_result(None)

    async def _drop_stale(self, clip: _Clip) -> None:
        await self._close_audio(clip.audio)
        await self._mute()
        self._settle_done(clip.done)
        self._emit(
            "playback_dropped",
            clip.text,
            "cancelled" if clip.done.cancelled() else "interrupted",
            cue=clip.cue,
        )

    def _must_raise_cancel(self, play_task: asyncio.Task[Any]) -> bool:
        current = asyncio.current_task()
        return current is None or current.cancelling() > 0 or not play_task.cancelled()

    async def _play_one(self, play_task: asyncio.Task[Any], clip: _Clip) -> None:
        text, cue = clip.text, clip.cue
        try:
            async with asyncio.timeout(self._playback_timeout_seconds):
                await play_task
        except TimeoutError:
            play_task.cancel()
            with suppress(TimeoutError):
                async with asyncio.timeout(self._cleanup_timeout_seconds):
                    await play_task
            self._emit("playback_failed", text, "playback timed out", cue=cue)
            self._record_fault("playback", TimeoutError("playback timed out"))
        except asyncio.CancelledError:
            if self._must_raise_cancel(play_task):
                raise
            self._emit("playback_dropped", text, "interrupted", cue=cue)
        except (OSError, PlaybackError, ProviderFault) as error:
            self._emit("playback_failed", text, f"{type(error).__name__}: {error}", cue=cue)
            self._record_fault("playback", error)
        else:
            self._emit("playback_finished", text, cue=cue)
        finally:
            self._play_tasks.discard(play_task)
            self._active_done = None
            self._active_play_task = None
            self._settle_done(clip.done)

    async def _idle_tail(self) -> None:
        if not self._queue.empty():
            return
        generation = self._generation
        await asyncio.sleep(self._tail_seconds)
        if generation == self._generation and self._queue.empty():
            await self._mute()

    async def _run(self) -> None:
        while True:
            clip = await self._queue.get()
            generation = self._generation
            if clip.done.cancelled():
                await self._close_audio(clip.audio)
                continue
            unmuted = False
            try:
                await self._unmute()
                unmuted = True
            finally:
                if not unmuted:
                    await self._close_audio(clip.audio)
                    self._settle_done(clip.done)
            if generation != self._generation or clip.done.cancelled() or self._closed:
                await self._drop_stale(clip)
                continue
            self._emit("playback_started", clip.text, cue=clip.cue)
            play_task = asyncio.create_task(self._consume_audio(clip.audio))
            self._play_tasks.add(play_task)
            self._active_done = clip.done
            self._active_play_task = play_task
            await self._play_one(play_task, clip)
            await self._idle_tail()
