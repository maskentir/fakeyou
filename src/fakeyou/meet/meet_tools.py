import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from langchain_core.tools import BaseTool, InjectedToolCallId, tool

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.meet_action_ledger import LedgerResult, MeetActionKind
from fakeyou.observability.journal import Journal

MEET_LEAVE_SCHEDULED = (
    "Leave is scheduled for the end of this turn. Say one brief farewell now, "
    "or stay silent, and do not call more tools."
)
MEET_CHECK_ACTION = "meet_check_action"
MEET_SEND_CHAT = "meet_send_chat"

MeetToolCode = Literal["queued", "succeeded", "refused", "failed", "duplicate", "rate_limited"]

_MEET_REACTIONS = (
    "\U0001f496 \U0001f44d \U0001f44e \U0001f389 \U0001f44f"
    " \U0001f602 \U0001f62e \U0001f622 \U0001f914"
)

_EMOJI_BASE = r"[\U0001f300-\U0001faff\u2600-\u27bf\u2b00-\u2bff]"
_EMOJI_CLUSTER = re.compile(
    _EMOJI_BASE + r"(?:\ufe0f|[\U0001f3fb-\U0001f3ff]|\u200d" + _EMOJI_BASE + r"\ufe0f?)*"
)


def _single_emoji(text: str) -> str | None:
    match = _EMOJI_CLUSTER.search(text)
    return match.group(0) if match else None


@dataclass(frozen=True, slots=True)
class MeetToolResult:
    code: MeetToolCode
    message: str
    action_id: str | None = None
    state: str | None = None


def render_tool_result(result: MeetToolResult) -> str:
    payload: dict[str, str] = {"code": result.code, "message": result.message}
    if result.action_id is not None:
        payload["action_id"] = result.action_id
    if result.state is not None:
        payload["state"] = result.state
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class MeetActionDispatcher(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def closed(self) -> bool: ...

    def request(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> str: ...

    async def settle(
        self, kind: MeetActionKind, *, action_id: str, text: str = ""
    ) -> LedgerResult | None: ...

    def schedule_leave(self, *, action_id: str) -> str: ...

    def status(self, action_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class MeetActionRequest:
    kind: MeetActionKind
    action_id: str
    text: str = ""


def _error_result(error: FakeYouError, action_id: str) -> str:
    code: MeetToolCode = (
        "rate_limited" if error.code is ErrorCode.MEET_CHAT_RATE_LIMITED else "refused"
    )
    return render_tool_result(MeetToolResult(code, str(error), action_id=action_id))


def _settled_result(action_id: str, outcome: LedgerResult | None) -> str:
    if outcome is None:
        return render_tool_result(
            MeetToolResult(
                "failed",
                "The meeting action did not settle; do not claim it happened.",
                action_id=action_id,
                state="unknown",
            )
        )
    if outcome.succeeded:
        return render_tool_result(
            MeetToolResult(
                "succeeded",
                outcome.detail or "The meeting action is confirmed.",
                action_id=action_id,
                state="observed_done",
            )
        )
    if outcome.refused:
        return render_tool_result(
            MeetToolResult("refused", outcome.detail, action_id=action_id, state="refused")
        )
    return render_tool_result(
        MeetToolResult("failed", outcome.detail, action_id=action_id, state="failed")
    )


async def _settle_meet_action(
    dispatcher: MeetActionDispatcher,
    refuse: Callable[..., str],
    journal: Journal | None,
    tool_name: str,
    request: MeetActionRequest,
) -> str:
    if not dispatcher.enabled or dispatcher.closed:
        return render_tool_result(
            MeetToolResult(
                "refused", refuse(tool_name, journal=journal), action_id=request.action_id
            )
        )
    try:
        outcome = await dispatcher.settle(
            request.kind, action_id=request.action_id, text=request.text
        )
    except FakeYouError as error:
        return _error_result(error, request.action_id)
    return _settled_result(request.action_id, outcome)


def _schedule_leave_action(
    dispatcher: MeetActionDispatcher,
    refuse: Callable[..., str],
    journal: Journal | None,
    action_id: str,
) -> str:
    if not dispatcher.enabled or dispatcher.closed:
        return render_tool_result(
            MeetToolResult("refused", refuse("meet_leave", journal=journal), action_id=action_id)
        )
    try:
        dispatcher.schedule_leave(action_id=action_id)
    except FakeYouError as error:
        return _error_result(error, action_id)
    return render_tool_result(
        MeetToolResult("queued", MEET_LEAVE_SCHEDULED, action_id=action_id, state="scheduled")
    )


def create_meet_action_tools(
    dispatcher: MeetActionDispatcher,
    *,
    refuse: Callable[..., str],
    journal: Journal | None = None,
) -> list[BaseTool]:
    @tool(description="Raise your hand in the meeting to show you want to speak.")
    async def meet_raise_hand(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return await _settle_meet_action(
            dispatcher,
            refuse,
            journal,
            "meet_raise_hand",
            MeetActionRequest("raise_hand", tool_call_id),
        )

    @tool(description="Lower your hand in the meeting.")
    async def meet_lower_hand(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return await _settle_meet_action(
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
        return await _settle_meet_action(
            dispatcher,
            refuse,
            journal,
            "meet_send_chat",
            MeetActionRequest("send_chat", tool_call_id, message),
        )

    @tool(
        description=(
            "Send one emoji reaction in the meeting. Pass exactly one of Meet's "
            "reaction glyphs: \U0001f496 heart, \U0001f44d thumbs up, \U0001f44e thumbs down, "
            "\U0001f389 party, \U0001f44f clap, \U0001f602 laugh, \U0001f62e surprised, "
            "\U0001f622 sad, \U0001f914 thinking or confused. Pass the glyph itself, "
            "not its name. Use it to acknowledge without speaking; never send "
            "reaction streaks."
        )
    )
    async def meet_react(emoji: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        cleaned = _single_emoji(emoji)
        if cleaned is None:
            return render_tool_result(
                MeetToolResult(
                    "refused",
                    f"No emoji glyph in {emoji.strip()[:40]!r}. Pass one glyph "
                    f"itself, not its name: {_MEET_REACTIONS}.",
                )
            )
        return await _settle_meet_action(
            dispatcher,
            refuse,
            journal,
            "meet_react",
            MeetActionRequest("react", tool_call_id, cleaned),
        )

    @tool(description="Mute your own microphone in the meeting.")
    async def meet_mute(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return await _settle_meet_action(
            dispatcher, refuse, journal, "meet_mute", MeetActionRequest("mute", tool_call_id)
        )

    @tool(description="Unmute your own microphone in the meeting.")
    async def meet_unmute(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return await _settle_meet_action(
            dispatcher, refuse, journal, "meet_unmute", MeetActionRequest("unmute", tool_call_id)
        )

    @tool(
        description=(
            "Leave the meeting call. This ends your presence and cannot be undone; "
            "use it only when the meeting is over or you are told to leave."
        )
    )
    async def meet_leave(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return _schedule_leave_action(dispatcher, refuse, journal, tool_call_id)

    @tool(
        MEET_CHECK_ACTION,
        description=(
            "Check the delivery state of one of your earlier meeting actions by its id. "
            "States: requested, acted, observed_done, refused, failed, unknown."
        ),
    )
    async def meet_check_action(action_id: str) -> str:
        return json.dumps(
            {"action_id": action_id, "state": dispatcher.status(action_id)},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return [
        meet_raise_hand,
        meet_lower_hand,
        meet_send_chat,
        meet_react,
        meet_mute,
        meet_unmute,
        meet_leave,
        meet_check_action,
    ]
