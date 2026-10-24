from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast, override

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from fakeyou.foundation.triggers import current_trigger, meeting_event_of, trigger_kind
from fakeyou.meet.meet_tools import MEET_SEND_CHAT

ORPHAN_TOOL_RESULT = "Tool call did not complete; no result is available."
TOKENS_PER_MESSAGE = 2


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def estimate_message_tokens(messages: Sequence[BaseMessage]) -> int:
    return sum(len(_message_text(message)) // 4 + TOKENS_PER_MESSAGE for message in messages)


def _answered_calls(messages: Sequence[BaseMessage]) -> set[str]:
    return {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}


def unanswered_tool_calls(messages: Sequence[BaseMessage]) -> list[str]:
    answered = _answered_calls(messages)
    pending: list[str] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            call_id = call["id"]
            if call_id is not None and call_id not in answered:
                answered.add(call_id)
                pending.append(call_id)
    return pending


def patch_orphan_tool_calls(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    pending = unanswered_tool_calls(messages)
    patched: list[BaseMessage] = []
    for message in messages:
        patched.append(message)
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call["id"] in pending:
                pending.remove(call["id"])
                patched.append(ToolMessage(content=ORPHAN_TOOL_RESULT, tool_call_id=call["id"]))
    return patched


def _drop_dangling_answers(messages: list[AnyMessage]) -> list[AnyMessage]:
    calls = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage) and message.tool_calls
        for call in message.tool_calls
        if call["id"] is not None
    }
    pruned = [
        message
        for message in messages
        if not (isinstance(message, ToolMessage) and message.tool_call_id not in calls)
    ]
    return pruned if len(pruned) != len(messages) else messages


def trim_messages_to_budget(messages: list[AnyMessage], *, max_tokens: int) -> list[AnyMessage]:
    if estimate_message_tokens(messages) <= max_tokens:
        return messages
    system = [message for message in messages if isinstance(message, SystemMessage)]
    rest = [message for message in messages if not isinstance(message, SystemMessage)]
    budget = max_tokens - estimate_message_tokens(system)
    kept_reversed: list[AnyMessage] = []
    total = 0
    for message in reversed(rest):
        cost = len(_message_text(message)) // 4 + TOKENS_PER_MESSAGE
        if kept_reversed and total + cost > budget:
            break
        kept_reversed.append(message)
        total += cost
    kept = system + list(reversed(kept_reversed))
    return cast(list[AnyMessage], patch_orphan_tool_calls(_drop_dangling_answers(kept)))


class OrphanToolCallMiddleware(AgentMiddleware[Any, Any, Any]):
    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        messages = list(request.messages)
        patched = patch_orphan_tool_calls(messages)
        if len(patched) == len(messages):
            return await handler(request)
        return await handler(request.override(messages=cast(list[AnyMessage], patched)))


class HistoryTrimMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, *, max_tokens: int) -> None:
        self._max_tokens = max_tokens

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        messages = list(request.messages)
        trimmed = trim_messages_to_budget(messages, max_tokens=self._max_tokens)
        if trimmed is messages:
            return await handler(request)
        return await handler(request.override(messages=trimmed))


def _tool_name(tool: object) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class ChatTurnToolsMiddleware(AgentMiddleware[Any, Any, Any]):
    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        _, trigger = current_trigger(request.messages)
        if not trigger or trigger_kind(trigger[-1]) != "chat":
            return await handler(request)
        tools = [tool for tool in request.tools if _tool_name(tool) != MEET_SEND_CHAT]
        if len(tools) == len(request.tools):
            return await handler(request)
        return await handler(request.override(tools=tools))


def attributed(message: AnyMessage) -> AnyMessage:
    event = meeting_event_of(message)
    speaker = event.get("speaker") if event is not None else None
    if not isinstance(message, HumanMessage) or not isinstance(speaker, str):
        return message
    if isinstance(message.content, str):
        return message.model_copy(update={"content": f"{speaker}: {message.content}"})
    labelled: list[str | dict[Any, Any]] = [{"type": "text", "text": f"{speaker}:"}]
    return message.model_copy(update={"content": [*labelled, *message.content]})


class SpeakerAttributionMiddleware(AgentMiddleware[Any, Any, Any]):
    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(
            request.override(messages=[attributed(message) for message in request.messages])
        )


class PlainReplyMiddleware(AgentMiddleware[Any, Any, Any]):
    @override
    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        try:
            return handler(request)
        except StructuredOutputValidationError as error:
            return ModelResponse(result=[error.ai_message])

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        try:
            return await handler(request)
        except StructuredOutputValidationError as error:
            return ModelResponse(result=[error.ai_message])
