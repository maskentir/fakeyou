import struct

from fakeyou.audio.audio import vad_params
from fakeyou.audio.graph import TURN_TAP_MAX_SECONDS, TurnAudioTap
from fakeyou.audio.vad import VadParams
from fakeyou.config.settings import DEFAULTS, VADSettings


def _chunk(byte: int, count: int) -> bytes:
    return bytes([byte]) * count


def _tap_stream_frame(index: int) -> bytes:
    base = index * 160
    samples = [((base + offset) * 257 + 12345) % 65536 - 32768 for offset in range(160)]
    return struct.pack("<160h", *samples)


def _drive_tap(tap: TurnAudioTap, total: int, start_at: int, stop_at: int) -> None:
    for index in range(total):
        if index == start_at:
            tap.speech_started()
        if index == stop_at:
            tap.speech_stopped()
        tap.feed(_tap_stream_frame(index))
    if start_at == total:
        tap.speech_started()
    if stop_at == total:
        tap.speech_stopped()


def test_vad_params_map_settings_and_defaults_match_the_analyzer() -> None:
    assert vad_params(DEFAULTS.vad) == VadParams()
    custom = vad_params(VADSettings(confidence=0.5, start_secs=0.1, stop_secs=0.4, min_volume=0.3))
    assert custom == VadParams(confidence=0.5, start_secs=0.1, stop_secs=0.4, min_volume=0.3)


def test_turn_tap_collects_audio_between_speech_boundaries() -> None:
    tap = TurnAudioTap()

    tap.feed(_chunk(0x01, 320))
    tap.speech_started()
    tap.feed(_chunk(0x02, 320))
    tap.feed(_chunk(0x03, 160))
    tap.speech_stopped()

    assert tap.take_clip() == _chunk(0x01, 320) + _chunk(0x02, 320) + _chunk(0x03, 160)
    assert tap.take_clip() == b""


def test_turn_tap_stop_without_start_leaves_no_clip() -> None:
    tap = TurnAudioTap()

    tap.speech_stopped()

    assert tap.take_clip() == b""


def test_turn_tap_restarts_on_repeated_start() -> None:
    tap = TurnAudioTap()

    tap.speech_started()
    tap.feed(_chunk(0x04, 320))
    tap.speech_started()
    tap.feed(_chunk(0x05, 320))
    tap.speech_stopped()

    assert tap.take_clip() == _chunk(0x04, 320) + _chunk(0x05, 320)


def test_turn_tap_accumulates_segments_across_vad_cycles() -> None:
    tap = TurnAudioTap()

    tap.speech_started()
    tap.feed(_chunk(0x0A, 320))
    tap.speech_stopped()
    tap.speech_started()
    tap.feed(_chunk(0x0B, 160))
    tap.speech_stopped()

    assert tap.take_clip() == _chunk(0x0A, 320) + _chunk(0x0B, 160)


def test_turn_tap_trims_accumulation_to_recent_tail() -> None:
    limit = int(16000 * 2 * TURN_TAP_MAX_SECONDS)
    first = _chunk(0x0C, limit)
    second = _chunk(0x0D, 3200)
    tap = TurnAudioTap()

    tap.speech_started()
    tap.feed(first)
    tap.speech_stopped()
    tap.speech_started()
    tap.feed(second)
    tap.speech_stopped()

    assert tap.take_clip() == (first + second)[-limit:]


def test_turn_tap_accumulated_take_clears() -> None:
    tap = TurnAudioTap()

    tap.speech_started()
    tap.feed(_chunk(0x0E, 320))
    tap.speech_stopped()
    tap.speech_started()
    tap.feed(_chunk(0x0F, 160))
    tap.speech_stopped()

    assert tap.take_clip() == _chunk(0x0E, 320) + _chunk(0x0F, 160)
    assert tap.take_clip() == b""


def test_turn_tap_clips_within_one_grid_cell_are_byte_identical() -> None:
    first = TurnAudioTap()
    second = TurnAudioTap()
    _drive_tap(first, 300, 105, 231)
    _drive_tap(second, 300, 109, 235)

    first_clip = first.take_clip()
    second_clip = second.take_clip()

    assert first_clip == second_clip
    assert first_clip == b"".join(_tap_stream_frame(index) for index in range(80, 260))


def test_turn_tap_includes_preroll_before_speech_start() -> None:
    tap = TurnAudioTap()
    _drive_tap(tap, 100, 50, 100)

    clip = tap.take_clip()

    assert clip == b"".join(_tap_stream_frame(index) for index in range(30, 100))
    assert clip[:6400] == b"".join(_tap_stream_frame(index) for index in range(30, 50))


def test_turn_tap_includes_trailing_after_speech_stop() -> None:
    tap = TurnAudioTap()
    _drive_tap(tap, 110, 10, 60)

    clip = tap.take_clip()

    assert clip == b"".join(_tap_stream_frame(index) for index in range(80))
    assert clip[600 * 32 : 800 * 32] == b"".join(
        _tap_stream_frame(index) for index in range(60, 80)
    )


def test_turn_tap_keep_open_second_take_is_byte_identical() -> None:
    first = TurnAudioTap()
    second = TurnAudioTap()
    first_bounds = (20, 40, 105, 231)
    second_bounds = (20, 40, 109, 235)
    total = 300
    mid = 150

    for index in range(total):
        if index == mid:
            assert first.take_clip() == second.take_clip()
        for tap, bounds in ((first, first_bounds), (second, second_bounds)):
            start_first, stop_first, start_second, stop_second = bounds
            if index in (start_first, start_second):
                tap.speech_started()
            if index in (stop_first, stop_second):
                tap.speech_stopped()
            tap.feed(_tap_stream_frame(index))

    first_clip = first.take_clip()
    second_clip = second.take_clip()

    assert first_clip == second_clip
    assert first_clip == b"".join(_tap_stream_frame(index) for index in range(80, 260))
