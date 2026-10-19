from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fakeyou.foundation.triggers import current_trigger, meeting_event_of, trigger_kind


def _event(text: str, message_id: str, **event: str) -> HumanMessage:
    return HumanMessage(content=text, id=message_id, additional_kwargs={"meeting_event": event})


def test_meeting_events_default_to_speech() -> None:
    assert trigger_kind(_event("hi", "t1")) == "speech"
    assert trigger_kind(_event("deck?", "c1", kind="chat")) == "chat"
    assert trigger_kind(_event("notice", "a1", kind="attention")) == "attention"


def test_other_messages_are_not_triggers() -> None:
    assert trigger_kind(_event("caption", "v1", kind="visual")) is None
    assert trigger_kind(HumanMessage(content="plain")) is None
    assert trigger_kind(AIMessage(content="reply")) is None
    assert (
        meeting_event_of(HumanMessage(content="x", additional_kwargs={"meeting_event": 3})) is None
    )


def test_current_trigger_is_the_last_contiguous_run() -> None:
    messages = [
        _event("first", "t1"),
        AIMessage(content="answer"),
        _event("second", "t2"),
        _event("third", "c3", kind="chat"),
    ]

    end, run = current_trigger(messages)

    assert end == 4
    assert [message.id for message in run] == ["t2", "c3"]


def test_tool_traffic_after_the_trigger_keeps_it_current() -> None:
    call = AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "call-1"}])
    messages = [_event("question", "t1"), call, ToolMessage(content="ok", tool_call_id="call-1")]

    end, run = current_trigger(messages)

    assert end == 1
    assert [message.id for message in run] == ["t1"]


def test_no_trigger_yields_an_empty_run() -> None:
    assert current_trigger([AIMessage(content="hello")]) == (0, [])
