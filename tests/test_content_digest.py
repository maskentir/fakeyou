from dataclasses import replace
from typing import Any

import pytest

from fakeyou.foundation.content_digest import (
    DECODED_PIXELS_SHA256,
    DigestRecord,
    SampleSource,
    encoded_bytes_record,
)

SCREEN = SampleSource("screen", "stream-screen", "screen")


def pixels(digest: str | None = "aa", **changes: Any) -> DigestRecord:
    record = DigestRecord(
        algorithm=DECODED_PIXELS_SHA256,
        version=1,
        width=1920,
        height=1080,
        pixel_format="I420",
        role="screen",
        stream_id="stream-screen",
        subject_id="screen",
        sequence=1,
        media_timestamp=1000.0,
        digest=digest,
    )
    return replace(record, **changes)


def test_equal_digests_of_comparable_records_are_the_same_content() -> None:
    assert pixels().same_content_as(pixels(sequence=9, media_timestamp=5000.0)) is True


def test_different_digests_of_comparable_records_are_different_content() -> None:
    assert pixels().same_content_as(pixels("bb")) is False


@pytest.mark.parametrize(
    "change",
    [
        {"width": 1280},
        {"height": 720},
        {"pixel_format": "NV12"},
        {"algorithm": "sha256-encoded-bytes"},
        {"version": 2},
        {"digest": None, "skip_reason": "hash_over_budget"},
        {"digest": ""},
    ],
)
def test_records_that_are_not_comparable_say_nothing_about_content(
    change: dict[str, Any],
) -> None:
    other = pixels(**change)

    assert pixels().same_content_as(other) is None
    assert other.same_content_as(pixels()) is None


def test_encoded_bytes_records_compare_only_with_their_own_kind() -> None:
    left = encoded_bytes_record("aa", SCREEN, sequence=1, size=(1280, 720))
    right = encoded_bytes_record("aa", SCREEN, sequence=2, size=(1280, 720))

    assert left.same_content_as(right) is True
    assert left.same_content_as(pixels(width=1280, height=720)) is None
