import asyncio

from fakeyou.audio.stream_input import StreamAudioInput
from fakeyou.foundation.transcript import TranscriptUpdate
from tests._capture_support import FakePlayback, echo_stt


async def test_stream_input_tags_transcripts_with_stream_id() -> None:
    from fakeyou.audio.audio import (
        StreamCallbacks,
        StreamComponents,
        StreamSource,
        open_stream_audio,
    )

    left: list[tuple[str, str]] = []
    right: list[tuple[str, str]] = []
    left_ready = asyncio.Event()
    right_ready = asyncio.Event()

    async def on_left(frame: TranscriptUpdate) -> None:
        left.append((frame.stream_id or "", frame.text))
        left_ready.set()

    async def on_right(frame: TranscriptUpdate) -> None:
        right.append((frame.stream_id or "", frame.text))
        right_ready.set()

    assert callable(open_stream_audio)
    assert callable(StreamAudioInput)

    left_input = StreamAudioInput()
    right_input = StreamAudioInput()
    async with (
        open_stream_audio(
            source=StreamSource("slot-0", left_input),
            callbacks=StreamCallbacks(on_left),
            components=StreamComponents(stt=echo_stt("left speech"), playback=FakePlayback()),
        ),
        open_stream_audio(
            source=StreamSource("slot-1", right_input),
            callbacks=StreamCallbacks(on_right),
            components=StreamComponents(stt=echo_stt("right speech"), playback=FakePlayback()),
        ),
    ):
        await left_input.feed(b"\x01\x02" * 160)
        await right_input.feed(b"\x03\x04" * 160)
        await asyncio.wait_for(left_ready.wait(), timeout=5)
        await asyncio.wait_for(right_ready.wait(), timeout=5)

    assert left == [("slot-0", "left speech")]
    assert right == [("slot-1", "right speech")]


async def test_stream_input_buffers_audio_before_start() -> None:
    from fakeyou.audio.audio import (
        StreamCallbacks,
        StreamComponents,
        StreamSource,
        open_stream_audio,
    )
    from fakeyou.foundation.transcript import TranscriptUpdate

    seen: list[tuple[str, str]] = []
    ready = asyncio.Event()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        seen.append((frame.stream_id or "", frame.text))
        ready.set()

    streamer = StreamAudioInput()
    await streamer.feed(b"\x05\x06" * 160)
    async with open_stream_audio(
        source=StreamSource("slot-9", streamer),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=echo_stt("early speech"), playback=FakePlayback()),
    ):
        await asyncio.wait_for(ready.wait(), timeout=5)
    assert seen == [("slot-9", "early speech")]
