import io

import pytest
from PIL import Image, ImageDraw

from fakeyou.perception.frames import changed_blocks, compare_screens
from tests._perception_fakes import keyframe, slide_jpeg

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027"])


def _with_cursor(data: bytes) -> bytes:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    ImageDraw.Draw(image).polygon([(600, 400), (600, 430), (620, 420)], fill="white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("size", "quality"), [((1280, 720), 90), ((878, 494), 60), ((1920, 1080), 80)]
)
def test_the_same_slide_at_another_resolution_is_not_a_distinct_screen(
    size: tuple[int, int], quality: int
) -> None:
    sharp = keyframe(slide_jpeg(*TIMELINE))
    reencoded = keyframe(slide_jpeg(*TIMELINE, size=size, quality=quality))

    assert compare_screens(sharp, reencoded) != "distinct"


def test_a_moving_cursor_is_a_minor_change() -> None:
    data = slide_jpeg(*TIMELINE)

    assert compare_screens(keyframe(data), keyframe(_with_cursor(data))) == "minor"


@pytest.mark.parametrize(
    "other",
    [
        ("Launch Timeline", ["Starter: 99,000 per month", "Pro: 299,000 per month"]),
        ("Pricing Plans", ["Alpha: 12 January 2027", "Beta: 17 March 2027"]),
        ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027", "GA: 9 June 2027"]),
    ],
)
def test_slides_sharing_a_template_but_not_their_text_are_distinct(
    other: tuple[str, list[str]],
) -> None:
    assert compare_screens(keyframe(slide_jpeg(*TIMELINE)), keyframe(slide_jpeg(*other))) == (
        "distinct"
    )


def test_identical_bytes_are_the_same_screen() -> None:
    data = slide_jpeg(*TIMELINE)

    assert changed_blocks(keyframe(data), keyframe(data, seconds=5)) == 0
    assert compare_screens(keyframe(data), keyframe(data, seconds=5)) == "same"


def test_keyframes_record_size_and_hash_but_never_serialize_the_signature() -> None:
    frame = keyframe(slide_jpeg(*TIMELINE, size=(1920, 1080)))

    assert (frame.width, frame.height) == (1920, 1080)
    assert len(frame.sha256) == 64
    assert "signature" not in frame.model_dump()
