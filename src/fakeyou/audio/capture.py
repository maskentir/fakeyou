import asyncio
import contextlib
from collections.abc import Callable
from subprocess import DEVNULL, PIPE
from typing import Protocol

from fakeyou.audio.graph import PcmQueue
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE, PCM_CHANNELS
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES

CAPTURE_CHUNK_SECONDS = 0.08
CAPTURE_CHUNK_BYTES = int(CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES * CAPTURE_CHUNK_SECONDS)


class CaptureError(RuntimeError):
    pass


class CaptureSource(Protocol):
    @property
    def queue(self) -> PcmQueue: ...

    async def start(self) -> None: ...

    def terminate(self) -> None: ...


class ParecCapture:
    def __init__(
        self,
        *,
        on_failure: Callable[[CaptureError], None] | None = None,
        rate: int = CAPTURE_SAMPLE_RATE,
    ) -> None:
        self._queue = PcmQueue()
        self._on_failure = on_failure
        self._rate = rate
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def queue(self) -> PcmQueue:
        return self._queue

    async def start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "parec",
                "--format=s16le",
                f"--rate={self._rate}",
                f"--channels={PCM_CHANNELS}",
                "--raw",
                stdin=DEVNULL,
                stdout=PIPE,
                stderr=PIPE,
            )
        except OSError as error:
            raise CaptureError(f"parec is unavailable: {error}") from error
        self._pump = asyncio.create_task(self._read())

    def terminate(self) -> None:
        self._stopping = True
        if self._pump is not None:
            self._pump.cancel()
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()

    async def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise CaptureError("parec stdout is unavailable")
        try:
            while True:
                chunk = await proc.stdout.read(CAPTURE_CHUNK_BYTES)
                if not chunk:
                    break
                self._queue.put(chunk)
        except OSError as error:
            self._fail(CaptureError(f"parec read failed: {error}"))
            return
        if self._stopping:
            return
        returncode = await proc.wait()
        detail = b""
        if proc.stderr is not None:
            detail = await proc.stderr.read()
        message = detail.decode(errors="replace").strip()
        self._fail(CaptureError(f"parec exited with {returncode}: {message}"))

    def _fail(self, error: CaptureError) -> None:
        if self._on_failure is not None and not self._stopping:
            self._on_failure(error)
