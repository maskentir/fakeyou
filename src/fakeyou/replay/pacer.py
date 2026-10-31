import asyncio

from fakeyou.audio.pulse_writer import DEFAULT_PULSE_BUFFER
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES

BYTES_PER_SECOND = PLAYBACK_SAMPLE_RATE * PCM16_SAMPLE_BYTES


class PacedWriter:
    def __init__(self, buffer_seconds: float = DEFAULT_PULSE_BUFFER.target_seconds) -> None:
        self._buffer_seconds = buffer_seconds
        self._heard_until = 0.0

    @property
    def buffer_seconds(self) -> float:
        return self._buffer_seconds

    async def write(self, pcm: bytes) -> None:
        now = asyncio.get_running_loop().time()
        self._heard_until = max(now, self._heard_until) + len(pcm) / BYTES_PER_SECOND
        await self._sleep_until(self._heard_until - self._buffer_seconds)

    async def drain(self) -> None:
        await self._sleep_until(self._heard_until)

    async def close(self) -> None:
        return None

    async def _sleep_until(self, moment: float) -> None:
        delay = moment - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)
