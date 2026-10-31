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
    ends: float | None = None
    async for chunk in chunks:
        now = loop.time()
        ends = (now if ends is None else max(ends, now)) + len(chunk) / BYTES_PER_SECOND
    if ends is not None and ends > loop.time():
        await asyncio.sleep(ends - loop.time())


def create_pacer_backend() -> PlaybackBackend:
    return PlaybackBackend(set_muted=set_muted, stream_audio=pacer_stream)
