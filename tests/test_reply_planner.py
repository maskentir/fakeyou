import json

from fakeyou.conversation.reply_planner import ReplyPlanner, planned_reply
from fakeyou.foundation.sentences import split_sentences


def plan(text: str, *, intent: str = "answer", language: str = "en") -> str:
    return json.dumps({"language": language, "intent": intent, "text": text})


def test_the_plan_fields_are_read_and_only_the_text_is_spoken() -> None:
    planned = planned_reply(plan("The launch is on 14 October."), max_sentences=2, language="id")

    assert planned.text == "The launch is on 14 October."
    assert planned.language == "en"


def test_a_plain_reply_is_spoken_as_an_answer_in_the_default_language() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")

    assert planner.feed("The launch is on 14 October.") == "The launch is on 14 October."
    assert not planner.explicit
    assert planner.language == "id"


def test_a_streaming_plan_releases_the_text_as_it_grows() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")
    raw = plan("The launch is on 14 October.")
    pieces = [raw[:12], raw[12:40], raw[40:60], raw[60:]]

    released = "".join(planner.feed(piece) for piece in pieces) + planner.finish()

    assert released == "The launch is on 14 October."
    assert planner.language == "en"


def test_nothing_is_released_before_the_intent_and_language_are_known() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")

    assert planner.feed('{"language": "en", "inte') == ""


def test_an_unknown_intent_is_treated_as_an_answer() -> None:
    planned = planned_reply(plan("The launch.", intent="banana"), max_sentences=2, language="en")

    assert planned.text == "The launch."


def test_a_silent_plan_says_nothing() -> None:
    assert planned_reply(plan("", intent="silent"), max_sentences=2, language="en").text == ""


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


def test_an_answer_plan_drops_the_closing_question() -> None:
    planned = planned_reply(
        plan("The launch is on 14 October. Anything else?"), max_sentences=2, language="en"
    )

    assert planned.text == "The launch is on 14 October."


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
    raw = json.dumps(
        {
            "understood": "Rani, what did you work on today?",
            "language": "en",
            "intent": "answer",
            "text": "I worked on the launch checklist.",
        }
    )

    pieces = [raw[start : start + 7] for start in range(0, len(raw), 7)]
    released = "".join(planner.feed(piece) for piece in pieces) + planner.finish()

    assert "work on today" not in released
    assert planner.language == "en"
