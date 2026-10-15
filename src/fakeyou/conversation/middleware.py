from collections.abc import Awaitable, Callable, Sequence
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)

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


def patch_orphan_tool_calls(messages: list[AnyMessage]) -> list[AnyMessage]:
    answered = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    patched: list[AnyMessage] = []
    changed = False
    for message in messages:
        patched.append(message)
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                call_id = call["id"]
                if call_id is None or call_id in answered:
                    continue
                answered.add(call_id)
                patched.append(ToolMessage(content=ORPHAN_TOOL_RESULT, tool_call_id=call_id))
                changed = True
    return patched if changed else messages


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
    return patch_orphan_tool_calls(_drop_dangling_answers(kept))


class OrphanToolCallMiddleware(AgentMiddleware[Any, Any, Any]):
    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        messages = list(request.messages)
        patched = patch_orphan_tool_calls(messages)
        if patched is messages:
            return await handler(request)
        return await handler(request.override(messages=patched))


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
