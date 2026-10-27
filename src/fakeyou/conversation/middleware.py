import json
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

from fakeyou.foundation.capabilities import MEET_ACTION_TOOLS as REGISTRY_MEET_ACTION_TOOLS
from fakeyou.foundation.capabilities import ONCE_PER_TURN_TOOLS
from fakeyou.foundation.triggers import current_trigger, meeting_event_of, trigger_kind
from fakeyou.meet.meet_tools import (
    MEET_CHECK_ACTION,
    MEET_SEND_CHAT,
    MeetToolResult,
    render_tool_result,
)
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


MEET_ACTION_TOOLS = REGISTRY_MEET_ACTION_TOOLS


def _meet_tools_used(messages: Sequence[AnyMessage]) -> set[str]:
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            start = index
    return {
        message.name
        for message in messages[start:]
        if isinstance(message, ToolMessage)
        and message.name in ONCE_PER_TURN_TOOLS
        and not message.additional_kwargs.get("synthetic_view")
        and not message.additional_kwargs.get("memory_prefetch")
    }


MAX_EVIDENCE_RESULTS_PER_TURN = 2


def _evidence_results_this_turn(messages: Sequence[AnyMessage]) -> int:
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            start = index
    return sum(
        1
        for message in messages[start:]
        if isinstance(message, ToolMessage)
        and message.name in _PLANNED_TOOLS
        and _result_code(message) not in {"refused", "duplicate"}
        and not message.additional_kwargs.get("synthetic_view")
        and not message.additional_kwargs.get("memory_prefetch")
    )


def _pending_stamped_action(tool_name: str | None, messages: Sequence[AnyMessage]) -> bool:
    if tool_name not in _GATED_MEET_TOOLS:
        return False
    if _meet_tools_used(messages) & _GATED_MEET_TOOLS:
        return False
    kind = _trigger_kind(messages)
    if kind == "speech":
        return tool_name == _stamped_action_tool(messages)
    if kind == "chat":
        if _turn_planned(messages):
            return tool_name == _chat_action_tool(messages)
        return tool_name != MEET_SEND_CHAT
    return False


class MeetActionOnceMiddleware(AgentMiddleware[Any, Any, Any]):
    def _without_used(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        used = _meet_tools_used(request.messages)
        evidence_spent = (
            _evidence_results_this_turn(request.messages) >= MAX_EVIDENCE_RESULTS_PER_TURN
        )
        tools = [
            tool
            for tool in request.tools
            if _tool_name(tool) not in used
            and not (evidence_spent and _tool_name(tool) in _PLANNED_TOOLS)
        ]
        if len(tools) == len(request.tools):
            return request
        return request.override(tools=tools)

    def _duplicate(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call.get("name")
        messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
        if (
            name in _PLANNED_TOOLS
            and _evidence_results_this_turn(messages) >= MAX_EVIDENCE_RESULTS_PER_TURN
        ):
            return ToolMessage(
                content=(
                    "The evidence budget for this turn is used up. Answer now with what you have."
                ),
                tool_call_id=str(request.tool_call.get("id") or ""),
                name=str(name),
            )
        if name not in ONCE_PER_TURN_TOOLS or name not in _meet_tools_used(messages):
            return None
        content = (
            render_tool_result(
                MeetToolResult(
                    "duplicate",
                    "Already used in this turn. Do not repeat it; "
                    "give your spoken reply now or stay silent.",
                )
            )
            if name in MEET_ACTION_TOOLS
            else "Already used in this turn. Answer with what you already have."
        )
        return ToolMessage(
            content=content,
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


MEET_ACTION_BY_TOOL = {
    "meet_raise_hand": "raise_hand",
    "meet_lower_hand": "lower_hand",
    "meet_react": "react",
    "meet_mute": "mute",
    "meet_unmute": "unmute",
    "meet_leave": "leave",
}
_GATED_MEET_TOOLS = frozenset({MEET_SEND_CHAT, *MEET_ACTION_BY_TOOL})
_TOOL_BY_MEET_ACTION = {action: tool for tool, action in MEET_ACTION_BY_TOOL.items()}
_TOOLS_BY_EVIDENCE_NEED: dict[str, frozenset[str]] = {
    "screen": frozenset({"look_at_screen"}),
    "participant": frozenset({"look_at_participant"}),
    "chat_history": frozenset({"look_back_chat"}),
    "memory": frozenset({"recall_meeting_context"}),
    "action_status": frozenset({MEET_CHECK_ACTION}),
}
_PLANNED_TOOLS = frozenset(tool for tools in _TOOLS_BY_EVIDENCE_NEED.values() for tool in tools)


def _trigger_event(messages: Sequence[AnyMessage]) -> dict[str, Any] | None:
    _, trigger = current_trigger(messages)
    if not trigger:
        return None
    return meeting_event_of(trigger[-1])


def _trigger_kind(messages: Sequence[AnyMessage]) -> str | None:
    _, trigger = current_trigger(messages)
    if not trigger:
        return None
    return trigger_kind(trigger[-1])


def _stamped_action_tool(messages: Sequence[AnyMessage]) -> str | None:
    event = _trigger_event(messages)
    if event is None:
        return None
    if event.get("requests_chat") is True:
        return MEET_SEND_CHAT
    action = event.get("meet_action")
    return _TOOL_BY_MEET_ACTION.get(action) if isinstance(action, str) else None


def _turn_planned(messages: Sequence[AnyMessage]) -> bool:
    event = _trigger_event(messages)
    return event is not None and event.get("turn_planned") is True


def _chat_action_tool(messages: Sequence[AnyMessage]) -> str | None:
    event = _trigger_event(messages)
    if event is None:
        return None
    action = event.get("meet_action")
    return _TOOL_BY_MEET_ACTION.get(action) if isinstance(action, str) else None


def _evidence_tools(messages: Sequence[AnyMessage]) -> frozenset[str]:
    event = _trigger_event(messages)
    if event is None:
        return frozenset()
    needs = event.get("evidence_needs")
    if not isinstance(needs, Sequence) or isinstance(needs, str):
        return frozenset()
    return frozenset(
        tool
        for need in needs
        if isinstance(need, str)
        for tool in _TOOLS_BY_EVIDENCE_NEED.get(need, frozenset())
    )


def _action_taken(messages: Sequence[AnyMessage]) -> bool:
    return bool(_meet_tools_used(messages) & _GATED_MEET_TOOLS)


def _gated_tool_allowed(
    tool_name: str | None, messages: Sequence[AnyMessage], kind: str | None
) -> bool:
    if _action_taken(messages):
        return False
    if kind == "chat":
        if _turn_planned(messages):
            return tool_name == _chat_action_tool(messages)
        return tool_name != MEET_SEND_CHAT
    if kind == "speech":
        return tool_name == _stamped_action_tool(messages)
    return False


def _planned_tool_allowed(
    tool_name: str | None, messages: Sequence[AnyMessage], kind: str | None
) -> bool:
    if kind == "speech":
        if _stamped_action_tool(messages) is not None:
            return False
        if not _turn_planned(messages):
            return True
        return tool_name in _evidence_tools(messages)
    if kind == "chat":
        if _turn_planned(messages):
            if _chat_action_tool(messages) is not None:
                return False
            return tool_name in _evidence_tools(messages)
        return tool_name != MEET_CHECK_ACTION and not _action_taken(messages)
    if kind == "attention":
        return tool_name != MEET_CHECK_ACTION
    return kind is None


def _tool_allowed(tool_name: str | None, messages: Sequence[AnyMessage]) -> bool:
    kind = _trigger_kind(messages)
    if tool_name in _GATED_MEET_TOOLS:
        return _gated_tool_allowed(tool_name, messages, kind)
    if tool_name in _PLANNED_TOOLS:
        return _planned_tool_allowed(tool_name, messages, kind)
    return True


def _chat_allowed(messages: Sequence[AnyMessage]) -> bool:
    return _tool_allowed(MEET_SEND_CHAT, messages)


def _first_gated_call_id(messages: Sequence[AnyMessage], call_id: str) -> str | None:
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        gated = [
            str(call.get("id") or "")
            for call in message.tool_calls
            if call.get("name") in _GATED_MEET_TOOLS
        ]
        if call_id in gated:
            return gated[0]
    return None


class ChannelToolsMiddleware(AgentMiddleware[Any, Any, Any]):
    def _without_blocked(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        tools = [
            tool for tool in request.tools if _tool_allowed(_tool_name(tool), request.messages)
        ]
        if len(tools) == len(request.tools):
            return request
        return request.override(tools=tools)

    def _refused(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call.get("name")
        if name not in _GATED_MEET_TOOLS and name not in _PLANNED_TOOLS:
            return None
        messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
        call_id = str(request.tool_call.get("id") or "")
        first_gated = _first_gated_call_id(messages, call_id)
        if name in _GATED_MEET_TOOLS and first_gated is not None and first_gated != call_id:
            return ToolMessage(
                content=render_tool_result(
                    MeetToolResult(
                        "duplicate",
                        "Only the first meeting action in this turn can run.",
                    )
                ),
                tool_call_id=call_id,
                name=str(name),
            )
        if _tool_allowed(str(name), messages):
            return None
        message = (
            "That meeting action is not available in this turn phase. "
            "Answer by voice or stay silent."
            if name in _GATED_MEET_TOOLS
            else "That evidence is not available in this turn phase. "
            "Answer with what you already have or stay silent."
        )
        return ToolMessage(
            content=render_tool_result(MeetToolResult("refused", message)),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=str(name),
        )

    def _apply_stamped_reaction(self, request: ToolCallRequest) -> None:
        if request.tool_call.get("name") != "meet_react":
            return
        messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
        event = _trigger_event(messages)
        emoji = event.get("reaction_emoji") if event is not None else None
        if not isinstance(emoji, str):
            return
        args = request.tool_call.get("args")
        request.tool_call["args"] = {**(args if isinstance(args, dict) else {}), "emoji": emoji}

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._without_blocked(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._without_blocked(request))

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        refused = self._refused(request)
        if refused is not None:
            return refused
        self._apply_stamped_reaction(request)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        refused = self._refused(request)
        if refused is not None:
            return refused
        self._apply_stamped_reaction(request)
        return await handler(request)


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


def _result_code(message: ToolMessage) -> str | None:
    try:
        parsed = json.loads(message.text)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, str):
            return code
    return None


_UNPRODUCTIVE_CODES = frozenset(
    {
        "not_meeting_content",
        "frame_unavailable",
        "camera_unavailable",
        "unknown_participant",
        "unknown_screen",
        "budget_exhausted",
        "look_failed",
        "refused",
        "duplicate",
        "failed",
    }
)


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

    def _evidence_trigger(self, message: ToolMessage) -> str | None:
        if message.additional_kwargs.get("synthetic_view"):
            return None
        if message.additional_kwargs.get("memory_prefetch"):
            return None
        if message.name not in self._evidence_tools:
            return None
        if _result_code(message) in _UNPRODUCTIVE_CODES:
            return None
        return message.name or "evidence_tool"

    def _strong_trigger(self, request: ModelRequest[Any]) -> str | None:
        messages = request.messages
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                break
            found = self._evidence_trigger(message)
            if found is not None:
                return found
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
