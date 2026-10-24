from fakeyou.conversation.reply_planner import ReplyPlanner, planned_reply, split_sentences


def test_the_intent_line_is_read_and_removed_from_the_reply() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en")

    assert planner.feed("intent: answer\nThe launch is on 14 October.") == (
        "The launch is on 14 October."
    )
    assert planner.intent == "answer"
    assert planner.explicit


def test_a_reply_without_an_intent_line_is_left_alone() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en")

    assert planner.feed("The launch is on 14 October.") == "The launch is on 14 October."
    assert not planner.explicit


def test_the_intent_line_is_read_while_it_arrives_in_pieces() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en")

    assert planner.feed("int") == ""
    assert planner.feed("ent: an") == ""
    assert planner.feed("swer\nThe launch") == "The launch"
    assert planner.intent == "answer"


def test_a_malformed_intent_line_is_dropped_and_the_rest_is_spoken() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en")

    assert planner.feed("intent: banana\nThe launch is on 14 October.") == (
        "The launch is on 14 October."
    )
    assert planner.intent == "answer"


def test_a_silent_plan_says_nothing() -> None:
    assert planned_reply("intent: silent\n", max_sentences=2, language="en").text == ""


def test_an_ack_plan_speaks_one_sentence() -> None:
    assert (
        planned_reply("intent: ack\nOkay. Anything else?", max_sentences=2, language="en").text
        == "Okay."
    )


def test_a_decline_plan_speaks_one_sentence() -> None:
    assert (
        planned_reply(
            "intent: decline\nI cannot approve that. The owner has to decide.",
            max_sentences=2,
            language="en",
        ).text
        == "I cannot approve that."
    )


def test_an_answer_plan_drops_the_closing_question() -> None:
    assert (
        planned_reply(
            "intent: answer\nThe launch is on 14 October. Anything else?",
            max_sentences=2,
            language="en",
        ).text
        == "The launch is on 14 October."
    )


def test_an_answer_plan_keeps_the_question_when_it_is_all_there_is() -> None:
    assert (
        planned_reply("intent: answer\nWhich launch?", max_sentences=2, language="en").text
        == "Which launch?"
    )


def test_a_clarify_plan_keeps_its_question() -> None:
    assert (
        planned_reply("intent: clarify\nWhich launch?", max_sentences=2, language="en").text
        == "Which launch?"
    )


def test_a_reply_without_an_intent_line_keeps_its_closing_question() -> None:
    assert planned_reply(
        "The launch is on 14 October. Anything else?", max_sentences=2, language="en"
    ).text == ("The launch is on 14 October. Anything else?")


def test_sentences_are_split_by_the_language_of_the_reply() -> None:
    assert split_sentences("Good morning. How are you?", language="en") == [
        "Good morning.",
        "How are you?",
    ]


def test_the_declared_language_travels_with_the_reply() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")

    planner.feed("intent: answer lang: en\nThe launch is on 14 October.")

    assert planner.language == "en"


def test_a_reply_without_a_language_keeps_the_default() -> None:
    planner = ReplyPlanner(max_sentences=2, language="id")

    planner.feed("The launch is on 14 October.")

    assert planner.language == "id"


def test_the_planned_reply_carries_the_declared_language() -> None:
    planned = planned_reply(
        "intent: answer lang: en\nThe launch is on 14 October.", max_sentences=2, language="id"
    )

    assert planned.language == "en"
    assert planned.text == "The launch is on 14 October."
