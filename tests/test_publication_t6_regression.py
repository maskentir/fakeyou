import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from fakeyou.foundation.publication import SnapshotRef
from fakeyou.perception.frames import changed_blocks, compare_screens
from fakeyou.perception.inspector import VisualInspection
from tests._perception_fakes import keyframe, reading, slide_jpeg
from tests.test_publication_activation import Meeting, Turn, ask


def figure_slide(revenue: str) -> bytes:
    image = Image.new("RGB", (1280, 720), (18, 32, 72))
    draw = ImageDraw.Draw(image)
    draw.text((80, 70), "Quarterly Review", fill="white", font=ImageFont.load_default(size=64))
    draw.text(
        (100, 260),
        f"Revenue: ${revenue}M",
        fill=(255, 196, 64),
        font=ImageFont.load_default(size=140),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


LARGE_BEFORE = figure_slide("1.2")
LARGE_AFTER = figure_slide("1.3")
SMALL_BEFORE = slide_jpeg("Quarterly Review", ["Revenue: $1.2M"])
SMALL_AFTER = slide_jpeg("Quarterly Review", ["Revenue: $1.3M"])
CASES = [
    pytest.param(LARGE_BEFORE, LARGE_AFTER, "minor", id="large_figure_minor"),
    pytest.param(SMALL_BEFORE, SMALL_AFTER, "same", id="small_figure_below_threshold"),
]


def figure_meeting(before: bytes, after: bytes | None) -> Meeting:
    def one_digit_changes(meeting: Meeting) -> None:
        assert after is not None
        presenter = meeting.timeline.participant("device-ayu", "Ayu")
        meeting.timeline.show_screen(keyframe(after, seconds=1.5), presenter, data=after)

    meeting = Meeting(
        inspections=(VisualInspection(verdict="supported", answer="Revenue is $1.2M."),),
        before_write=None if after is None else one_digit_changes,
    )
    presenter = meeting.timeline.participant("device-ayu", "Ayu")
    shown = meeting.timeline.show_screen(keyframe(before, seconds=0.5), presenter, data=before)
    assert shown.change == "new"
    meeting.timeline.read_screen(
        shown.segment.ref, reading("Quarterly Review", text="Revenue $1.2M")
    )
    return meeting


async def revenue_turn(meeting: Meeting) -> Turn:
    return await ask(meeting, "What is revenue?", needs=("screen",), reply="Revenue is $1.2M.")


@pytest.mark.parametrize(("before", "after", "comparison"), CASES)
def test_one_digit_change_is_not_a_distinct_screen_to_the_comparison(
    before: bytes, after: bytes, comparison: str
) -> None:
    left, right = keyframe(before), keyframe(after)

    assert left.sha256 != right.sha256
    assert compare_screens(left, right) == comparison
    assert changed_blocks(left, right) <= 2


@pytest.mark.parametrize(("before", "after", "comparison"), CASES)
def test_one_digit_change_keeps_the_segment_but_the_scene_reports_the_newer_sample(
    before: bytes, after: bytes, comparison: str
) -> None:
    del comparison
    meeting = figure_meeting(before, after)
    analyzed = meeting.timeline.snapshot().current_screen
    assert analyzed is not None
    bound = SnapshotRef(
        "screen", analyzed.ref, analyzed.keyframe.sha256, 0, 0, 0, analyzed.keyframe.captured_at
    )

    presenter = meeting.timeline.participant("device-ayu", "Ayu")
    shown = meeting.timeline.show_screen(keyframe(after, seconds=1.5), presenter, data=after)

    current = meeting.timeline.snapshot().current_screen
    assert current is not None
    assert shown.change == "same"
    assert current.ref == analyzed.ref
    assert current.keyframe.sha256 == keyframe(before).sha256
    assert shown.arrival is not None
    assert shown.arrival.sha256 == keyframe(after).sha256
    scene = meeting.perception.current_scene(bound)
    assert isinstance(scene, SnapshotRef)
    assert scene.subject_id == bound.subject_id
    assert scene.stream_id == keyframe(after).sha256
    assert meeting.timeline.latest_keyframe(current).sha256 == keyframe(after).sha256


@pytest.mark.parametrize(("before", "after", "comparison"), CASES)
async def test_claim_about_a_figure_is_not_published_after_one_digit_changed(
    before: bytes, after: bytes, comparison: str
) -> None:
    del comparison
    turn = await revenue_turn(figure_meeting(before, after))

    assert [item.outcome for item in turn.meeting.settled] == ["prevented"]
    assert turn.meeting.prevented == "unknown:content_changed_unverified"
    assert turn.counted == []


@pytest.mark.parametrize(("before", "after", "comparison"), CASES)
async def test_look_after_the_change_analyzes_the_newer_frame_and_is_published(
    before: bytes, after: bytes, comparison: str
) -> None:
    del comparison
    meeting = figure_meeting(before, None)
    presenter = meeting.timeline.participant("device-ayu", "Ayu")
    meeting.timeline.show_screen(keyframe(after, seconds=0.8), presenter, data=after)

    turn = await revenue_turn(meeting)

    [binding] = turn.meeting.bindings
    [evidence] = binding.evidence
    assert evidence.snapshot is not None
    assert evidence.snapshot.stream_id == keyframe(after).sha256
    assert [item.outcome for item in turn.meeting.settled] == ["completed"]


@pytest.mark.parametrize(("before", "after", "comparison"), CASES)
async def test_control_claim_about_an_unchanged_figure_is_published(
    before: bytes, after: bytes, comparison: str
) -> None:
    del after, comparison
    turn = await revenue_turn(figure_meeting(before, None))

    [binding] = turn.meeting.bindings
    [evidence] = binding.evidence
    assert evidence.status == "produced"
    assert turn.meeting.writer.written
    assert [item.outcome for item in turn.meeting.settled] == ["completed"]
    assert len(turn.counted) == 1
