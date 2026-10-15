import struct

import pytest
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from fakeyou.audio.audio import TurnAudioTap


def test_vad_analyzer_maps_settings_to_params() -> None:
    from pipecat.audio.vad.vad_analyzer import VADParams

    from fakeyou.audio.audio import vad_analyzer
    from fakeyou.config.settings import DEFAULTS, VADSettings
    from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE

    default = vad_analyzer(CAPTURE_SAMPLE_RATE, DEFAULTS.vad)
    expected = VADParams()
    assert default.params.confidence == expected.confidence
    assert default.params.start_secs == expected.start_secs
    assert default.params.stop_secs == expected.stop_secs
    assert default.params.min_volume == expected.min_volume
    custom = vad_analyzer(
        CAPTURE_SAMPLE_RATE,
        VADSettings(confidence=0.5, start_secs=0.1, stop_secs=0.4, min_volume=0.3),
    )
    assert custom.params.confidence == 0.5
    assert custom.params.start_secs == 0.1
    assert custom.params.stop_secs == 0.4
    assert custom.params.min_volume == 0.3


async def test_turn_tap_collects_audio_between_speech_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()
    pushed: list[Frame] = []

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        pushed.append(frame)
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x01" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x02" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x03" * 160, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x01" * 320 + b"\x02" * 320 + b"\x03" * 160
    assert tap.take_clip() == b""
    assert [type(frame).__name__ for frame in pushed] == [
        "InputAudioRawFrame",
        "VADUserStartedSpeakingFrame",
        "InputAudioRawFrame",
        "InputAudioRawFrame",
        "VADUserStoppedSpeakingFrame",
    ]


async def test_turn_tap_stop_without_start_leaves_no_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b""


async def test_turn_tap_restarts_on_repeated_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x04" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x05" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x04" * 320 + b"\x05" * 320


async def test_turn_tap_accumulates_segments_across_vad_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0a" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0b" * 160, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x0a" * 320 + b"\x0b" * 160


async def test_turn_tap_trims_accumulation_to_recent_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    from fakeyou.audio.audio import TURN_TAP_MAX_SECONDS

    limit = int(16000 * 2 * TURN_TAP_MAX_SECONDS)
    first = b"\x0c" * limit
    second = b"\x0d" * 3200
    tap = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=first, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=second, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == (first + second)[-limit:]


async def test_turn_tap_accumulated_take_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0e" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0f" * 160, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x0e" * 320 + b"\x0f" * 160
    assert tap.take_clip() == b""


def _tap_stream_frame(index: int) -> bytes:
    base = index * 160
    samples = [((base + offset) * 257 + 12345) % 65536 - 32768 for offset in range(160)]
    return struct.pack("<160h", *samples)


async def _drive_tap(tap: TurnAudioTap, total: int, start_at: int, stop_at: int) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    for index in range(total):
        if index == start_at:
            await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        if index == stop_at:
            await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await tap.process_frame(
            InputAudioRawFrame(audio=_tap_stream_frame(index), sample_rate=16000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
    if start_at == total:
        await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    if stop_at == total:
        await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


async def test_turn_tap_clips_within_one_grid_cell_are_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = TurnAudioTap()
    second = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(first, "push_frame", sink)
    monkeypatch.setattr(second, "push_frame", sink)
    await _drive_tap(first, 300, 105, 231)
    await _drive_tap(second, 300, 109, 235)

    first_clip = first.take_clip()
    second_clip = second.take_clip()

    assert first_clip == second_clip
    assert first_clip == b"".join(_tap_stream_frame(index) for index in range(80, 260))


async def test_turn_tap_includes_preroll_before_speech_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await _drive_tap(tap, 100, 50, 100)

    clip = tap.take_clip()

    assert clip == b"".join(_tap_stream_frame(index) for index in range(30, 100))
    assert clip[:6400] == b"".join(_tap_stream_frame(index) for index in range(30, 50))


async def test_turn_tap_includes_trailing_after_speech_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(tap, "push_frame", sink)
    await _drive_tap(tap, 110, 10, 60)

    clip = tap.take_clip()

    assert clip == b"".join(_tap_stream_frame(index) for index in range(80))
    assert clip[600 * 32 : 800 * 32] == b"".join(
        _tap_stream_frame(index) for index in range(60, 80)
    )


async def test_turn_tap_keep_open_second_take_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    first = TurnAudioTap()
    second = TurnAudioTap()

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(first, "push_frame", sink)
    monkeypatch.setattr(second, "push_frame", sink)
    first_bounds = (20, 40, 105, 231)
    second_bounds = (20, 40, 109, 235)
    total = 300
    mid = 150

    for index in range(mid):
        for tap, bounds in ((first, first_bounds), (second, second_bounds)):
            start_first, stop_first, start_second, stop_second = bounds
            if index == start_first or index == start_second:
                await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            if index == stop_first or index == stop_second:
                await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            await tap.process_frame(
                InputAudioRawFrame(
                    audio=_tap_stream_frame(index), sample_rate=16000, num_channels=1
                ),
                FrameDirection.DOWNSTREAM,
            )

    assert first.take_clip() == second.take_clip()

    for index in range(mid, total):
        for tap, bounds in ((first, first_bounds), (second, second_bounds)):
            start_first, stop_first, start_second, stop_second = bounds
            if index == start_first or index == start_second:
                await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            if index == stop_first or index == stop_second:
                await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            await tap.process_frame(
                InputAudioRawFrame(
                    audio=_tap_stream_frame(index), sample_rate=16000, num_channels=1
                ),
                FrameDirection.DOWNSTREAM,
            )

    first_clip = first.take_clip()
    second_clip = second.take_clip()

    assert first_clip == second_clip
    assert first_clip == b"".join(_tap_stream_frame(index) for index in range(80, 260))
