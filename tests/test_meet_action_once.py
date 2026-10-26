from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.middleware import ChannelToolsMiddleware
from fakeyou.meet.meet_action_ledger import LedgerResult, MeetActionKind
from fakeyou.meet.meet_tools import create_meet_action_tools
from tests._conversation_fakes import ToolCallingFakeModel
from tests._meet_support import tool_call


class _CountingDispatcher:
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


def _send(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "meet_send_chat",
                "args": {"message": "Hello from chat"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


async def test_a_second_chat_send_in_the_same_turn_never_reaches_the_dispatcher() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(
        responses=[_send("c1"), _send("c2"), AIMessage(content="I sent it in chat.")]
    )
    agent = create_meeting_agent(model, tools=tools)

    result = await agent.ainvoke({"messages": [HumanMessage("Harun, send a hello in chat please")]})

    assert dispatcher.requests == [("send_chat", "c1", "Hello from chat")]
    assert result["messages"][-1].text


async def test_a_plain_spoken_turn_cannot_send_chat_at_all() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(
        responses=[_send("c1"), AIMessage(content="Hello, all good here.")]
    )
    agent = create_meeting_agent(model, tools=tools, middleware=[ChannelToolsMiddleware()])
    spoken = HumanMessage(
        content="Harun, how are you?",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": "speech"}},
    )

    result = await agent.ainvoke({"messages": [spoken]})

    assert dispatcher.requests == []
    assert result["messages"][-1].text


_LOOKS: list[str] = []


@tool(description="Look at the shared screen.")
def look_at_screen(question: str) -> str:
    _LOOKS.append(question)
    return '{"code":"ok","answer":"A feed page"}'


def _look(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "look_at_screen",
                "args": {"question": "what is shown?"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


async def test_a_second_look_in_the_same_turn_is_blocked() -> None:
    _LOOKS.clear()
    model = ToolCallingFakeModel(
        responses=[_look("l1"), _look("l2"), AIMessage(content="It is a feed page.")]
    )
    agent = create_meeting_agent(model, tools=[look_at_screen])
    spoken = HumanMessage(
        content="Harun, what is on my screen?",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": "speech"}},
    )

    result = await agent.ainvoke({"messages": [spoken]})

    assert _LOOKS == ["what is shown?"]
    assert result["messages"][-1].text


async def test_react_mute_and_leave_reach_the_dispatcher_once_each() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    by_name = {tool.name: tool for tool in tools}

    await by_name["meet_react"].ainvoke(tool_call("meet_react", "r1", {"emoji": "\U0001f44d"}))
    await by_name["meet_mute"].ainvoke(tool_call("meet_mute", "m1"))
    await by_name["meet_leave"].ainvoke(tool_call("meet_leave", "l1"))

    assert dispatcher.requests == [
        ("react", "r1", "\U0001f44d"),
        ("mute", "m1", ""),
        ("leave", "l1", ""),
    ]


async def test_react_rejects_words_instead_of_an_emoji() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    by_name = {tool.name: tool for tool in tools}

    result = await by_name["meet_react"].ainvoke(
        tool_call("meet_react", "r9", {"emoji": "thinking face"})
    )

    assert "refused" in str(result)
    assert "\U0001f914" in str(result)
    assert dispatcher.requests == []


async def test_react_extracts_one_glyph_from_a_named_emoji() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    by_name = {tool.name: tool for tool in tools}

    await by_name["meet_react"].ainvoke(
        tool_call("meet_react", "r2", {"emoji": "confused \U0001f914"})
    )
    await by_name["meet_react"].ainvoke(tool_call("meet_react", "r3", {"emoji": "\u2764\ufe0f"}))

    assert dispatcher.requests == [
        ("react", "r2", "\U0001f914"),
        ("react", "r3", "\u2764\ufe0f"),
    ]


def _react(call_id: str, emoji: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[tool_call("meet_react", call_id, {"emoji": emoji})],
    )


async def test_the_judge_stamped_reaction_replaces_the_model_glyph() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(responses=[_react("r1", "\U0001f44d"), AIMessage(content="Done.")])
    agent = create_meeting_agent(model, tools=tools, middleware=[ChannelToolsMiddleware()])
    asked = HumanMessage(
        content="Harun, give me a confused reaction",
        id="m1",
        additional_kwargs={
            "meeting_event": {
                "kind": "speech",
                "meet_action": "react",
                "reaction_emoji": "\U0001f914",
            }
        },
    )

    await agent.ainvoke({"messages": [asked]})

    assert dispatcher.requests == [("react", "r1", "\U0001f914")]


async def test_only_the_first_parallel_meet_action_runs_on_a_chat_turn() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    both = AIMessage(
        content="",
        tool_calls=[
            tool_call("meet_react", "r1", {"emoji": "\U0001f914"}),
            tool_call("meet_leave", "l1"),
        ],
    )
    model = ToolCallingFakeModel(responses=[both, AIMessage(content="Done.")])
    agent = create_meeting_agent(model, tools=tools, middleware=[ChannelToolsMiddleware()])
    asked = HumanMessage(
        content="Harun, react and leave",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": "chat"}},
    )

    await agent.ainvoke({"messages": [asked]})

    assert dispatcher.requests == [("react", "r1", "\U0001f914")]


async def test_check_action_reports_the_ledger_state() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    by_name = {tool.name: tool for tool in tools}
    await by_name["meet_mute"].ainvoke(tool_call("meet_mute", "m7"))

    known = await by_name["meet_check_action"].ainvoke(
        tool_call("meet_check_action", "c1", {"action_id": "m7"})
    )
    unknown = await by_name["meet_check_action"].ainvoke(
        tool_call("meet_check_action", "c2", {"action_id": "nope"})
    )

    assert '"state":"observed_done"' in str(known)
    assert '"state":"unknown"' in str(unknown)


async def test_a_stamped_chat_turn_cannot_spend_its_phase_on_evidence() -> None:
    _LOOKS.clear()
    dispatcher = _CountingDispatcher()
    meet_tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(
        responses=[
            _look("l1"),
            _look("l2"),
            _send("c9"),
            AIMessage(content="It is a feed page."),
        ]
    )
    agent = create_meeting_agent(
        model, tools=[look_at_screen, *meet_tools], middleware=[ChannelToolsMiddleware()]
    )
    asked = HumanMessage(
        content="Harun, send a hello in chat please",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": "speech", "requests_chat": True}},
    )

    result = await agent.ainvoke({"messages": [asked]})

    assert _LOOKS == []
    assert dispatcher.requests == [("send_chat", "c9", "Hello from chat")]
    assert result["messages"][-1].text


def _leave(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "meet_leave", "args": {}, "id": call_id, "type": "tool_call"}],
    )


async def test_evidence_tools_cannot_starve_a_stamped_leave() -> None:
    _LOOKS.clear()
    dispatcher = _CountingDispatcher()
    meet_tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(
        responses=[
            _look("l1"),
            _look("l2"),
            _leave("x1"),
            AIMessage(content="Goodbye."),
        ]
    )
    agent = create_meeting_agent(
        model, tools=[look_at_screen, *meet_tools], middleware=[ChannelToolsMiddleware()]
    )
    asked = HumanMessage(
        content="Harun, you can leave now",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": "speech", "meet_action": "leave"}},
    )

    result = await agent.ainvoke({"messages": [asked]})

    assert dispatcher.requests == [("leave", "x1", "")]
    assert result["messages"][-1].text
