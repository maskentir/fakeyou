from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from fakeyou.audio.audio import PreRollBuffer


class _Sink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__(enable_direct_mode=True)
        self.frames: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        self.frames.append(frame)


def _audio(byte: int, count: int = 320) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=bytes([byte]) * count, sample_rate=16000, num_channels=1)


async def test_preroll_replays_ring_before_speech_start() -> None:
    buffer = PreRollBuffer(sample_rate=16000, preroll_ms=40.0)
    sink = _Sink()
    buffer.link(sink)
    for index in range(4):
        await buffer.process_frame(_audio(index), FrameDirection.DOWNSTREAM)
    await buffer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await buffer.process_frame(_audio(9), FrameDirection.DOWNSTREAM)
    await buffer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    kinds = [type(frame).__name__ for frame in sink.frames]
    start_at = kinds.index("VADUserStartedSpeakingFrame")
    replay = sink.frames[start_at - 1]
    assert isinstance(replay, AudioRawFrame)
    assert replay.audio == b"".join(bytes([index]) * 320 for index in range(4))
    assert kinds[: start_at - 1] == ["InputAudioRawFrame"] * 4
    assert any(
        isinstance(frame, AudioRawFrame) and frame.audio == bytes([9]) * 320
        for frame in sink.frames
    )


async def test_preroll_ring_caps_at_preroll_ms() -> None:
    buffer = PreRollBuffer(sample_rate=16000, preroll_ms=40.0)
    sink = _Sink()
    buffer.link(sink)
    for index in range(10):
        await buffer.process_frame(_audio(index), FrameDirection.DOWNSTREAM)
    assert buffer.buffered_ms <= 40.0
