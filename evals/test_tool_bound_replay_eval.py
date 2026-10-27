from langchain_core.messages import AIMessage

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import MeetingResponder
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.middleware import ChannelToolsMiddleware
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.meet.meet_action_ledger import LedgerResult, MeetActionKind
from fakeyou.meet.meet_tools import create_meet_action_tools
from tests._conversation_fakes import ToolCallingFakeModel
from tests._meet_support import tool_call


class _Dispatcher:
    enabled = True
    closed = False

    def __init__(self) -> None:
        self.requests: list[tuple[MeetActionKind, str, str]] = []

    def request(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> str:
        self.requests.append((kind, action_id, text))
        return action_id

    async def settle(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> LedgerResult:
        self.request(kind, action_id=action_id, text=text)
        return LedgerResult(action_id=action_id, outcome="succeeded", detail="ok", attempts=1)

    def schedule_leave(self, *, action_id: str) -> str:
        return self.request("leave", action_id=action_id)

    def status(self, action_id: str) -> str:
        known = {request_id for _, request_id, _ in self.requests}
        return "observed_done" if action_id in known else "unknown"


def _event(event_id: str, text: str, *, requests_chat: bool = False) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-26T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="p1",
        speaker_evidence_id=None,
        requests_chat=requests_chat,
    )


def _send(call_id: str, message: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[tool_call("meet_send_chat", call_id, {"message": message})],
    )


async def test_a_voice_turn_speaks_without_mirroring_into_chat() -> None:
    dispatcher = _Dispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(
        responses=[_send("c1", "All good here."), AIMessage(content="All good here.")]
    )
    agent = create_meeting_agent(model, tools=tools, middleware=[ChannelToolsMiddleware()])
    responder = MeetingResponder(agent, NoHistory())

    reply = await responder.respond([_event("t1", "Harun, how are you?")], lambda: None)

    assert reply.text == "All good here."
    assert dispatcher.requests == []


async def test_an_explicit_chat_request_sends_exactly_once() -> None:
    dispatcher = _Dispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(
        responses=[
            _send("c1", "Hello from chat."),
            _send("c2", "Hello from chat."),
            AIMessage(content="Sent in chat."),
        ]
    )
    agent = create_meeting_agent(model, tools=tools, middleware=[ChannelToolsMiddleware()])
    responder = MeetingResponder(agent, NoHistory())
    event = _event("t2", "Harun, send a hello in chat please", requests_chat=True)

    reply = await responder.respond([event], lambda: None)

    assert reply.text == "Sent in chat."
    assert dispatcher.requests == [("send_chat", "c1", "Hello from chat.")]
