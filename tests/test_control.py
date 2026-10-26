from datetime import UTC, datetime
from typing import cast

from langchain_core.messages import AIMessage

from fakeyou.conversation.control import ReplyControl
from fakeyou.conversation.conversation import MeetingAgent, MeetingResponder
from fakeyou.conversation.reply_planner import ReplyHandlers, ReplyPlanner, planned_reply
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.persistence.record import TwinSpeechEntry
from tests._conversation_fakes import ToolCallingFakeModel


class _History:
    def __init__(self, entries: list[TwinSpeechEntry]) -> None:
        self._entries = entries

    def entries(self) -> list[TwinSpeechEntry]:
        return self._entries


def _twin(text: str, *, interrupted: bool = False) -> TwinSpeechEntry:
    return TwinSpeechEntry(at=datetime.now(UTC), text=text, interrupted=interrupted)


def _event(text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id="t1",
        timestamp=datetime.now(UTC).isoformat(),
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
    )


def test_the_planner_hands_the_intent_to_the_handler() -> None:
    seen: list[str] = []

    planner = ReplyPlanner(
        max_sentences=2,
        language="en",
        handlers=ReplyHandlers(on_intent=lambda _intent: seen.append(_intent)),
    )
    planner.feed('{"language": "en", "intent": "shorter", "text": "Okay, shorter."}')

    assert seen == ["shorter"]
    assert planner.intent == "shorter"


def test_the_planner_keeps_the_words_the_handler_returns() -> None:
    planner = ReplyPlanner(
        max_sentences=2,
        language="en",
        handlers=ReplyHandlers(on_intent=lambda _intent: "Earlier sentence."),
    )

    assert (
        planner.feed('{"language": "en", "intent": "repeat", "text": "Let me say that again."}')
        == "Earlier sentence."
    )


def test_a_shorter_plan_lowers_the_sentence_budget() -> None:
    control = ReplyControl()
    responder = MeetingResponder(
        cast(MeetingAgent, ToolCallingFakeModel(responses=[AIMessage(content="x")])),
        _History([]),
        control=control,
        language="en",
    )

    reply = responder.on_intent("shorter")

    assert reply is None
    assert control.sentence_cap == 1
    assert control.limit(2) == 1


def test_a_repeat_plan_replays_the_last_twin_sentence() -> None:
    control = ReplyControl()
    responder = MeetingResponder(
        cast(MeetingAgent, ToolCallingFakeModel(responses=[AIMessage(content="x")])),
        _History([_twin("The launch is on 14 October.")]),
        control=control,
        language="en",
    )

    assert responder.on_intent("repeat") == "The launch is on 14 October."


def test_a_repeat_plan_without_a_previous_sentence_says_nothing() -> None:
    responder = MeetingResponder(
        cast(MeetingAgent, ToolCallingFakeModel(responses=[AIMessage(content="x")])),
        _History([]),
        language="en",
    )

    assert responder.on_intent("repeat") is None


def test_a_louder_plan_asks_the_session_for_more_volume() -> None:
    control = ReplyControl()
    calls: list[int] = []
    control.on_louder = lambda: calls.append(1)
    responder = MeetingResponder(
        cast(MeetingAgent, ToolCallingFakeModel(responses=[AIMessage(content="x")])),
        _History([]),
        control=control,
        language="en",
    )

    responder.on_intent("louder")

    assert control.gain_steps == 1
    assert calls == [1]


def test_the_control_intents_are_spoken_as_one_short_sentence() -> None:
    assert (
        planned_reply(
            '{"language": "en", "intent": "shorter", "text": "Okay, shorter. Anything else?"}',
            max_sentences=2,
            language="en",
        ).text
        == "Okay, shorter."
    )


def test_a_stop_plan_keeps_the_acknowledgement_short() -> None:
    assert (
        planned_reply(
            '{"language": "en", "intent": "stop", "text": "Okay, stopping. Anything else?"}',
            max_sentences=2,
            language="en",
        ).text
        == "Okay, stopping."
    )


def test_a_repeat_plan_skips_a_sentence_that_was_cut_off() -> None:
    responder = MeetingResponder(
        cast(MeetingAgent, ToolCallingFakeModel(responses=[AIMessage(content="x")])),
        _History([_twin("The launch is on 14 October.", interrupted=True)]),
        control=ReplyControl(),
        language="en",
    )

    assert responder.on_intent("repeat") is None


def test_a_repeat_plan_never_replays_the_same_sentence_twice() -> None:
    responder = MeetingResponder(
        cast(MeetingAgent, ToolCallingFakeModel(responses=[AIMessage(content="x")])),
        _History([_twin("The launch is on 14 October."), _twin("The launch is on 14 October.")]),
        control=ReplyControl(),
        language="en",
    )

    assert responder.on_intent("repeat") is None
