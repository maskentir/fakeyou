from fakeyou.foundation.pcm import (
    PCM16_FULL_SCALE,
    PCM16_SAMPLE_BYTES,
    VOICED_PAD_FRAMES,
    decode_pcm16,
    encode_pcm16,
    pcm16_to_floats,
    voiced_clip,
)


def test_decode_reads_little_endian_signed_samples() -> None:
    assert decode_pcm16(b"\x01\x00\xff\xff\x00\x80") == (1, -1, -32768)


def test_decode_ignores_a_trailing_odd_byte() -> None:
    assert decode_pcm16(b"\x01\x00\x02") == (1,)


def test_decode_of_empty_bytes_is_empty() -> None:
    assert decode_pcm16(b"") == ()


def test_encode_writes_little_endian_signed_samples() -> None:
    assert encode_pcm16((1, -1, -32768)) == b"\x01\x00\xff\xff\x00\x80"


def test_encode_of_no_samples_is_empty() -> None:
    assert encode_pcm16(()) == b""


def test_decode_inverts_encode() -> None:
    samples = (0, 1234, -4321, 32767, -32768)

    assert decode_pcm16(encode_pcm16(samples)) == samples


def test_floats_are_normalised_to_full_scale() -> None:
    assert pcm16_to_floats(b"\x00\x40\x00\x80\x00\x00") == [0.5, -1.0, 0.0]


def test_floats_ignore_a_trailing_odd_byte_and_accept_no_bytes() -> None:
    assert pcm16_to_floats(b"\x00\x40\x01") == [0.5]
    assert pcm16_to_floats(b"") == []


def test_constants_describe_sixteen_bit_samples() -> None:
    assert PCM16_SAMPLE_BYTES == 2
    assert PCM16_FULL_SCALE == 32768.0


def _level(frames: int, level: int, rate: int = 16_000) -> bytes:
    return encode_pcm16([level] * (rate // 50 * frames))


def test_voiced_clip_trims_silence_and_keeps_a_short_pad() -> None:
    pad = _level(VOICED_PAD_FRAMES, 0)
    clip = voiced_clip(_level(10, 0) + _level(5, 3000) + _level(30, 0), 16_000)

    assert clip is not None
    assert clip.pcm == pad + _level(5, 3000) + pad
    assert clip.longest_pause_seconds == 0.0


def test_voiced_clip_measures_the_longest_pause_inside_the_speech() -> None:
    clip = voiced_clip(_level(3, 3000) + _level(15, 0) + _level(3, 3000), 16_000)

    assert clip is not None
    assert round(clip.longest_pause_seconds, 2) == 0.3


def test_voiced_clip_of_silence_or_too_little_audio_is_none() -> None:
    assert voiced_clip(_level(20, 0), 16_000) is None
    assert voiced_clip(b"\x00\x10" * 10, 16_000) is None
