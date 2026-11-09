from typing import get_type_hints

from fakeyou.conversation.reply_planner import (
    Claim,
    PlanRead,
    ReplyHandlers,
    ReplyPlanner,
    SpeechGate,
    SpeechPiece,
    SpeechPlan,
    TurnLanguage,
    _ClaimDraft,
    _PlanDraft,
    planned_reply,
)
from fakeyou.foundation.sentences import split_sentences
from fakeyou.session.speech import BookEntry, ClaimHeader, FactBook, SpeechVerdict
from tests._conversation_support import claim, plan_reply


def plan(text: str, *, intent: str = "answer", language: str = "en") -> str:
    return plan_reply(text, intent=intent, language=language)


def spoken_text(pieces: list[SpeechPiece]) -> str:
    return "".join(piece.text for piece in pieces)


def beta_gate() -> SpeechGate:
    entry = BookEntry("om-beta", "The beta launches on 14 October.", "owner_model")
    return SpeechGate(lambda: FactBook({"om-beta": entry}))


def test_the_plan_fields_are_read_and_only_the_claims_are_spoken() -> None:
    planned = planned_reply(plan("The launch is on 14 October."), max_sentences=2, language="id")

    assert planned.text == "The launch is on 14 October."
    assert planned.language == "en"


def test_a_plain_reply_is_spoken_as_an_answer_in_the_default_language() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")

    assert planner.feed("The launch is on 14 October.") == [
        SpeechPiece(0, "The launch is on 14 October.")
    ]
    assert not planner.explicit
    assert planner.language == "id"


def test_a_streaming_plan_releases_the_text_as_it_grows() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")
    raw = plan("The launch is on 14 October.")
    pieces = [raw[start : start + 9] for start in range(0, len(raw), 9)]

    released = [part for piece in pieces for part in planner.feed(piece)] + planner.finish()

    assert spoken_text(released) == "The launch is on 14 October."
    assert {part.claim for part in released} == {0}
    assert planner.language == "en"


def test_each_claim_is_released_under_its_own_index() -> None:
    planner = ReplyPlanner(max_sentences=3, language="en")
    raw = plan_reply(claim("The beta moved.", "om-beta"), claim("Rani asked for it.", "mem-1"))
    pieces = [raw[start : start + 5] for start in range(0, len(raw), 5)]

    released = [part for piece in pieces for part in planner.feed(piece)] + planner.finish()

    by_claim = {
        index: "".join(part.text for part in released if part.claim == index) for index in (0, 1)
    }
    assert by_claim == {0: "The beta moved.", 1: "Rani asked for it."}
    assert [header.refs for header in planner.claims] == [("om-beta",), ("mem-1",)]


def test_a_truncated_ref_is_never_judged() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en", speech_gate=beta_gate())
    raw = plan_reply(claim("The beta launches on 14 October.", "om-beta"))
    cut = raw.index("om-beta") + len("om-be")

    assert spoken_text(planner.feed(raw[:cut])) == ""
    assert not planner.claims
    released = planner.feed(raw[cut:]) + planner.finish()

    assert spoken_text(released) == "The beta launches on 14 October."
    assert planner.claims[0].refs == ("om-beta",)
    assert planner.take_sentence("The beta launches on 14 October.") == [
        "The beta launches on 14 October."
    ]


def test_nothing_is_released_before_the_intent_and_language_are_known() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")

    assert planner.feed('{"language": "en", "inte') == []


def test_an_unknown_intent_leaves_the_plan_unparsed() -> None:
    rejected: list[SpeechVerdict] = []
    planned = planned_reply(
        plan("The launch.", intent="banana"),
        max_sentences=2,
        language="en",
        handlers=ReplyHandlers(on_gate=lambda verdict, _claim: rejected.append(verdict)),
        speech_gate=beta_gate(),
    )

    assert planned.text == "I'm not sure about that, so I'd rather not guess."
    assert rejected == ["unparsed_plan"]


def test_an_unknown_claim_act_leaves_the_plan_unparsed_even_in_the_last_claim() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en", speech_gate=beta_gate())

    spoken = planner.speak(
        plan_reply(claim("Hello.", act="meta"), claim("The beta is late.", act="rumour"))
    )

    assert planner.blocked == "unparsed_plan"
    assert "The beta is late." not in spoken


def test_a_claim_without_its_header_leaves_the_plan_unparsed() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en", speech_gate=beta_gate())

    spoken = planner.speak(
        '{"language": "en", "intent": "answer", "claims": [{"say": "The beta is late."}]}'
    )

    assert planner.blocked == "unparsed_plan"
    assert spoken == ["I'm not sure about that, so I'd rather not guess."]


def test_a_plan_that_never_lists_its_claims_falls_back_instead_of_going_quiet() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en", speech_gate=beta_gate())

    spoken = planner.speak('{"language": "en", "intent": "answer", "text": "The beta is late."}')

    assert planner.blocked == "unparsed_plan"
    assert spoken == ["I'm not sure about that, so I'd rather not guess."]


def test_a_silent_plan_needs_no_claims() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en", speech_gate=beta_gate())

    assert planner.speak('{"language": "en", "intent": "silent"}') == []
    assert planner.blocked is None


def test_a_silent_plan_says_nothing() -> None:
    assert planned_reply(plan_reply(intent="silent"), max_sentences=2, language="en").text == ""


def test_an_ack_plan_speaks_one_sentence() -> None:
    planned = planned_reply(
        plan("Okay. Anything else?", intent="ack"), max_sentences=2, language="en"
    )

    assert planned.text == "Okay."


def test_a_decline_plan_speaks_one_sentence() -> None:
    planned = planned_reply(
        plan("I cannot approve that. The owner has to decide.", intent="decline"),
        max_sentences=2,
        language="en",
    )

    assert planned.text == "I cannot approve that."


def test_an_answer_plan_keeps_the_closing_question() -> None:
    planned = planned_reply(
        plan("The launch is on 14 October. Anything else?"), max_sentences=2, language="en"
    )

    assert planned.text == "The launch is on 14 October. Anything else?"


def test_an_answer_plan_keeps_the_question_when_it_is_all_there_is() -> None:
    assert planned_reply(plan("Which launch?"), max_sentences=2, language="en").text == (
        "Which launch?"
    )


def test_a_clarify_plan_keeps_its_question() -> None:
    planned = planned_reply(plan("Which launch?", intent="clarify"), max_sentences=2, language="en")

    assert planned.text == "Which launch?"


def test_a_plain_reply_keeps_its_closing_question() -> None:
    planned = planned_reply(
        "The launch is on 14 October. Anything else?", max_sentences=2, language="en"
    )

    assert planned.text == "The launch is on 14 October. Anything else?"


def test_sentences_are_split_by_the_language_of_the_reply() -> None:
    assert split_sentences("Good morning. How are you?", language="en") == [
        "Good morning.",
        "How are you?",
    ]


def test_the_understood_line_is_never_spoken() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")
    raw = plan_reply(
        "I worked on the launch checklist.", understood="Rani, what did you work on today?"
    )

    pieces = [raw[start : start + 7] for start in range(0, len(raw), 7)]
    released = [part for piece in pieces for part in planner.feed(piece)] + planner.finish()

    assert "work on today" not in spoken_text(released)
    assert planner.language == "en"


def test_the_plan_read_is_reported_with_its_intent_language_and_claims() -> None:
    reads: list[PlanRead] = []
    planner = ReplyPlanner(
        max_sentences=2, language="id", handlers=ReplyHandlers(on_plan=reads.append)
    )

    planner.speak(plan_reply(claim("The beta launches on 14 October.", "om-beta")))

    assert [(read.structured, read.intent, read.language) for read in reads] == [
        (True, "answer", "en")
    ]
    assert reads[0].claims == (
        ClaimHeader(
            act="fact",
            basis="owner_model",
            refs=("om-beta",),
            certainty="stated",
            speaks_as="owner_proxy",
        ),
    )


def test_a_plain_reply_is_reported_as_a_reply_without_a_plan() -> None:
    reads: list[PlanRead] = []
    planner = ReplyPlanner(
        max_sentences=2, language="id", handlers=ReplyHandlers(on_plan=reads.append)
    )

    planner.speak("The launch is on 14 October.")

    assert [(read.structured, read.intent, read.language, read.claims) for read in reads] == [
        (False, "answer", "id", ())
    ]


def test_a_repeat_intent_is_reported_as_a_replacement() -> None:
    reads: list[PlanRead] = []
    planner = ReplyPlanner(
        max_sentences=2,
        language="en",
        handlers=ReplyHandlers(
            on_intent=lambda intent: "The last line." if intent == "repeat" else None,
            on_plan=reads.append,
        ),
    )

    assert planner.speak(plan("Sure.", intent="repeat")) == ["The last line."]
    assert reads[0].intent == "repeat"
    assert reads[0].replaced is True


def test_the_plan_is_reported_once_per_reply() -> None:
    reads: list[PlanRead] = []
    planner = ReplyPlanner(
        max_sentences=2, language="en", handlers=ReplyHandlers(on_plan=reads.append)
    )
    raw = plan("The launch is on 14 October.")

    planner.feed(raw[:20])
    planner.feed(raw[20:])
    planner.finish()
    planner.finish_sentences()
    planner.finish_sentences()

    assert len(reads) == 1


def test_the_declared_language_reaches_the_turn() -> None:
    language = TurnLanguage("en")
    planner = ReplyPlanner(
        max_sentences=2, language="en", handlers=ReplyHandlers(on_language=language.declare)
    )

    planner.feed(plan("Baik.", intent="answer", language="id"))

    assert language.language == "id"


def test_the_plan_text_joins_what_each_claim_says() -> None:
    parsed = SpeechPlan.model_validate_json(
        plan_reply(claim("The beta moved.", "om-beta"), claim("Rani asked.", "mem-1"))
    )

    assert parsed.text == "The beta moved. Rani asked."


def test_the_plan_schema_puts_claims_after_the_header_and_say_last_in_a_claim() -> None:
    schema = SpeechPlan.model_json_schema()

    assert list(schema["properties"]) == [
        "understood",
        "language",
        "intent",
        "commits_owner",
        "claims",
    ]
    assert list(schema["$defs"]["Claim"]["properties"]) == [
        "act",
        "basis",
        "refs",
        "certainty",
        "speaks_as",
        "say",
    ]


def test_the_partial_drafts_mirror_the_plan_models() -> None:
    assert list(get_type_hints(_PlanDraft)) == list(SpeechPlan.model_fields)
    assert list(get_type_hints(_ClaimDraft)) == list(Claim.model_fields)
