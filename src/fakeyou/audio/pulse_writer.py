import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass

from pasimple import PA_SAMPLE_S16LE, PA_STREAM_PLAYBACK, PaSimple, PaSimpleError

from fakeyou.audio.playback import PlaybackError
from fakeyou.foundation.invariants import PCM_CHANNELS
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES


@dataclass(frozen=True, slots=True)
class PulseBuffer:
    target_seconds: float = 0.04
    prebuffer_seconds: float = 0.02


DEFAULT_PULSE_BUFFER = PulseBuffer()


class PulseWriter:
    def __init__(self, *, sink: str, rate: int, buffer: PulseBuffer = DEFAULT_PULSE_BUFFER) -> None:
        self._sink = sink
        self._rate = rate
        self._buffer = buffer
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pulse-writer")
        self._stream: PaSimple | None = None
        self._closed = False
        self._prime_pcm = b"\x00" * self._bytes(buffer.prebuffer_seconds)
        self._executor.submit(self._prime)

    def _prime(self) -> None:
        try:
            self._open().write(self._prime_pcm)
        except PaSimpleError:
            self._release_quietly()

    @property
    def buffer_seconds(self) -> float:
        return self._buffer.target_seconds

    async def write(self, pcm: bytes) -> None:
        await self._call(self._write, pcm)

    async def drain(self) -> None:
        await self._call(self._drain)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._call(self._release, closing=True)
        finally:
            self._executor.shutdown(wait=False)

    def _bytes(self, seconds: float) -> int:
        frame = PCM16_SAMPLE_BYTES * PCM_CHANNELS
        return round(self._rate * seconds) * frame

    def _open(self) -> PaSimple:
        if self._stream is None:
            self._stream = PaSimple(
                PA_STREAM_PLAYBACK,
                PA_SAMPLE_S16LE,
                PCM_CHANNELS,
                self._rate,
                app_name="fakeyou",
                stream_name="twin-voice",
                device_name=self._sink,
                tlength=self._bytes(self._buffer.target_seconds),
                prebuf=self._bytes(self._buffer.prebuffer_seconds),
            )
        return self._stream

    def _write(self, pcm: bytes) -> None:
        if pcm:
            self._open().write(pcm)

    def _drain(self) -> None:
        if self._stream is not None:
            self._stream.drain()

    def _release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.flush()
        finally:
            stream.close()

    async def _call(
        self, operation: Callable[..., None], *args: bytes, closing: bool = False
    ) -> None:
        if self._closed and not closing:
            raise PlaybackError("the voice writer is closed")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self._guarded, operation, *args)
        except PaSimpleError as error:
            raise PlaybackError(f"PulseAudio playback failed: {error}") from error

    def _guarded(self, operation: Callable[..., None], *args: bytes) -> None:
        try:
            operation(*args)
        except PaSimpleError:
            self._release_quietly()
            raise

    def _release_quietly(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with suppress(PaSimpleError):
                stream.close()
