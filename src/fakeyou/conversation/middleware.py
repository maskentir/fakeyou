from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast, override

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ModelRetryMiddleware,
)
from langchain.agents.middleware.types import ToolCallRequest
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.exceptions import ModelError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command

from fakeyou.foundation.triggers import current_trigger, meeting_event_of, trigger_kind
from fakeyou.meet.meet_tools import MEET_SEND_CHAT, MeetToolResult, render_tool_result
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_exception

ORPHAN_TOOL_RESULT = "Tool call did not complete; no result is available."


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


def trim_messages_to_budget(messages: list[AnyMessage], *, max_tokens: int) -> list[AnyMessage]:
    if count_tokens_approximately(messages) <= max_tokens:
        return messages
    trimmed = trim_messages(
        messages,
        max_tokens=max_tokens,
        token_counter="approximate",
        strategy="last",
        include_system=True,
        start_on="human",
    )
    if not any(not isinstance(message, SystemMessage) for message in trimmed):
        system = [message for message in messages if isinstance(message, SystemMessage)]
        trimmed = [*system, messages[-1]]
    return cast(list[AnyMessage], patch_orphan_tool_calls(trimmed))


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


MEET_ACTION_TOOLS = frozenset({MEET_SEND_CHAT, "meet_raise_hand", "meet_lower_hand"})


def _meet_tools_used(messages: Sequence[AnyMessage]) -> set[str]:
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            start = index
    return {
        message.name
        for message in messages[start:]
        if isinstance(message, ToolMessage) and message.name in MEET_ACTION_TOOLS
    }


class MeetActionOnceMiddleware(AgentMiddleware[Any, Any, Any]):
    def _without_used(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        used = _meet_tools_used(request.messages)
        if not used:
            return request
        tools = [tool for tool in request.tools if _tool_name(tool) not in used]
        if len(tools) == len(request.tools):
            return request
        return request.override(tools=tools)

    def _duplicate(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call.get("name")
        messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
        if name not in MEET_ACTION_TOOLS or name not in _meet_tools_used(messages):
            return None
        return ToolMessage(
            content=render_tool_result(
                MeetToolResult(
                    "duplicate",
                    "Already queued in this turn. Do not send it again; "
                    "give your spoken reply now or stay silent.",
                )
            ),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=str(name),
        )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._without_used(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._without_used(request))

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        duplicate = self._duplicate(request)
        return duplicate if duplicate is not None else handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        duplicate = self._duplicate(request)
        return duplicate if duplicate is not None else await handler(request)


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


class EvidenceRoutingMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(
        self,
        strong: BaseChatModel,
        *,
        evidence_tools: frozenset[str],
        journal: Journal | None = None,
    ) -> None:
        self._strong = strong
        self._evidence_tools = evidence_tools
        self._journal = journal or Journal("", echo=False)

    def _strong_trigger(self, request: ModelRequest[Any]) -> str | None:
        messages = request.messages
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                break
            if message.additional_kwargs.get("synthetic_view"):
                continue
            if message.additional_kwargs.get("memory_prefetch"):
                continue
            if message.name in self._evidence_tools:
                return message.name or "evidence_tool"
        _, trigger = current_trigger(messages)
        if trigger and trigger[-1].id:
            expected = f"{trigger[-1].id}-memory"
            for message in messages:
                if not isinstance(message, ToolMessage) or message.id != expected:
                    continue
                prefetch = message.additional_kwargs.get("memory_prefetch")
                if isinstance(prefetch, dict) and prefetch.get("outcome") == "found":
                    return "memory_prefetch_found"
        return None

    def route(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        trigger = self._strong_trigger(request)
        self._journal.record(
            "model_route_selected",
            source="agent",
            route="strong" if trigger is not None else "small",
            trigger=trigger or "none",
        )
        if trigger is None:
            return request
        return request.override(model=self._strong)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self.route(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self.route(request))


@dataclass(slots=True)
class SpeechMark:
    started: bool = False


SPEECH_MARK: ContextVar[SpeechMark | None] = ContextVar("speech_mark", default=None)
REPLY_RETRIES = 1
REPLY_RETRY_DELAY_SECONDS = 0.2


def retry_before_speech(error: Exception) -> bool:
    mark = SPEECH_MARK.get()
    if mark is not None and mark.started:
        return False
    if isinstance(error, ModelError):
        return error.is_retryable
    return classify_exception("llm", error).retryable


def create_reply_retry_middleware() -> ModelRetryMiddleware:
    return ModelRetryMiddleware(
        max_retries=REPLY_RETRIES,
        retry_on=retry_before_speech,
        on_failure="error",
        initial_delay=REPLY_RETRY_DELAY_SECONDS,
        max_delay=REPLY_RETRY_DELAY_SECONDS,
    )
