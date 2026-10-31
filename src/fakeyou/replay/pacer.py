import asyncio
from collections.abc import AsyncIterator

from fakeyou.audio.playback import PlaybackBackend
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES

BYTES_PER_SECOND = PLAYBACK_SAMPLE_RATE * PCM16_SAMPLE_BYTES


async def set_muted(*, muted: bool) -> None:
    del muted


async def pacer_stream(chunks: AsyncIterator[bytes]) -> None:
    loop = asyncio.get_running_loop()
    started: float | None = None
    played = 0
    async for chunk in chunks:
        if started is None:
            started = loop.time()
        played += len(chunk)
        delay = started + played / BYTES_PER_SECOND - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


def create_pacer_backend() -> PlaybackBackend:
    return PlaybackBackend(set_muted=set_muted, stream_audio=pacer_stream)
