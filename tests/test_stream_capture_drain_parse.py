import base64

import pytest

from tests._capture_support import recording_registry


def test_parse_drain_decodes_pcm_windows() -> None:
    from fakeyou.audio.stream_capture import parse_drain

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [],
        "dropped": 0,
    }
    parsed = parse_drain(payload)
    (chunk,) = parsed.chunks
    assert parsed.skipped == 0
    assert chunk.slot == 0
    assert chunk.csrcs == (111,)
    assert len(chunk.pcm) == 4


@pytest.mark.parametrize(
    "payload",
    [
        {"roster": [], "dropped": 0},
        {"streams": {}, "roster": [], "dropped": 0},
    ],
)
def test_parse_drain_rejects_corrupt_windows(payload: dict[str, object]) -> None:
    from typing import Any, cast

    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import parse_drain

    with pytest.raises(MeetingAudioError):
        parse_drain(cast(dict[str, Any], payload))


def test_parse_drain_skips_corrupt_entries_and_keeps_good_chunks() -> None:
    from fakeyou.audio.stream_capture import parse_drain

    good = {
        "slot": 0,
        "seq": 0,
        "csrcs": [111],
        "level": 0.4,
        "page_ms": 1000.0,
        "pcm_b64": "AAP//w==",
    }
    payload = {
        "streams": [
            "nope",
            {**good, "slot": "0"},
            {**good, "pcm_b64": "!!!"},
            dict(good),
            {**good, "slot": 1, "seq": 3},
        ],
        "roster": [],
        "dropped": 0,
    }

    parsed = parse_drain(payload)

    assert [(chunk.slot, chunk.seq) for chunk in parsed.chunks] == [(0, 0), (1, 3)]
    assert parsed.skipped == 3
    assert parsed.error_type == "MeetingAudioError"


def test_parse_drain_skips_bad_seq_entries() -> None:
    from typing import Any, cast

    from fakeyou.audio.stream_capture import parse_drain

    good = {
        "slot": 0,
        "seq": 0,
        "csrcs": [],
        "level": 0.1,
        "page_ms": 10.0,
        "pcm_b64": base64.b64encode(b"\x00\x00").decode(),
    }
    assert parse_drain({"streams": [good]}).chunks[0].seq == 0
    for bad in ("1", 1.5, True, -1, None):
        parsed = parse_drain(cast(dict[str, Any], {"streams": [{**good, "seq": bad}]}))
        assert parsed.chunks == []
        assert parsed.skipped == 1
    parsed = parse_drain(
        cast(dict[str, Any], {"streams": [{k: v for k, v in good.items() if k != "seq"}]})
    )
    assert parsed.chunks == []
    assert parsed.skipped == 1


async def test_drain_throughput_within_budget() -> None:
    import base64
    import time

    from fakeyou.audio.stream_capture import parse_drain

    pcm_b64 = base64.b64encode(bytes(8000)).decode("ascii")
    payload = {
        "streams": [
            {
                "slot": slot,
                "seq": 0,
                "csrcs": [100 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": pcm_b64,
            }
            for slot in range(4)
        ],
        "roster": [],
        "dropped": 0,
    }
    registry, _ = recording_registry(time.monotonic)
    started = time.monotonic()
    for _ in range(240):
        for chunk in parse_drain(payload).chunks:
            await registry.feed(chunk)
    assert time.monotonic() - started < 5.0
