import asyncio
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from fakeyou.foundation.invariants import PCM_CHANNELS, PCM_FORMAT
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_exception

PlaybackEventKind = Literal[
    "playback_started", "playback_finished", "playback_failed", "playback_dropped", "mute_failed"
]


@dataclass(frozen=True, slots=True)
class PlaybackEvent:
    kind: PlaybackEventKind
    text: str = ""
    detail: str = ""


class SpeechSynthesizer(Protocol):
    def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


PlayStream = Callable[[AsyncIterator[bytes]], Coroutine[Any, Any, None]]
SetMuted = Callable[[bool], Coroutine[Any, Any, None]]

SYNTH_TIMEOUT_SECONDS = 45.0
PLAYBACK_TIMEOUT_SECONDS = 120.0
CLEANUP_TIMEOUT_SECONDS = 5.0


class PlaybackError(RuntimeError):
    pass


async def _cleanup_process(proc: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        proc.kill()
    if proc.stdin is not None:
        with suppress(Exception):
            proc.stdin.close()
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

            async def cleanup() -> None:
                if proc.returncode is None:
                    await _cleanup_process(proc)
                if stderr_task is not None and not stderr_task.done():
                    stderr_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stderr_task

            cleanup_task = asyncio.create_task(cleanup())
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


def create_pactl_muter(*, sink: str) -> SetMuted:
    async def apply(muted: bool) -> None:
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
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise PlaybackError(
                    f"pactl sink mute failed: {stderr.decode(errors='replace')[:200]}"
                )
        finally:
            if proc.returncode is None:
                await _cleanup_process(proc)

    return apply


class PlaybackEngine:
    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        *,
        set_muted: SetMuted,
        on_event: Callable[[PlaybackEvent], None] | None = None,
        tail_seconds: float = 0.5,
        synth_timeout_seconds: float = SYNTH_TIMEOUT_SECONDS,
        stream_audio: PlayStream,
        playback_timeout_seconds: float = PLAYBACK_TIMEOUT_SECONDS,
        cleanup_timeout_seconds: float = CLEANUP_TIMEOUT_SECONDS,
        faults: FaultReporter | None = None,
        journal: Journal | None = None,
    ) -> None:
        self._synthesizer = synthesizer
        self._set_muted = set_muted
        self._on_event = on_event
        self._tail_seconds = tail_seconds
        self._synth_timeout_seconds = synth_timeout_seconds
        self._stream_audio = stream_audio
        self._playback_timeout_seconds = playback_timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._faults = faults
        self._journal = journal
        self._queue: asyncio.Queue[tuple[AsyncIterator[bytes], str, asyncio.Future[None]]] = (
            asyncio.Queue()
        )
        self._worker: asyncio.Task[None] | None = None
        self._play_tasks: set[asyncio.Task[Any]] = set()
        self._muted = True
        self._speaking = False
        self._generation = 0
        self._closed = False
        self._active_done: asyncio.Future[None] | None = None
        self._active_play_task: asyncio.Task[Any] | None = None

    @property
    def is_speaking(self) -> bool:
        return self._speaking

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

        async def retryable_stream() -> AsyncIterator[bytes]:
            for attempt in range(2):
                emitted = False
                chunks: AsyncIterator[bytes] | None = None
                try:
                    extra: dict[str, Any] = {}
                    if sentence_index is not None:
                        extra["sentence_index"] = sentence_index
                    if transcript_id is not None:
                        extra["transcript_id"] = transcript_id
                    chunks = self._synthesizer.stream(text, **extra)
                    if not hasattr(chunks, "__aiter__"):
                        raise TypeError("stream() must return an async iterator")
                    async with asyncio.timeout(self._synth_timeout_seconds):
                        async for chunk in chunks:
                            if chunk:
                                emitted = True
                            yield chunk
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if (
                        attempt == 0
                        and not emitted
                        and _is_timeout(error)
                        and generation == self._generation
                        and not self._closed
                    ):
                        self._record_retry(streaming=True)
                        continue
                    fault = classify_exception("tts", error)
                    if fault is error:
                        raise
                    raise fault from error
                finally:
                    if chunks is not None:
                        close = getattr(chunks, "aclose", None)
                        if close is not None:
                            with suppress(Exception):
                                await close()

        if generation != self._generation or self._closed:
            self._emit("playback_dropped", text, "interrupted")
            return
        await self._play_queued(retryable_stream(), text)
        return

    async def _play_queued(self, audio: AsyncIterator[bytes], text: str) -> None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        await self._queue.put((audio, text, done))
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
                audio, text, done = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped.append((text, done))
            task = asyncio.create_task(self._close_audio(audio))
            closing.append(task)
            self._play_tasks.add(task)
        try:
            await self._cancelled_tasks_cleanup()
        finally:
            self._play_tasks.difference_update(closing)
            for text, done in dropped:
                if not done.cancelled() and not done.done():
                    done.set_result(None)
                self._emit("playback_dropped", text, "interrupted")
        await self._mute()

    async def _close_audio(self, audio: AsyncIterator[bytes]) -> None:
        close = getattr(audio, "aclose", None)
        if close is not None:
            with suppress(Exception):
                async with asyncio.timeout(self._cleanup_timeout_seconds):
                    await close()

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
        with suppress(Exception):
            await self._synthesizer.aclose()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def _emit(self, kind: PlaybackEventKind, text: str, detail: str = "") -> None:
        if self._on_event is None:
            return
        with suppress(Exception):
            self._on_event(PlaybackEvent(kind, text, detail))

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
            await self._set_muted(True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit("mute_failed", "", f"{type(error).__name__}: {error}")
            self._record_fault("playback", error)
            return
        self._muted = True
        self._speaking = False

    async def _unmute(self) -> None:
        if not self._muted:
            return
        try:
            await self._set_muted(False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit("mute_failed", "", f"{type(error).__name__}: {error}")
            self._record_fault("playback", error)
        self._muted = False
        self._speaking = True

    async def _run(self) -> None:
        while True:
            audio, text, done = await self._queue.get()
            generation = self._generation
            if done.cancelled():
                await self._close_audio(audio)
                continue
            try:
                await self._unmute()
            except BaseException:
                await self._close_audio(audio)
                if not done.cancelled() and not done.done():
                    done.set_result(None)
                raise
            if generation != self._generation or done.cancelled() or self._closed:
                await self._close_audio(audio)
                await self._mute()
                if not done.cancelled() and not done.done():
                    done.set_result(None)
                self._emit(
                    "playback_dropped",
                    text,
                    "cancelled" if done.cancelled() else "interrupted",
                )
                continue
            self._emit("playback_started", text)
            play_task = asyncio.create_task(self._consume_audio(audio))
            self._play_tasks.add(play_task)
            self._active_done = done
            self._active_play_task = play_task
            try:
                async with asyncio.timeout(self._playback_timeout_seconds):
                    await play_task
            except TimeoutError:
                play_task.cancel()
                with suppress(TimeoutError):
                    async with asyncio.timeout(self._cleanup_timeout_seconds):
                        await play_task
                self._emit("playback_failed", text, "playback timed out")
                self._record_fault("playback", TimeoutError("playback timed out"))
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is None or current.cancelling() > 0 or not play_task.cancelled():
                    raise
                self._emit("playback_dropped", text, "interrupted")
            except Exception as error:
                self._emit("playback_failed", text, f"{type(error).__name__}: {error}")
                self._record_fault("playback", error)
            else:
                self._emit("playback_finished", text)
            finally:
                self._play_tasks.discard(play_task)
                self._active_done = None
                self._active_play_task = None
                if not done.cancelled() and not done.done():
                    done.set_result(None)
            if self._queue.empty():
                generation = self._generation
                await asyncio.sleep(self._tail_seconds)
                if generation == self._generation and self._queue.empty():
                    await self._mute()
