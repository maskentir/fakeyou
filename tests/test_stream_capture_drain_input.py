import asyncio

from pipecat.frames.frames import TranscriptionFrame
from pipecat.transports.base_transport import TransportParams

from fakeyou.audio.stream_capture import (
    StreamAudioInput,
)
from tests._capture_support import EchoSTT, FakePlayback


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

    async def on_left(frame: TranscriptionFrame) -> None:
        left.append((frame.transport_source or "", frame.text))
        left_ready.set()

    async def on_right(frame: TranscriptionFrame) -> None:
        right.append((frame.transport_source or "", frame.text))
        right_ready.set()

    assert callable(open_stream_audio)
    assert callable(StreamAudioInput)

    left_input = StreamAudioInput(TransportParams(audio_in_enabled=True))
    right_input = StreamAudioInput(TransportParams(audio_in_enabled=True))
    async with (
        open_stream_audio(
            source=StreamSource("slot-0", left_input),
            callbacks=StreamCallbacks(on_left),
            components=StreamComponents(stt=EchoSTT("left speech"), playback=FakePlayback()),
        ),
        open_stream_audio(
            source=StreamSource("slot-1", right_input),
            callbacks=StreamCallbacks(on_right),
            components=StreamComponents(stt=EchoSTT("right speech"), playback=FakePlayback()),
        ),
    ):
        await left_input.feed(b"\x01\x02" * 160)
        await right_input.feed(b"\x03\x04" * 160)
        await asyncio.wait_for(left_ready.wait(), timeout=5)
        await asyncio.wait_for(right_ready.wait(), timeout=5)

    assert left == [("slot-0", "left speech")]
    assert right == [("slot-1", "right speech")]


async def test_stream_input_buffers_audio_before_start() -> None:
    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.audio import (
        StreamCallbacks,
        StreamComponents,
        StreamSource,
        open_stream_audio,
    )

    seen: list[tuple[str, str]] = []
    ready = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        seen.append((frame.transport_source or "", frame.text))
        ready.set()

    streamer = StreamAudioInput(TransportParams(audio_in_enabled=True))
    await streamer.feed(b"\x05\x06" * 160)
    async with open_stream_audio(
        source=StreamSource("slot-9", streamer),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=EchoSTT("early speech"), playback=FakePlayback()),
    ):
        await asyncio.wait_for(ready.wait(), timeout=5)
    assert seen == [("slot-9", "early speech")]
