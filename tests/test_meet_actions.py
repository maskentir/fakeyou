import json
from pathlib import Path

import pytest

from fakeyou.browser import open_direct_page
from fakeyou.ingestion_status import RecordLockError, record_lock
from fakeyou.meet_action_ledger import (
    ACTIONS_FILE_NAME,
    LedgerError,
    MeetActionLedger,
    action_id_for,
    actions_path,
    execute_meet_action,
    ledger_lock,
    reconcile_ledger,
)
from fakeyou.meet_actions import (
    MeetActionResult,
    MeetActionSignals,
    lower_hand,
    raise_hand,
    send_chat,
)
from fakeyou.provider_faults import ProviderFault
from tests.fakes import IN_CALL, LOBBY, FakeClock, FakePage, RecordingJournal, meet_page


def _button_xpath(prefix: str) -> str:
    return (
        "xpath=//button[starts-with(translate(@aria-label, "
        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{prefix}')]"
    )


RAISE_XPATH = _button_xpath("raise hand")
LOWER_XPATH = _button_xpath("lower hand")
CHAT_BUTTON_XPATH = _button_xpath("chat")
CHAT_INPUT_XPATH = "xpath=//textarea[@maxlength='4000']"
ECHO_ITEM_XPATH = _button_xpath("pin message")

CHAT_TEXT = "twin check-in ping"
ECHO_CONTENT = IN_CALL + f'<div>{CHAT_TEXT}<button aria-label="Pin message">Pin</button></div>'
TEXT_WITHOUT_PIN = IN_CALL + f"<div>{CHAT_TEXT}</div>"

FIXTURE_URL = "https://meet-actions.fakeyou.test/fixture"
FIXTURE_HTML = """<button aria-label="Leave call">Leave</button>
<button id="hand" aria-label="Raise hand (ctrl + alt + h)">Hand</button>
<button id="chatbtn" aria-label="Chat with everyone">Chat</button>
<div id="panel" style="display:none">
<textarea maxlength="4000" id="box" aria-label="Send a message"></textarea>
<div id="messages"></div>
</div>
<script>
const hand = document.getElementById('hand');
hand.addEventListener('click', () => {
  const raised = hand.getAttribute('aria-label').startsWith('Lower');
  const next = raised ? 'Raise hand' : 'Lower hand';
  hand.setAttribute('aria-label', next + ' (ctrl + alt + h)');
});
const panel = document.getElementById('panel');
document.getElementById('chatbtn').addEventListener('click', () => {
  panel.style.display = 'block';
});
const box = document.getElementById('box');
box.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    const item = document.createElement('div');
    item.textContent = event.target.value;
    const pin = document.createElement('button');
    pin.setAttribute('aria-label', 'Pin message');
    pin.textContent = 'Pin';
    item.appendChild(pin);
    document.getElementById('messages').appendChild(item);
  }
});
</script>
"""


def _with_enter(page: FakePage) -> list[str]:
    pressed: list[str] = []

    async def press(key: str) -> None:
        pressed.append(key)

    page.keyboard.press = press
    return pressed


async def test_raise_hand_noop_when_already_raised() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: True})
    clock = FakeClock()

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert "already" in result.detail
    assert page.clicked == []


async def test_raise_hand_clicks_then_succeeds() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False})
    clock = FakeClock()

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [RAISE_XPATH]


async def test_lower_hand_clicks_then_succeeds() -> None:
    page = meet_page([IN_CALL], visible={RAISE_XPATH: False})
    clock = FakeClock()

    result = await lower_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [LOWER_XPATH]


async def test_raise_hand_status_timeout_raises() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False}, wait_failures={LOWER_XPATH})
    clock = FakeClock()

    with pytest.raises(RuntimeError, match="raised"):
        await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert page.clicked == [RAISE_XPATH]


async def test_hand_click_retries_three_times_with_fresh_locator() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False}, click_flaky={RAISE_XPATH: 2})
    clock = FakeClock()
    locate_calls: list[str] = []
    real_locator = page.locator

    def counting(selector: str):
        locate_calls.append(selector)
        return real_locator(selector)

    page.locator = counting

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [RAISE_XPATH, RAISE_XPATH, RAISE_XPATH]
    assert locate_calls.count(RAISE_XPATH) == 3


async def test_send_chat_opens_panel_types_and_succeeds_on_echo() -> None:
    page = meet_page([IN_CALL, ECHO_CONTENT], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    pressed = _with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [CHAT_BUTTON_XPATH, CHAT_INPUT_XPATH]
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]
    assert CHAT_TEXT not in result.detail


async def test_send_chat_fails_without_echo_and_never_resends() -> None:
    page = meet_page([IN_CALL] + [IN_CALL] * 11)
    clock = FakeClock()
    pressed = _with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert "not observed" in result.detail
    assert CHAT_TEXT not in result.detail
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]


async def test_send_chat_fails_when_pin_item_missing() -> None:
    page = meet_page([IN_CALL] + [TEXT_WITHOUT_PIN] * 11, counts={ECHO_ITEM_XPATH: [0] * 11})
    clock = FakeClock()
    _with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert page.typed == [CHAT_TEXT]


@pytest.mark.parametrize("action", ["raise", "lower", "chat"])
async def test_actions_refuse_fast_when_not_admitted(action: str) -> None:
    page = meet_page([LOBBY])
    clock = FakeClock()
    _with_enter(page)

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


async def test_send_chat_rejects_empty_text_without_touching_dom() -> None:
    page = meet_page([])
    clock = FakeClock()

    with pytest.raises(ValueError, match="empty"):
        await send_chat(page, "   ", sleep=clock.sleep, monotonic=clock.monotonic)

    assert page.clicked == []
    assert page.typed == []


async def test_custom_signals_drive_selectors() -> None:
    signals = MeetActionSignals(raise_hand="lever la main", lower_hand="baisser la main")
    page = meet_page([IN_CALL], visible={_button_xpath("baisser la main"): False})
    clock = FakeClock()

    result = await raise_hand(page, signals, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [_button_xpath("lever la main")]


@pytest.mark.browser
async def test_hand_and_chat_actions_against_fixture_page() -> None:
    async with open_direct_page(headless=True) as real_page:
        await real_page.route(
            FIXTURE_URL,
            lambda route: route.fulfill(body=FIXTURE_HTML, content_type="text/html"),
        )
        await real_page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        raised = await raise_hand(real_page)
        assert raised.succeeded
        lowered = await lower_hand(real_page)
        assert lowered.succeeded
        sent = await send_chat(real_page, "live fixture ping")
        assert sent.succeeded


SECRET_PHRASE = "secret twin phrase"


async def test_ledger_records_intent_before_first_click(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-1" / "record.jsonl"
    journal = RecordingJournal("meeting-ledger-1")
    observations = [False, True]
    captured: dict[str, str] = {}

    async def observe() -> bool:
        return observations.pop(0) if observations else True

    async def act() -> MeetActionResult:
        captured["raw"] = actions_path(record).read_text(encoding="utf-8")
        return MeetActionResult(outcome="succeeded", detail="chat typed")

    result = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-1",
        kind="send_chat",
        act=act,
        observe=observe,
        tool_call_id="call-1",
        params=SECRET_PHRASE,
        chars=len(SECRET_PHRASE),
        journal=journal,
    )

    assert result.succeeded
    assert result.action_id == "call-1"
    assert result.attempts == 1
    assert not result.duplicate
    assert actions_path(record).name == ACTIONS_FILE_NAME
    assert SECRET_PHRASE not in captured["raw"]
    assert json.loads(captured["raw"])["intents"]["call-1"]["state"] == "requested"
    sidecar = json.loads(actions_path(record).read_text(encoding="utf-8"))
    intent = sidecar["intents"]["call-1"]
    assert intent["state"] == "observed_done"
    assert intent["observed_before"] is False
    assert intent["observed_after"] is True
    assert intent["chars"] == len(SECRET_PHRASE)
    assert len(intent["params_hash"]) == 64
    assert SECRET_PHRASE not in json.dumps(sidecar)
    requested = journal.events("meet_action_requested")
    finished = journal.events("meet_action_finished")
    assert [row["action_id"] for row in requested] == ["call-1"]
    assert finished and finished[0]["observed"] is True
    assert SECRET_PHRASE not in json.dumps(journal.lines)


async def test_observe_before_act_skips_click_and_records_observed_before(
    tmp_path: Path,
) -> None:
    record = tmp_path / "meeting-ledger-2" / "record.jsonl"
    acted: list[str] = []

    async def observe() -> bool:
        return True

    async def act() -> MeetActionResult:
        acted.append("click")
        return MeetActionResult(outcome="succeeded", detail="must not run")

    result = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-2",
        kind="raise_hand",
        act=act,
        observe=observe,
        tool_call_id="call-2",
    )

    assert result.succeeded
    assert acted == []
    ledger = MeetActionLedger.load(record, "meeting-ledger-2")
    intent = ledger.get("call-2")
    assert intent is not None
    assert intent.state == "observed_done"
    assert intent.observed_before is True
    assert intent.attempt == 1


async def test_only_retryable_faults_are_retried(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-3" / "record.jsonl"
    calls: list[str] = []
    failures: list[BaseException] = [
        ProviderFault("agent", "unavailable", retryable=True),
    ]
    observations = [False, False, True]

    async def observe() -> bool:
        return observations.pop(0) if observations else True

    async def flaky() -> MeetActionResult:
        calls.append("act")
        if failures:
            raise failures.pop(0)
        return MeetActionResult(outcome="succeeded", detail="hand raised")

    retried = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-3",
        kind="raise_hand",
        act=flaky,
        observe=observe,
        tool_call_id="call-retry",
    )

    assert retried.succeeded
    assert len(calls) == 2
    assert retried.attempts == 2

    async def refuse() -> bool:
        return False

    async def doomed() -> MeetActionResult:
        calls.append("doomed")
        raise ProviderFault("agent", "bad_request")

    failed = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-3",
        kind="lower_hand",
        act=doomed,
        observe=refuse,
        tool_call_id="call-doomed",
    )
    redelivered = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-3",
        kind="lower_hand",
        act=doomed,
        observe=refuse,
        tool_call_id="call-doomed",
    )

    assert failed.failed
    assert redelivered.failed
    assert redelivered.duplicate
    assert calls.count("doomed") == 1

    async def never() -> MeetActionResult:
        calls.append("never")
        return MeetActionResult(outcome="succeeded", detail="must not run")

    rejected = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-3",
        kind="send_chat",
        act=never,
        observe=refuse,
        tool_call_id="call-denied",
        authorized=False,
    )
    rejected_again = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-3",
        kind="send_chat",
        act=never,
        observe=refuse,
        tool_call_id="call-denied",
        authorized=False,
    )

    assert rejected.refused
    assert rejected_again.refused
    assert rejected_again.duplicate
    assert "never" not in calls


async def test_crash_resume_reconciles_through_observation_only(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-4" / "record.jsonl"
    journal = RecordingJournal("meeting-ledger-4")
    ledger = MeetActionLedger.load(record, "meeting-ledger-4")
    ledger.record("done-1", "raise_hand", "hash-done", True)
    ledger.mark("done-1", "observed_done", observed_before=False, observed_after=True)
    ledger.record("refused-1", "lower_hand", "hash-refused", False)
    ledger.mark("refused-1", "refused")
    ledger.record("crashed-1", "raise_hand", "hash-crashed", True)
    ledger.mark("crashed-1", "acted", observed_before=False, attempt=1)
    ledger.record("pending-1", "send_chat", "hash-pending", True, chars=9)
    ledger.save()
    seen: list[str] = []

    async def raise_state() -> bool:
        seen.append("raise_hand")
        return True

    async def chat_state() -> bool:
        seen.append("send_chat")
        return False

    async def lower_state() -> bool:
        raise AssertionError("terminal intents must not be rechecked")

    summary = await reconcile_ledger(
        record,
        "meeting-ledger-4",
        {"raise_hand": raise_state, "send_chat": chat_state, "lower_hand": lower_state},
        journal=journal,
    )

    assert summary == {"adopted": 1, "pending": 1}
    assert seen == ["raise_hand", "send_chat"]
    revived = MeetActionLedger.load(record, "meeting-ledger-4")
    crashed = revived.get("crashed-1")
    assert crashed is not None
    assert crashed.state == "observed_done"
    assert crashed.observed_after is True
    assert crashed.attempt == 1
    pending = revived.get("pending-1")
    assert pending is not None
    assert pending.state == "requested"
    done = revived.get("done-1")
    assert done is not None
    assert done.state == "observed_done"
    refused = revived.get("refused-1")
    assert refused is not None
    assert refused.state == "refused"
    observed = journal.events("meet_action_observed")
    assert [row["action_id"] for row in observed] == ["crashed-1"]


async def test_meet_action_writes_hold_single_writer_ledger_lock(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-5" / "record.jsonl"

    async def observe() -> bool:
        return True

    async def act() -> MeetActionResult:
        raise AssertionError("act must not run while the ledger lock is held")

    with ledger_lock(record):
        with pytest.raises(RecordLockError, match="locked"):
            await execute_meet_action(
                record,
                meeting_id="meeting-ledger-5",
                kind="raise_hand",
                act=act,
                observe=observe,
                tool_call_id="call-contended",
            )
    with ledger_lock(record):
        with pytest.raises(RecordLockError, match="locked"):
            with ledger_lock(record):
                pytest.fail("second writer must not acquire the lock")


async def test_double_delivery_actuates_exactly_once(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-6" / "record.jsonl"
    acted: list[str] = []
    observations = [False, True]
    observed_calls: list[str] = []

    async def observe() -> bool:
        observed_calls.append("observe")
        return observations.pop(0) if observations else True

    async def act() -> MeetActionResult:
        acted.append("click")
        return MeetActionResult(outcome="succeeded", detail="hand raised")

    first = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-6",
        kind="raise_hand",
        act=act,
        observe=observe,
        tool_call_id="call-double",
    )
    assert first.succeeded
    assert not first.duplicate
    assert len(acted) == 1

    retry = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-6",
        kind="raise_hand",
        act=act,
        observe=observe,
        tool_call_id="call-double",
    )
    assert retry.succeeded
    assert retry.duplicate
    assert len(acted) == 1

    restart = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-6",
        kind="raise_hand",
        act=act,
        observe=observe,
        tool_call_id="call-double",
    )
    assert restart.succeeded
    assert restart.duplicate
    assert len(acted) == 1
    assert len(observed_calls) == 2
    ledger = MeetActionLedger.load(record, "meeting-ledger-6")
    intent = ledger.get("call-double")
    assert intent is not None
    assert intent.state == "observed_done"
    assert intent.attempt == 1


def test_action_id_prefers_tool_call_id_and_falls_back_to_uuid5() -> None:
    assert action_id_for("meeting-ledger-7", 3, tool_call_id="call-x") == "call-x"
    first = action_id_for("meeting-ledger-7", 3)
    assert first == action_id_for("meeting-ledger-7", 3)
    assert first != action_id_for("meeting-ledger-7", 4)
    assert first != action_id_for("other-meeting", 3)


async def test_execute_succeeds_while_meeting_lock_is_held(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-8" / "record.jsonl"
    observations = [False, True]

    async def observe() -> bool:
        return observations.pop(0) if observations else True

    async def act() -> MeetActionResult:
        return MeetActionResult(outcome="succeeded", detail="hand raised")

    with record_lock(record, purpose="meeting"):
        result = await execute_meet_action(
            record,
            meeting_id="meeting-ledger-8",
            kind="raise_hand",
            act=act,
            observe=observe,
            tool_call_id="call-held",
        )

    assert result.succeeded
    assert not result.duplicate


async def test_reconcile_succeeds_while_meeting_lock_is_held(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-9" / "record.jsonl"
    ledger = MeetActionLedger.load(record, "meeting-ledger-9")
    ledger.record("crashed-held", "raise_hand", "hash-held", True)
    ledger.mark("crashed-held", "acted", observed_before=False, attempt=1)
    ledger.save()

    async def raise_state() -> bool:
        return True

    with record_lock(record, purpose="meeting"):
        summary = await reconcile_ledger(record, "meeting-ledger-9", {"raise_hand": raise_state})

    assert summary == {"adopted": 1, "pending": 0}


def test_ledger_load_rejects_corrupt_sidecar(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-10" / "record.jsonl"
    path = actions_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")

    with pytest.raises(LedgerError):
        MeetActionLedger.load(record, "meeting-ledger-10")


def test_ledger_load_rejects_schema_violation(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-11" / "record.jsonl"
    path = actions_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"meeting_id": "meeting-ledger-11", "next_seq": -1}')

    with pytest.raises(LedgerError):
        MeetActionLedger.load(record, "meeting-ledger-11")


def test_ledger_load_rejects_meeting_mismatch(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-12" / "record.jsonl"
    ledger = MeetActionLedger.load(record, "meeting-ledger-12")
    ledger.save()

    with pytest.raises(LedgerError):
        MeetActionLedger.load(record, "other-meeting")


async def test_execute_rejects_corrupt_sidecar_without_acting(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-13" / "record.jsonl"
    path = actions_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")

    async def observe() -> bool:
        raise AssertionError("observe must not run for a corrupt ledger")

    async def act() -> MeetActionResult:
        raise AssertionError("act must not run for a corrupt ledger")

    with pytest.raises(LedgerError):
        await execute_meet_action(
            record,
            meeting_id="meeting-ledger-13",
            kind="raise_hand",
            act=act,
            observe=observe,
            tool_call_id="call-corrupt",
        )


async def test_three_retryable_faults_fail_with_attempts_three(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-14" / "record.jsonl"
    calls: list[str] = []
    observations = [False, False, False]

    async def observe() -> bool:
        return observations.pop(0) if observations else False

    async def always_flaky() -> MeetActionResult:
        calls.append("act")
        raise ProviderFault("agent", "unavailable", retryable=True)

    result = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-14",
        kind="raise_hand",
        act=always_flaky,
        observe=observe,
        tool_call_id="call-exhausted",
    )

    assert result.failed
    assert result.attempts == 3
    assert "exhausted" in result.detail
    assert len(calls) == 3
    ledger = MeetActionLedger.load(record, "meeting-ledger-14")
    intent = ledger.get("call-exhausted")
    assert intent is not None
    assert intent.state == "failed"
    assert intent.attempt == 3


async def test_generic_exception_fails_without_retry(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-15" / "record.jsonl"
    calls: list[str] = []

    async def observe() -> bool:
        return False

    async def boom() -> MeetActionResult:
        calls.append("act")
        raise RuntimeError("dom exploded")

    result = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-15",
        kind="send_chat",
        act=boom,
        observe=observe,
        tool_call_id="call-boom",
    )

    assert result.failed
    assert result.attempts == 1
    assert result.detail == "act raised"
    assert len(calls) == 1


async def test_act_failure_result_is_terminal_without_retry(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-16" / "record.jsonl"
    calls: list[str] = []

    async def observe() -> bool:
        return False

    async def flop() -> MeetActionResult:
        calls.append("act")
        return MeetActionResult(outcome="failed", detail="send did not land")

    result = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-16",
        kind="send_chat",
        act=flop,
        observe=observe,
        tool_call_id="call-flop",
    )

    assert result.failed
    assert result.attempts == 1
    assert len(calls) == 1
    retry = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-16",
        kind="send_chat",
        act=flop,
        observe=observe,
        tool_call_id="call-flop",
    )
    assert retry.failed
    assert retry.duplicate
    assert len(calls) == 1


async def test_explicit_seq_advances_auto_counter(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-17" / "record.jsonl"
    meeting_id = "meeting-ledger-17"

    async def observe() -> bool:
        return True

    async def act() -> MeetActionResult:
        raise AssertionError("already effective so act must not run")

    first = await execute_meet_action(
        record, meeting_id=meeting_id, kind="raise_hand", act=act, observe=observe, seq=0
    )
    second = await execute_meet_action(
        record, meeting_id=meeting_id, kind="send_chat", act=act, observe=observe
    )
    third = await execute_meet_action(
        record, meeting_id=meeting_id, kind="lower_hand", act=act, observe=observe
    )
    fourth = await execute_meet_action(
        record, meeting_id=meeting_id, kind="raise_hand", act=act, observe=observe, seq=9
    )
    fifth = await execute_meet_action(
        record, meeting_id=meeting_id, kind="raise_hand", act=act, observe=observe
    )

    assert first.action_id == action_id_for(meeting_id, 0)
    assert second.action_id == action_id_for(meeting_id, 1)
    assert third.action_id == action_id_for(meeting_id, 2)
    assert fourth.action_id == action_id_for(meeting_id, 9)
    assert fifth.action_id == action_id_for(meeting_id, 10)
    assert not first.duplicate
    assert not second.duplicate
    assert not third.duplicate
    assert not fourth.duplicate
    assert not fifth.duplicate
    sidecar = json.loads(actions_path(record).read_text(encoding="utf-8"))
    assert sidecar["next_seq"] == 11
    assert len(sidecar["intents"]) == 5


def test_ledger_load_treats_directory_as_error(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-18" / "record.jsonl"
    path = actions_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()

    with pytest.raises(LedgerError):
        MeetActionLedger.load(record, "meeting-ledger-18")


def test_loop_lock_registry_is_keyed_by_loop_object() -> None:
    import asyncio

    from fakeyou import meet_action_ledger as ledger_module

    async def fetch_lock() -> object:
        return ledger_module._loop_lock()

    loop = asyncio.new_event_loop()
    try:
        first = loop.run_until_complete(fetch_lock())
        second = loop.run_until_complete(fetch_lock())
    finally:
        loop.close()

    assert first is second
    assert loop in ledger_module._LOCKS


async def test_retry_observation_preserves_first_observed_before(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-19" / "record.jsonl"
    calls: list[str] = []
    observations = [False, True]

    async def observe() -> bool:
        return observations.pop(0) if observations else True

    async def flaky() -> MeetActionResult:
        calls.append("act")
        raise ProviderFault("agent", "unavailable", retryable=True)

    result = await execute_meet_action(
        record,
        meeting_id="meeting-ledger-19",
        kind="raise_hand",
        act=flaky,
        observe=observe,
        tool_call_id="call-retry-observe",
    )

    assert result.succeeded
    assert result.attempts == 2
    assert len(calls) == 1
    ledger = MeetActionLedger.load(record, "meeting-ledger-19")
    intent = ledger.get("call-retry-observe")
    assert intent is not None
    assert intent.state == "observed_done"
    assert intent.observed_before is False
