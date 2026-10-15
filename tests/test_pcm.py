from fakeyou.foundation.pcm import (
    PCM16_FULL_SCALE,
    PCM16_SAMPLE_BYTES,
    decode_pcm16,
    encode_pcm16,
    pcm16_to_floats,
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
