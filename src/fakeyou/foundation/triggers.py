from collections.abc import Sequence
from typing import Any, Literal, get_args

from langchain_core.messages import BaseMessage, HumanMessage

MEETING_EVENT = "meeting_event"
TriggerKind = Literal["speech", "chat", "attention"]
ReplyChannel = Literal["voice", "chat"]
TRIGGER_KINDS: frozenset[str] = frozenset(get_args(TriggerKind))
DEFAULT_TRIGGER_KIND: TriggerKind = "speech"


def meeting_event_of(message: BaseMessage) -> dict[str, Any] | None:
    event = (message.additional_kwargs or {}).get(MEETING_EVENT)
    return event if isinstance(event, dict) else None


def is_history(message: BaseMessage) -> bool:
    event = meeting_event_of(message)
    return event is not None and event.get("history") is True


def trigger_kind(message: BaseMessage) -> TriggerKind | None:
    if not isinstance(message, HumanMessage):
        return None
    event = meeting_event_of(message)
    if event is None:
        return None
    match event.get("kind", DEFAULT_TRIGGER_KIND):
        case "speech":
            return "speech"
        case "chat":
            return "chat"
        case "attention":
            return "attention"
        case _:
            return None


def current_trigger(messages: Sequence[BaseMessage]) -> tuple[int, list[HumanMessage]]:
    run: list[HumanMessage] = []
    end = 0
    for index, message in enumerate(messages):
        if trigger_kind(message) is None or is_history(message):
            continue
        if not isinstance(message, HumanMessage):
            continue
        if end != index:
            run = []
        run.append(message)
        end = index + 1
    return end, run
