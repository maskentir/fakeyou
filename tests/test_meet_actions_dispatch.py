import ast
import asyncio
import json
import time
from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.session import SessionConfig
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet_action_ledger import LedgerError, MeetActionLedger, actions_path
from fakeyou.meet.meet_actions import lower_hand, raise_hand, send_chat
from fakeyou.memory.memory import (
    MEET_ACTION_QUEUED,
    PREFETCH_EVIDENCE_PREFIX,
    create_meet_action_tools,
)
from fakeyou.memory.persona import (
    MEET_REFUSAL_TEXT,
    PersonaProfile,
    meeting_tool_names,
    refuse_meet_tool,
)
from fakeyou.observability.faults import FaultReporter
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import IN_CALL, LOBBY, meet_page
from tests._meet_support import (
    CHAT_BUTTON_XPATH,
    CHAT_INPUT_XPATH,
    CHAT_TEXT,
    ECHO_CONTENT,
    FIXTURE_HTML,
    FIXTURE_URL,
    LOWER_XPATH,
    RAISE_XPATH,
    task5_dispatcher,
    tool_call,
    tool_text,
    with_enter,
)
from tests._persistence_fakes import RecordingJournal


async def test_send_chat_tool_refuses_invalid_messages(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-chat")
    dispatcher, _record = task5_dispatcher(tmp_path, "meeting-chat", journal=journal)
    tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)

    long = await tools[2].ainvoke(
        tool_call("meet_send_chat", "call-tool-1", {"message": "y" * 501})
    )
    evidence = await tools[2].ainvoke(
        tool_call("meet_send_chat", "call-tool-2", {"message": PREFETCH_EVIDENCE_PREFIX + "{}"})
    )

    assert MEET_ACTION_QUEUED not in tool_text(long)
    assert "500" in tool_text(long)
    assert MEET_ACTION_QUEUED not in tool_text(evidence)
    assert "evidence" in tool_text(evidence)
    assert long.status == "success"
    assert evidence.status == "success"


@pytest.mark.parametrize("action", ["raise", "lower", "chat"])
async def test_actions_refuse_fast_when_not_admitted(action: str) -> None:
    page = meet_page([LOBBY])
    clock = FakeClock()
    with_enter(page)

    if action == "raise":
        result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)
    elif action == "lower":
        result = await lower_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)
    else:
        result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.refused
    assert "lobby controls are visible" in result.detail
    assert page.clicked == []
    assert page.typed == []
    assert clock.sleeps == []


async def test_flag_off_by_default_and_bound_tools_refuse_without_execution(
    tmp_path: Path,
) -> None:
    assert DEFAULTS.conversation.meet_actions_enabled is False
    config = SessionConfig(
        owner_id=SessionId("owner-a"),
        twin_id=SessionId("twin-a"),
        meeting_id=SessionId("meeting-flag"),
        link=LINK,
        twin_name="Rani Twin",
    )
    assert config.meet_actions_enabled is False
    journal = RecordingJournal("meeting-flag")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-flag", enabled=False, journal=journal)
    page = meet_page([IN_CALL])
    clock = FakeClock()
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)

    assert [tool.name for tool in tools] == [
        "meet_raise_hand",
        "meet_lower_hand",
        "meet_send_chat",
    ]
    raised = await tools[0].ainvoke(tool_call("meet_raise_hand", "call-flag-1"))
    sent = await tools[2].ainvoke(
        tool_call("meet_send_chat", "call-flag-2", {"message": CHAT_TEXT})
    )

    assert tool_text(raised) == MEET_REFUSAL_TEXT
    assert tool_text(sent) == MEET_REFUSAL_TEXT
    await dispatcher.drain()
    assert page.clicked == []
    assert page.typed == []
    assert actions_path(record).exists() is False
    assert journal.events("meet_action_requested") == []
    assert journal.events("meet_action_started") == []
    rejected = journal.events("tool_rejected")
    assert [(row["tool"], row["reason"]) for row in rejected] == [
        ("meet_raise_hand", "authority_denied"),
        ("meet_send_chat", "authority_denied"),
    ]


async def test_stale_generation_and_closed_requests_cancel_without_dom_touch(
    tmp_path: Path,
) -> None:
    journal = RecordingJournal("meeting-stale")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-stale", journal=journal)
    dispatcher.request("raise_hand", action_id="call-stale-1")
    dispatcher.close()
    await dispatcher.drain()
    page = meet_page([])
    clock = FakeClock()
    live, live_record = task5_dispatcher(tmp_path, "meeting-stale-live", journal=journal)
    live.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    live.request("send_chat", action_id="call-stale-2", text=CHAT_TEXT)
    live.cancel_pending()
    await live.drain()

    refused = journal.events("meet_action_rejected")

    assert [(row["action"], row["action_id"], row["reason"]) for row in refused] == [
        ("raise_hand", "call-stale-1", "stale_cancelled"),
        ("send_chat", "call-stale-2", "stale_cancelled"),
    ]
    assert page.clicked == []
    assert page.typed == []
    assert page.waited == []
    assert page.evaluate_calls == []
    assert actions_path(record).exists() is False
    assert actions_path(live_record).exists() is False
    closed_tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)
    closed = await closed_tools[0].ainvoke(tool_call("meet_raise_hand", "call-stale-3"))
    await dispatcher.drain()

    assert tool_text(closed) == MEET_REFUSAL_TEXT
    assert journal.events("meet_action_requested") == []


async def test_meet_tool_call_schedules_async_without_sync_side_effects(
    tmp_path: Path,
) -> None:
    journal = RecordingJournal("meeting-async")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-async", journal=journal)
    tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)

    queued = await tools[2].ainvoke(
        tool_call("meet_send_chat", "call-async-1", {"message": CHAT_TEXT})
    )

    assert tool_text(queued) == MEET_ACTION_QUEUED
    assert journal.events() == []
    assert actions_path(record).exists() is False
    page = meet_page([IN_CALL, ECHO_CONTENT], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    pressed = with_enter(page)
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    await dispatcher.drain()

    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]
    finished = journal.events("meet_action_finished")

    assert len(finished) == 1
    assert finished[0]["observed"] is True
    assert finished[0]["action_id"] == "call-async-1"


async def test_stop_cancels_pending_actions_without_stale_effects(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-stop")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-stop", journal=journal)
    page = meet_page([])
    clock = FakeClock()
    stop = asyncio.Event()
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=stop)
    dispatcher.request("raise_hand", action_id="call-stop-1")
    stop.set()
    dispatcher.close()
    await dispatcher.drain()
    await dispatcher.drain()

    refused = journal.events("meet_action_rejected")

    assert [(row["action_id"], row["reason"]) for row in refused] == [
        ("call-stop-1", "stale_cancelled")
    ]
    assert journal.events("meet_action_finished") == []
    assert journal.events("meet_action_failed") == []
    assert page.clicked == []
    assert page.typed == []
    assert actions_path(record).exists() is False


async def test_stop_aborts_midflight_action_without_terminal_claim(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-midflight")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-midflight", journal=journal)
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False})
    clock = FakeClock()
    entered = asyncio.Event()
    released = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        del seconds
        entered.set()
        await released.wait()

    stop = asyncio.Event()
    dispatcher.bind_page(page, sleep=gated_sleep, monotonic=clock.monotonic, stop=stop)
    dispatcher.request("raise_hand", action_id="call-midflight-1")
    draining = asyncio.create_task(dispatcher.drain())
    await asyncio.wait_for(entered.wait(), timeout=5)
    stop.set()
    dispatcher.close()
    released.set()
    await draining

    assert page.clicked == []
    assert journal.events("meet_action_finished") == []
    refused = journal.events("meet_action_rejected")

    assert [(row["action_id"], row["reason"]) for row in refused] == [
        ("call-midflight-1", "stale_cancelled")
    ]
    ledger = MeetActionLedger.load(record, "meeting-midflight")
    intent = ledger.get("call-midflight-1")

    assert intent is not None
    assert intent.state == "requested"
    assert intent.observed_before is False


async def test_stop_without_close_marks_action_stale(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-stop-bare")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-stop-bare", journal=journal)
    page = meet_page([])
    clock = FakeClock()
    stop = asyncio.Event()
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=stop)
    stop.set()
    dispatcher.request("raise_hand", action_id="call-stop-bare-1")
    await dispatcher.drain()

    refused = journal.events("meet_action_rejected")

    assert [(row["action_id"], row["reason"]) for row in refused] == [
        ("call-stop-bare-1", "stale_cancelled")
    ]
    assert page.clicked == []
    assert actions_path(record).exists() is False


async def test_bound_dispatcher_stays_idle_without_tool_calls(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-idle")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-idle", journal=journal)
    page = meet_page([IN_CALL])
    clock = FakeClock()
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    await asyncio.sleep(0.05)
    await dispatcher.drain()
    dispatcher.close()
    await dispatcher.drain()

    assert page.clicked == []
    assert page.typed == []
    assert page.waited == []
    assert page.evaluate_calls == []
    assert actions_path(record).exists() is False
    assert journal.events() == []


def test_meet_dispatcher_defines_no_timers_or_triggers() -> None:
    source = (
        Path(__file__).resolve().parent.parent / "src" / "fakeyou" / "orchestration" / "runtime.py"
    ).read_text(encoding="utf-8")
    [node] = [
        entry
        for entry in ast.walk(ast.parse(source))
        if isinstance(entry, ast.ClassDef) and entry.name == "MeetActionDispatcher"
    ]
    seen: set[str] = set()
    for entry in ast.walk(node):
        if isinstance(entry, ast.While):
            seen.add("while")
        if isinstance(entry, ast.Call) and isinstance(entry.func, ast.Attribute):
            seen.add(entry.func.attr)

    assert seen & {"while", "sleep", "call_later", "call_at", "Timer"} == set()


async def test_meet_raise_hand_end_to_end_observed_success(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-e2e-raise")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-e2e-raise", journal=journal)
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False})
    clock = FakeClock()
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)

    queued = await tools[0].ainvoke(tool_call("meet_raise_hand", "call-e2e-raise"))

    assert tool_text(queued) == MEET_ACTION_QUEUED
    await dispatcher.drain()
    dispatcher.close()

    assert page.clicked == [RAISE_XPATH]
    ledger = MeetActionLedger.load(record, "meeting-e2e-raise")
    intent = ledger.get("call-e2e-raise")

    assert intent is not None
    assert intent.state == "observed_done"
    assert intent.observed_before is False
    assert intent.observed_after is True
    assert intent.attempt == 1
    assert [row["event"] for row in journal.events()] == [
        "meet_action_requested",
        "meet_action_started",
        "meet_action_observed",
        "meet_action_finished",
    ]
    assert {row["action_id"] for row in journal.events()} == {"call-e2e-raise"}
    [finished] = journal.events("meet_action_finished")

    assert finished["observed"] is True


async def test_meet_chat_end_to_end_observed_success_without_content_leak(
    tmp_path: Path,
) -> None:
    journal = RecordingJournal("meeting-e2e-chat")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-e2e-chat", journal=journal)
    page = meet_page([IN_CALL, ECHO_CONTENT], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    pressed = with_enter(page)
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)

    queued = await tools[2].ainvoke(
        tool_call("meet_send_chat", "call-e2e-chat", {"message": CHAT_TEXT})
    )

    assert tool_text(queued) == MEET_ACTION_QUEUED
    await dispatcher.drain()
    dispatcher.close()

    assert page.clicked == [CHAT_BUTTON_XPATH, CHAT_INPUT_XPATH]
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]
    ledger = MeetActionLedger.load(record, "meeting-e2e-chat")
    intent = ledger.get("call-e2e-chat")

    assert intent is not None
    assert intent.state == "observed_done"
    assert intent.chars == len(CHAT_TEXT)
    assert CHAT_TEXT not in actions_path(record).read_text(encoding="utf-8")
    assert CHAT_TEXT not in json.dumps(journal.lines)
    [finished] = journal.events("meet_action_finished")

    assert finished["observed"] is True


async def test_unauthorized_persona_end_to_end_refuses_without_fault_or_effects(
    tmp_path: Path,
) -> None:
    journal = RecordingJournal("meeting-e2e-denied")
    faults = FaultReporter(journal=journal)
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-e2e-denied", journal=journal)
    page = meet_page([])
    clock = FakeClock()
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    denied = PersonaProfile(
        owner_id=SessionId("owner-a"),
        twin_id=SessionId("twin-a"),
        display_name="Rani Twin",
        role="Product lead",
        allowed_tools=frozenset(),
    )
    granted = PersonaProfile(
        owner_id=SessionId("owner-a"),
        twin_id=SessionId("twin-a"),
        display_name="Rani Twin",
        role="Product lead",
        allowed_tools=frozenset({"meet_send_chat"}),
    )

    assert meeting_tool_names(denied, None, memory_requested=False) == frozenset()
    assert meeting_tool_names(granted, None, memory_requested=False) == frozenset()
    tools = [
        tool
        for tool in create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)
        if tool.name in meeting_tool_names(denied, None, memory_requested=False)
    ]

    assert tools == []
    refused = refuse_meet_tool("meet_send_chat", journal=journal)

    assert refused == MEET_REFUSAL_TEXT
    await dispatcher.drain()
    dispatcher.close()

    assert faults.total() == 0
    assert faults.report() is None
    assert page.clicked == []
    assert page.typed == []
    assert actions_path(record).exists() is False
    assert journal.events("meet_action_requested") == []


@pytest.mark.browser
async def test_meet_actions_live_fixture_through_dispatcher(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-e2e-live")
    dispatcher, record = task5_dispatcher(tmp_path, "meeting-e2e-live", journal=journal)
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.route(
            FIXTURE_URL,
            lambda route: route.fulfill(body=FIXTURE_HTML, content_type="text/html"),
        )
        await real_page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        dispatcher.bind_page(
            real_page, sleep=asyncio.sleep, monotonic=time.monotonic, stop=asyncio.Event()
        )
        tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)
        await tools[0].ainvoke(tool_call("meet_raise_hand", "call-live-raise"))
        await tools[1].ainvoke(tool_call("meet_lower_hand", "call-live-lower"))
        await tools[2].ainvoke(
            tool_call("meet_send_chat", "call-live-chat", {"message": "live fixture ping"})
        )
        await dispatcher.drain()
        dispatcher.close()
    ledger = MeetActionLedger.load(record, "meeting-e2e-live")
    for call_id in ("call-live-raise", "call-live-lower", "call-live-chat"):
        intent = ledger.get(call_id)

        assert intent is not None
        assert intent.state == "observed_done"


async def test_dispatcher_records_ledger_error_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = RecordingJournal("meeting-ledger")
    dispatcher, _record = task5_dispatcher(tmp_path, "meeting-ledger", journal=journal)
    clock = FakeClock()
    dispatcher.bind_page(
        meet_page([IN_CALL]),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        stop=asyncio.Event(),
    )

    def boom_load(record_path: Path, meeting_id: str) -> MeetActionLedger:
        del meeting_id
        raise LedgerError(ErrorCode.ACTION_LEDGER_UNREADABLE, path=record_path)

    monkeypatch.setattr(MeetActionLedger, "load", boom_load)
    dispatcher.request("send_chat", action_id="call-ledger-1", text="hi")
    await dispatcher.drain()

    (failed,) = journal.events("meet_action_failed")
    assert failed["reason"] == "LedgerError"


async def test_dispatcher_journals_unexpected_ledger_error_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = RecordingJournal("meeting-ledger")
    dispatcher, _record = task5_dispatcher(tmp_path, "meeting-ledger", journal=journal)
    clock = FakeClock()
    dispatcher.bind_page(
        meet_page([IN_CALL]),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        stop=asyncio.Event(),
    )

    def boom_load(record_path: Path, meeting_id: str) -> MeetActionLedger:
        del meeting_id
        del record_path
        raise ValueError("bug")

    monkeypatch.setattr(MeetActionLedger, "load", boom_load)
    dispatcher.request("send_chat", action_id="call-task-1", text="hi")
    await dispatcher.drain()

    (failed,) = journal.events("meet_action_failed")
    assert failed["reason"] == "ValueError"
