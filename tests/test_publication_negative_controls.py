import io
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from fakeyou.foundation.abstention import abstention_entry
from fakeyou.foundation.publication import PublicationDecision, SentenceBinding
from fakeyou.perception.frames import changed_blocks, compare_screens
from fakeyou.perception.inspector import VisualInspection
from tests._perception_fakes import keyframe
from tests.test_publication_activation import Meeting, ask, spoken_texts
from tests.test_publication_freshness import decide, evidence, validated
from tests.test_publication_t6_regression import (
    SMALL_AFTER,
    SMALL_BEFORE,
    figure_meeting,
    revenue_turn,
)


@pytest.mark.parametrize("change", ["table_cell", "highlight"])
async def test_small_content_or_overlay_change_never_confirms_the_checked_figure(
    change: str,
) -> None:
    image = Image.open(io.BytesIO(SMALL_BEFORE)).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((300, 232, 306, 238), fill="yellow")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    after = SMALL_AFTER if change == "table_cell" else output.getvalue()
    before_frame, after_frame = keyframe(SMALL_BEFORE), keyframe(after)
    assert before_frame.sha256 != after_frame.sha256
    assert changed_blocks(before_frame, after_frame) <= 2
    assert compare_screens(before_frame, after_frame) in {"same", "minor"}

    turn = await revenue_turn(figure_meeting(SMALL_BEFORE, after))

    assert turn.meeting.prevented == "unknown:content_changed_unverified"
    assert all("currently displayed" not in text for text in spoken_texts(turn.meeting))
    assert turn.meeting.settled[0].local_audio_written is False


def test_required_snapshot_missing_never_becomes_valid() -> None:
    decision = decide(replace(evidence(), snapshot=None), validated())

    assert decision.validity == "unknown"
    assert decision.reason == "snapshot_missing"


async def test_second_independent_statement_can_publish_after_first_is_prevented() -> None:
    meeting = Meeting()
    context = meeting.publication_context
    assert context is not None
    assert context.commit is not None
    commit = context.commit
    first_publication: str | None = None

    def first_only(binding: SentenceBinding) -> PublicationDecision | None:
        nonlocal first_publication
        if binding.scope == "current" and first_publication is None:
            first_publication = binding.publication_id
            return PublicationDecision(
                "invalid", "capture_too_old", meeting.clock(), context.clock_domain
            )
        return commit(binding)

    meeting.publication_context = replace(context, commit=first_only)
    meeting.engine.configure_publication(meeting.publication_context)
    claims = (
        {"kind": "screen", "shape": "item_presence", "item": "Beta"},
        {"kind": "screen", "shape": "item_presence", "item": "17 March 2027"},
    )

    await ask(meeting, "When is beta?", needs=("screen",), claims=claims)

    assert [receipt.outcome for receipt in meeting.settled] == [
        "prevented",
        "completed",
        "completed",
    ]
    assert spoken_texts(meeting) == [
        "The checked frame showed Beta.",
        "The currently displayed screen shows 17 March 2027.",
    ]


async def test_zero_byte_clip_is_not_counted_or_recorded_as_speech() -> None:
    meeting = Meeting()
    meeting.synth.audio = b""

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    assert meeting.writer.written == []
    assert turn.counted == []
    assert all(not receipt.local_audio_written for receipt in meeting.settled)


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("entry_id", ["AB1", "AB2", "AB3"])
async def test_each_catalog_entry_has_exactly_one_audio_receipt_in_each_language(
    language: str,
    entry_id: str,
) -> None:
    meeting = (
        Meeting(inspections=(VisualInspection(verdict="unclear"),))
        if entry_id == "AB2"
        else Meeting()
    )
    if entry_id == "AB3":
        meeting.perception.interpreter = None

    turn = await ask(
        meeting,
        "When is beta?",
        needs=("screen",),
        language=language,
        claims=(),
    )

    assert spoken_texts(meeting) == [abstention_entry(entry_id, language).text]
    assert len(turn.counted) == 1
    assert len(meeting.settled) == 1
    assert meeting.settled[0].local_audio_written is True
    assert meeting.synth.texts == [abstention_entry(entry_id, language).text]


async def test_screen_and_camera_have_independent_publications() -> None:
    from tests.test_publication_activation import BETA_CLAIM, BETA_STATEMENT, GREY_SHIRT

    meeting = Meeting(
        inspections=(
            VisualInspection(verdict="supported", answer="Budi wears a grey shirt."),
            VisualInspection(verdict="supported", answer="Beta launch is 17 March 2027."),
        )
    )

    turn = await ask(
        meeting,
        "What is beta and what is Budi wearing?",
        needs=("screen", "participant"),
        claims=(BETA_CLAIM, GREY_SHIRT),
    )

    assert [binding.scope for binding in meeting.bindings] == ["current", "historical"]
    assert [binding.required_kinds for binding in meeting.bindings] == [
        ("screen",),
        ("participant",),
    ]
    assert spoken_texts(meeting)[0] == BETA_STATEMENT
    assert len({binding.publication_id for binding in meeting.bindings}) == 2
    assert len(turn.counted) == 2
