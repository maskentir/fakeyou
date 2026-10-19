from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from fakeyou.foundation.greetings import create_greeting_middleware


def _greet(kind: str) -> Any:
    middleware = create_greeting_middleware("en")
    message = HumanMessage(
        content="hi", id="m1", additional_kwargs={"meeting_event": {"kind": kind}}
    )
    return middleware.before_agent({"messages": [message]}, cast(Runtime[Any], None))


def test_a_spoken_greeting_gets_the_canned_reply() -> None:
    result = _greet("speech")

    assert result is not None
    assert isinstance(result["messages"][0], AIMessage)


def test_chat_and_attention_triggers_are_never_greeted() -> None:
    assert _greet("chat") is None
    assert _greet("attention") is None
