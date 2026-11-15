import ast
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import WithheldReplies, WithheldReply
from fakeyou.foundation.abstention import abstention_entry, catalog_languages
from fakeyou.foundation.publication import sentence_digest
from fakeyou.foundation.statement import pattern_text, rerender, statement_digest
from fakeyou.perception.inspector import VisualInspection
from tests.test_publication_activation import (
    BETA_CLAIM,
    BETA_STATEMENT,
    NOT_ENOUGH,
    Meeting,
    Turn,
    ask,
)

REVENUE_ANSWER = "Project A revenue is $1.2M."
REVENUE_CLAIM = {
    "kind": "screen",
    "shape": "entity_attribute_value",
    "entity": "Project A",
    "attribute": "revenue",
    "value": "$1.2M",
}
REVENUE_STATEMENT = "The currently displayed screen lists revenue for Project A as $1.2M."
CATALOG_TEXTS = {
    abstention_entry(name, language).text
    for name in ("AB1", "AB2", "AB3")
    for language in catalog_languages()
}


def revenue_meeting(answer: str = REVENUE_ANSWER) -> Meeting:
    return Meeting(inspections=(VisualInspection(verdict="supported", answer=answer),))


async def revenue_turn(reply: str, *claims: dict[str, str], answer: str = REVENUE_ANSWER) -> Turn:
    return await ask(
        revenue_meeting(answer), "What is revenue?", needs=("screen",), reply=reply, claims=claims
    )


def assert_only_runtime_text_was_synthesized(meeting: Meeting) -> None:
    rendered = {
        binding.rendering.text_digest
        for binding in meeting.bindings
        if binding.rendering is not None
    }
    for text in meeting.synth.texts:
        assert text in CATALOG_TEXTS or statement_digest(text) in rendered, text


async def test_supported_claim_is_spoken_as_the_rendered_pattern_not_the_model_sentence() -> None:
    turn = await ask(Meeting(), "When is beta?", needs=("screen",))

    meeting = turn.meeting
    [binding] = meeting.bindings
    assert meeting.synth.texts == [BETA_STATEMENT]
    assert [item.text for item in meeting.settled] == [BETA_STATEMENT]
    assert binding.scope == "current"
    assert binding.rendering is not None
    assert binding.rendering.pattern_id == "current.entity_attribute_value"
    assert rerender(binding.rendering, "Beta launch is 17 March 2027.") == BETA_STATEMENT
    assert [item.kind for item in binding.evidence] == ["screen"]
    assert len(turn.counted) == 1


@pytest.mark.parametrize(
    ("reply", "smuggled"),
    [
        ("Revenue is $1.2M and growing fast.", "growing fast"),
        ("Revenue is definitely $1.2M.", "definitely"),
        ("Revenue is $1.2M right now.", "right now"),
        ("Revenue is $1.2M, and costs are not approved.", "not approved"),
        ("Revenue is $1.2M. Everything has been approved.", "approved"),
        ("In the frame I checked, revenue is $1.2M right now.", "In the frame I checked"),
        ("The checked frame listed revenue as $9M.", "$9M"),
    ],
)
async def test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer(
    reply: str, smuggled: str
) -> None:
    turn = await revenue_turn(reply, REVENUE_CLAIM)

    meeting = turn.meeting
    assert meeting.synth.texts == [REVENUE_STATEMENT]
    assert all(smuggled not in text for text in meeting.synth.texts)
    assert all(smuggled not in item.text for item in meeting.receipts)
    assert [item.outcome for item in meeting.settled] == ["completed"]
    assert_only_runtime_text_was_synthesized(meeting)


@pytest.mark.parametrize(
    "reply",
    [
        "Everything has been approved.",
        "Revenue is $1.2M.",
        "Sure, let me check.",
        "There is nothing on the screen.",
    ],
)
async def test_visual_turn_without_declared_claims_speaks_one_abstention_only(reply: str) -> None:
    turn = await revenue_turn(reply)

    meeting = turn.meeting
    assert meeting.synth.texts == [NOT_ENOUGH]
    [binding] = meeting.bindings
    assert (binding.scope, binding.rendering) == ("no_visual", None)
    assert binding.abstention is not None
    assert len(turn.counted) == 1


@pytest.mark.parametrize(
    ("claim", "answer"),
    [
        ({**REVENUE_CLAIM, "value": "$1.3M"}, REVENUE_ANSWER),
        ({**REVENUE_CLAIM, "attribute": "costs"}, REVENUE_ANSWER),
        (REVENUE_CLAIM, "Project A revenue is not $1.2M."),
        (REVENUE_CLAIM, "Project A revenue $1.2M and costs $0.8M."),
        ({**REVENUE_CLAIM, "polarity": "negative"}, "Project A revenue is not $1.2M."),
        ({**REVENUE_CLAIM, "kind": "participant"}, REVENUE_ANSWER),
    ],
)
async def test_claim_that_is_not_supported_or_not_renderable_is_never_spoken(
    claim: dict[str, str], answer: str
) -> None:
    turn = await revenue_turn("Revenue is $1.2M.", claim, answer=answer)

    assert turn.meeting.synth.texts == [NOT_ENOUGH]
    assert all(binding.rendering is None for binding in turn.meeting.bindings)


async def test_model_declared_scope_or_extra_field_voids_the_proposal() -> None:
    turn = await revenue_turn("Revenue is $1.2M.", {**REVENUE_CLAIM, "scope": "no_visual"})

    assert turn.meeting.synth.texts == [NOT_ENOUGH]


async def test_same_claim_declared_twice_is_spoken_once() -> None:
    turn = await revenue_turn("Revenue is $1.2M.", REVENUE_CLAIM, dict(REVENUE_CLAIM))

    assert turn.meeting.synth.texts == [REVENUE_STATEMENT]


async def test_two_claims_are_two_statements_decided_separately() -> None:
    status = {
        "kind": "screen",
        "shape": "entity_status",
        "entity": "the plan",
        "status": "approved",
    }
    answer = "Project A revenue is $1.2M. The plan is approved."

    turn = await revenue_turn("Both.", REVENUE_CLAIM, status, answer=answer)

    meeting = turn.meeting
    assert meeting.synth.texts == [
        REVENUE_STATEMENT,
        "The currently displayed screen lists the status of The plan as approved.",
    ]
    assert [binding.sentence_index for binding in meeting.bindings] == [0, 1]
    assert len({binding.publication_id for binding in meeting.bindings}) == 2
    assert len(turn.counted) == 2


async def test_supported_claim_is_spoken_and_an_unsupported_one_is_dropped() -> None:
    wrong = {**REVENUE_CLAIM, "value": "$9M"}

    turn = await revenue_turn("Revenue.", wrong, REVENUE_CLAIM)

    assert turn.meeting.synth.texts == [REVENUE_STATEMENT]


async def test_indonesian_turn_is_rendered_with_the_indonesian_pattern() -> None:
    answer = "Beta launch is 2027-03-17"
    meeting = Meeting(inspections=(VisualInspection(verdict="supported", answer=answer),))

    turn = await ask(
        meeting,
        "When is beta?",
        needs=("screen",),
        language="id",
        claims=({**BETA_CLAIM, "value": "2027-03-17"},),
    )

    [binding] = turn.meeting.bindings
    [spoken] = turn.meeting.synth.texts
    assert binding.rendering is not None
    assert binding.rendering.language == "id"
    pattern = pattern_text("current", "entity_attribute_value", "id")
    assert pattern is not None
    assert spoken == pattern.format(attribute="launch", entity="Beta", value="2027-03-17")
    assert rerender(binding.rendering, answer) == spoken


async def test_turn_language_without_a_pattern_abstains() -> None:
    turn = await ask(Meeting(), "When is beta?", needs=("screen",), language="fr")

    assert turn.meeting.synth.texts == [NOT_ENOUGH]


async def test_turn_without_visual_needs_speaks_the_model_text_as_today() -> None:
    turn = await ask(Meeting(), "How are you?", reply="I am well.")

    assert turn.meeting.synth.texts == ["I am well."]
    [binding] = turn.meeting.bindings
    assert (binding.scope, binding.rendering) == ("no_visual", None)


async def test_without_a_publication_context_the_model_text_is_spoken_as_today() -> None:
    meeting = Meeting(gated=False)

    await ask(meeting, "When is beta?", needs=("screen",), claims=())

    assert meeting.synth.texts == ["Beta is on 17 March 2027."]


async def test_model_written_current_binding_is_refused_by_the_engine() -> None:
    meeting = Meeting()
    turn = await ask(meeting, "When is beta?", needs=("screen",))
    [rendered] = turn.meeting.bindings
    assert rendered.rendering is not None

    meeting = Meeting()
    text = "Revenue is $1.2M right now."
    prepared = await meeting.prepare(text, sentence_index=0, transcript_id="f1")
    with pytest.raises(ValueError, match="rendering digest"):
        meeting.engine.bind_publication(
            prepared, replace(rendered, sentence_hash=sentence_digest(text))
        )
    meeting.engine.bind_publication(
        prepared, replace(rendered, sentence_hash=sentence_digest(text), rendering=None)
    )
    await meeting.engine.speak_prepared(prepared)

    assert meeting.prevented == "unknown:statement_not_rendered"
    await meeting.engine.close()


class ScriptedStream:
    def __init__(self, *pieces: str) -> None:
        self.pieces = pieces

    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        for piece in self.pieces:
            yield SpokenChunk(text=piece, language="en")


class ScriptedWhole:
    async def respond(self, _transcript: str) -> SpokenChunk:
        return SpokenChunk(text="Revenue is $1.2M and growing fast.", language="en")


async def test_streamed_reply_is_withheld_whole_and_yields_nothing_to_speak() -> None:
    withheld: list[str] = []
    stream = WithheldReplies(
        ScriptedStream("Revenue is $1.2M ", "and growing fast."), withheld.append
    )

    spoken = [chunk async for chunk in stream.stream_reply("question")]

    assert spoken == []
    assert withheld == ["Revenue is $1.2M ", "and growing fast."]


async def test_whole_reply_is_withheld_and_replaced_by_an_empty_chunk() -> None:
    withheld: list[str] = []

    chunk = await WithheldReply(ScriptedWhole(), withheld.append).respond("question")

    assert chunk.text == ""
    assert withheld == ["Revenue is $1.2M and growing fast."]


def test_synthesis_is_reached_from_the_conversation_only_through_two_guarded_funnels() -> None:
    root = Path(__file__).parent.parent / "src" / "fakeyou" / "conversation"
    callers: set[tuple[str, str]] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "prepare"
                    and "speaker" in ast.unparse(node.func.value)
                ):
                    callers.add((path.name, function.name))

    assert callers == {
        ("turn_graph.py", "prepare"),
        ("turn_graph.py", "_speak_state_bound"),
        ("visual_speech.py", "abstain_with"),
        ("visual_speech.py", "_state"),
    }
