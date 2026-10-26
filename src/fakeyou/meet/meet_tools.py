import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from langchain_core.tools import BaseTool, InjectedToolCallId, tool

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.meet_action_ledger import MeetActionKind
from fakeyou.observability.journal import Journal

MEET_ACTION_QUEUED = (
    "Done. It will be delivered; do not repeat it. Continue with your spoken reply or stay silent."
)
MEET_SEND_CHAT = "meet_send_chat"

MeetToolCode = Literal["queued", "refused", "failed", "duplicate", "rate_limited"]


@dataclass(frozen=True, slots=True)
class MeetToolResult:
    code: MeetToolCode
    message: str


def render_tool_result(result: MeetToolResult) -> str:
    return json.dumps(
        {"code": result.code, "message": result.message},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class MeetActionDispatcher(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def closed(self) -> bool: ...

    def request(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> str: ...


@dataclass(frozen=True, slots=True)
class MeetActionRequest:
    kind: MeetActionKind
    action_id: str
    text: str = ""


def _request_meet_action(
    dispatcher: MeetActionDispatcher,
    refuse: Callable[..., str],
    journal: Journal | None,
    tool_name: str,
    request: MeetActionRequest,
) -> str:
    if not dispatcher.enabled or dispatcher.closed:
        return render_tool_result(MeetToolResult("refused", refuse(tool_name, journal=journal)))
    try:
        dispatcher.request(request.kind, action_id=request.action_id, text=request.text)
    except FakeYouError as error:
        code: MeetToolCode = (
            "rate_limited" if error.code is ErrorCode.MEET_CHAT_RATE_LIMITED else "refused"
        )
        return render_tool_result(MeetToolResult(code, str(error)))
    return render_tool_result(MeetToolResult("queued", MEET_ACTION_QUEUED))


def create_meet_action_tools(
    dispatcher: MeetActionDispatcher,
    *,
    refuse: Callable[..., str],
    journal: Journal | None = None,
) -> list[BaseTool]:
    @tool(description="Raise your hand in the meeting to show you want to speak.")
    async def meet_raise_hand(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return _request_meet_action(
            dispatcher,
            refuse,
            journal,
            "meet_raise_hand",
            MeetActionRequest("raise_hand", tool_call_id),
        )

    @tool(description="Lower your hand in the meeting.")
    async def meet_lower_hand(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return _request_meet_action(
            dispatcher,
            refuse,
            journal,
            "meet_lower_hand",
            MeetActionRequest("lower_hand", tool_call_id),
        )

    @tool(
        MEET_SEND_CHAT,
        description=(
            "Send a short chat message to everyone in the meeting. Use it only when the "
            "conversation is in chat or someone asked you to write in chat; never use it to "
            "answer a spoken question, and never send the same message twice."
        ),
    )
    async def meet_send_chat(message: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return _request_meet_action(
            dispatcher,
            refuse,
            journal,
            "meet_send_chat",
            MeetActionRequest("send_chat", tool_call_id, message),
        )

    return [meet_raise_hand, meet_lower_hand, meet_send_chat]
