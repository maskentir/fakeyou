import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from fakeyou.catalog.errors import ErrorCode
from fakeyou.meet.meet_action_ledger import (
    ActionIntent,
    LedgerError,
    LedgerResult,
    MeetActionAddress,
    MeetActionCall,
    MeetActionEffects,
    MeetActionLedger,
    action_id_for,
    actions_path,
    execute_meet_action,
    ledger_lock,
    reconcile_ledger,
)
from fakeyou.meet.meet_actions import MeetActionResult
from fakeyou.meet.meet_tools import create_meet_action_tools
from fakeyou.memory.persona import refuse_meet_tool
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.persistence.ingestion_status import RecordLockError, record_lock
from fakeyou.persistence.layout import ACTIONS_FILE_NAME
from tests._error_support import message_pattern
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import IN_CALL, meet_page
from tests._meet_support import LOWER_XPATH, RAISE_XPATH, task5_dispatcher, tool_call, tool_text
from tests._persistence_fakes import RecordingJournal

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
        address=MeetActionAddress(meeting_id="meeting-ledger-1", tool_call_id="call-1"),
        call=MeetActionCall(kind="send_chat", params=SECRET_PHRASE, chars=len(SECRET_PHRASE)),
        effects=MeetActionEffects(act, observe),
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
    assert finished
    assert finished[0]["observed"] is True
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
        address=MeetActionAddress(meeting_id="meeting-ledger-2", tool_call_id="call-2"),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act, observe),
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
        address=MeetActionAddress(meeting_id="meeting-ledger-3", tool_call_id="call-retry"),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(flaky, observe),
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
        address=MeetActionAddress(meeting_id="meeting-ledger-3", tool_call_id="call-doomed"),
        call=MeetActionCall(kind="lower_hand"),
        effects=MeetActionEffects(doomed, refuse),
    )
    redelivered = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-ledger-3", tool_call_id="call-doomed"),
        call=MeetActionCall(kind="lower_hand"),
        effects=MeetActionEffects(doomed, refuse),
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
        address=MeetActionAddress(meeting_id="meeting-ledger-3", tool_call_id="call-denied"),
        call=MeetActionCall(kind="send_chat", authorized=False),
        effects=MeetActionEffects(never, refuse),
    )
    rejected_again = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-ledger-3", tool_call_id="call-denied"),
        call=MeetActionCall(kind="send_chat", authorized=False),
        effects=MeetActionEffects(never, refuse),
    )

    assert rejected.refused
    assert rejected_again.refused
    assert rejected_again.duplicate
    assert "never" not in calls


async def test_crash_resume_reconciles_through_observation_only(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-4" / "record.jsonl"
    journal = RecordingJournal("meeting-ledger-4")
    ledger = MeetActionLedger.load(record, "meeting-ledger-4")
    ledger.record("done-1", "raise_hand", "hash-done", authorized=True)
    ledger.mark("done-1", "observed_done", observed_before=False, observed_after=True)
    ledger.record("refused-1", "lower_hand", "hash-refused", authorized=False)
    ledger.mark("refused-1", "refused")
    ledger.record("crashed-1", "raise_hand", "hash-crashed", authorized=True)
    ledger.mark("crashed-1", "acted", observed_before=False, attempt=1)
    ledger.record("pending-1", "send_chat", "hash-pending", authorized=True, chars=9)
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

    with ledger_lock(record), pytest.raises(RecordLockError, match="locked"):
        await execute_meet_action(
            record,
            address=MeetActionAddress(meeting_id="meeting-ledger-5", tool_call_id="call-contended"),
            call=MeetActionCall(kind="raise_hand"),
            effects=MeetActionEffects(act, observe),
        )
    with ledger_lock(record), pytest.raises(RecordLockError, match="locked"):
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
        address=MeetActionAddress(meeting_id="meeting-ledger-6", tool_call_id="call-double"),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act, observe),
    )
    assert first.succeeded
    assert not first.duplicate
    assert len(acted) == 1

    retry = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-ledger-6", tool_call_id="call-double"),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act, observe),
    )
    assert retry.succeeded
    assert retry.duplicate
    assert len(acted) == 1

    restart = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-ledger-6", tool_call_id="call-double"),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act, observe),
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
            address=MeetActionAddress(meeting_id="meeting-ledger-8", tool_call_id="call-held"),
            call=MeetActionCall(kind="raise_hand"),
            effects=MeetActionEffects(act, observe),
        )

    assert result.succeeded
    assert not result.duplicate


async def test_reconcile_succeeds_while_meeting_lock_is_held(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-9" / "record.jsonl"
    ledger = MeetActionLedger.load(record, "meeting-ledger-9")
    ledger.record("crashed-held", "raise_hand", "hash-held", authorized=True)
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

    with pytest.raises(
        LedgerError, match=message_pattern(ErrorCode.ACTION_LEDGER_UNREADABLE)
    ) as caught:
        MeetActionLedger.load(record, "meeting-ledger-10")
    assert caught.value.code is ErrorCode.ACTION_LEDGER_UNREADABLE


def test_ledger_load_rejects_schema_violation(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-11" / "record.jsonl"
    path = actions_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"meeting_id": "meeting-ledger-11", "next_seq": -1}')

    with pytest.raises(
        LedgerError, match=message_pattern(ErrorCode.ACTION_LEDGER_UNREADABLE)
    ) as caught:
        MeetActionLedger.load(record, "meeting-ledger-11")
    assert caught.value.code is ErrorCode.ACTION_LEDGER_UNREADABLE


def test_ledger_load_rejects_meeting_mismatch(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-12" / "record.jsonl"
    ledger = MeetActionLedger.load(record, "meeting-ledger-12")
    ledger.save()

    with pytest.raises(
        LedgerError, match=message_pattern(ErrorCode.ACTION_LEDGER_MEETING_MISMATCH)
    ) as caught:
        MeetActionLedger.load(record, "other-meeting")
    assert caught.value.code is ErrorCode.ACTION_LEDGER_MEETING_MISMATCH


async def test_execute_rejects_corrupt_sidecar_without_acting(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-13" / "record.jsonl"
    path = actions_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")

    async def observe() -> bool:
        raise AssertionError("observe must not run for a corrupt ledger")

    async def act() -> MeetActionResult:
        raise AssertionError("act must not run for a corrupt ledger")

    with pytest.raises(LedgerError, match=message_pattern(ErrorCode.ACTION_LEDGER_UNREADABLE)):
        await execute_meet_action(
            record,
            address=MeetActionAddress(meeting_id="meeting-ledger-13", tool_call_id="call-corrupt"),
            call=MeetActionCall(kind="raise_hand"),
            effects=MeetActionEffects(act, observe),
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
        address=MeetActionAddress(meeting_id="meeting-ledger-14", tool_call_id="call-exhausted"),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(always_flaky, observe),
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
        address=MeetActionAddress(meeting_id="meeting-ledger-15", tool_call_id="call-boom"),
        call=MeetActionCall(kind="send_chat"),
        effects=MeetActionEffects(boom, observe),
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
        address=MeetActionAddress(meeting_id="meeting-ledger-16", tool_call_id="call-flop"),
        call=MeetActionCall(kind="send_chat"),
        effects=MeetActionEffects(flop, observe),
    )

    assert result.failed
    assert result.attempts == 1
    assert len(calls) == 1
    retry = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-ledger-16", tool_call_id="call-flop"),
        call=MeetActionCall(kind="send_chat"),
        effects=MeetActionEffects(flop, observe),
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
        record,
        address=MeetActionAddress(meeting_id=meeting_id, seq=0),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act, observe),
    )
    second = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id=meeting_id),
        call=MeetActionCall(kind="send_chat"),
        effects=MeetActionEffects(act, observe),
    )
    third = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id=meeting_id),
        call=MeetActionCall(kind="lower_hand"),
        effects=MeetActionEffects(act, observe),
    )
    fourth = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id=meeting_id, seq=9),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act, observe),
    )
    fifth = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id=meeting_id),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act, observe),
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

    with pytest.raises(LedgerError, match=message_pattern(ErrorCode.ACTION_LEDGER_UNREADABLE)):
        MeetActionLedger.load(record, "meeting-ledger-18")


def test_contended_meet_actions_serialize_in_every_event_loop(tmp_path: Path) -> None:
    async def contend(meeting_id: str) -> list[bool]:
        record = tmp_path / meeting_id / "record.jsonl"
        release = asyncio.Event()
        raised: list[bool] = []

        async def observe() -> bool:
            return bool(raised)

        async def act() -> MeetActionResult:
            await release.wait()
            raised.append(True)
            return MeetActionResult(outcome="succeeded", detail="hand raised")

        def run(call_id: str) -> Coroutine[Any, Any, LedgerResult]:
            return execute_meet_action(
                record,
                address=MeetActionAddress(meeting_id=meeting_id, tool_call_id=call_id),
                call=MeetActionCall(kind="raise_hand"),
                effects=MeetActionEffects(act, observe),
            )

        first = asyncio.create_task(run("call-a"))
        second = asyncio.create_task(run("call-b"))
        await asyncio.sleep(0.01)
        release.set()
        return [result.succeeded for result in await asyncio.gather(first, second)]

    for index in range(2):
        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(contend(f"meeting-ledger-loop-{index}")) == [True, True]
        finally:
            loop.close()


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
        address=MeetActionAddress(
            meeting_id="meeting-ledger-19", tool_call_id="call-retry-observe"
        ),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(flaky, observe),
    )

    assert result.succeeded
    assert result.attempts == 2
    assert len(calls) == 1
    ledger = MeetActionLedger.load(record, "meeting-ledger-19")
    intent = ledger.get("call-retry-observe")
    assert intent is not None
    assert intent.state == "observed_done"
    assert intent.observed_before is False


async def test_double_delivery_retry_then_restart_actuates_exactly_once(
    tmp_path: Path,
) -> None:
    meeting_id = "meeting-e2e-double"
    journal = RecordingJournal(meeting_id)
    dispatcher, record = task5_dispatcher(tmp_path, meeting_id, journal=journal)
    page = meet_page(
        [IN_CALL],
        visible={LOWER_XPATH: False},
        reveal_on_click={RAISE_XPATH: LOWER_XPATH},
    )
    clock = FakeClock()
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=journal)

    settled = await tools[0].ainvoke(tool_call("meet_raise_hand", "call-e2e-double"))

    result = json.loads(tool_text(settled))
    assert result["code"] == "succeeded"
    assert result["state"] == "observed_done"
    assert result["action_id"] == "call-e2e-double"
    await dispatcher.drain()
    dispatcher.request("raise_hand", action_id="call-e2e-double")
    dispatcher.request("raise_hand", action_id="call-e2e-double")
    await dispatcher.drain()
    dispatcher.close()
    revived, _ = task5_dispatcher(tmp_path, meeting_id, journal=journal)
    revived.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    revived.request("raise_hand", action_id="call-e2e-double")
    await revived.drain()
    revived.close()

    assert page.clicked == [RAISE_XPATH]

    async def boom() -> bool:
        raise AssertionError("terminal intents must not be rechecked")

    summary = await reconcile_ledger(record, meeting_id, {"raise_hand": boom})

    assert summary == {"adopted": 0, "pending": 0}
    assert page.clicked == [RAISE_XPATH]
    ledger = MeetActionLedger.load(record, meeting_id)
    intent = ledger.get("call-e2e-double")

    assert intent is not None
    assert intent.state == "observed_done"
    assert intent.attempt == 1
    suppressed = [
        row
        for row in journal.events("meet_action_rejected")
        if row["reason"] == "duplicate_suppressed"
    ]

    assert len(suppressed) == 3
    assert [row["event"] for row in journal.events("meet_action_finished")] == [
        "meet_action_finished"
    ]


async def test_act_bug_returns_recorded_failure(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-bug" / "record.jsonl"
    journal = RecordingJournal("meeting-ledger-bug")

    async def observe() -> bool:
        return False

    async def boom() -> MeetActionResult:
        raise KeyError("selector bug")

    result = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-ledger-bug", tool_call_id="call-bug"),
        call=MeetActionCall(kind="send_chat"),
        effects=MeetActionEffects(boom, observe),
        journal=journal,
    )

    assert result.succeeded is False
    assert result.detail == "act raised"
    sidecar = json.loads(actions_path(record).read_text(encoding="utf-8"))
    assert sidecar["intents"]["call-bug"]["state"] == "failed"
    [row] = journal.events("meet_action_failed")
    assert row["reason"] == "KeyError"


async def test_playwright_error_from_act_fails_without_retry(tmp_path: Path) -> None:
    record = tmp_path / "meeting-ledger-pw" / "record.jsonl"
    calls: list[str] = []

    async def observe() -> bool:
        return False

    async def boom() -> MeetActionResult:
        calls.append("act")
        raise PlaywrightError("target crashed")

    result = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-ledger-pw", tool_call_id="call-pw"),
        call=MeetActionCall(kind="send_chat"),
        effects=MeetActionEffects(boom, observe),
    )

    assert result.failed
    assert result.attempts == 1
    assert result.detail == "act raised"
    assert len(calls) == 1


def _d6_record(tmp_path: Path) -> Path:
    return tmp_path / "meeting-d6" / "record.jsonl"


async def _run_d6_action(
    tmp_path: Path,
    *,
    tool_call_id: str,
    act: Callable[[], Awaitable[MeetActionResult]],
    observe: Callable[[], Awaitable[bool]] | None = None,
) -> LedgerResult:
    performed: list[str] = []

    async def act_and_record() -> MeetActionResult:
        performed.append("act")
        return await act()

    async def readback_after_act() -> bool:
        return bool(performed)

    return await execute_meet_action(
        _d6_record(tmp_path),
        address=MeetActionAddress(meeting_id="meeting-d6", tool_call_id=tool_call_id),
        call=MeetActionCall(kind="raise_hand"),
        effects=MeetActionEffects(act_and_record, observe or readback_after_act),
    )


def _load_d6_intent(tmp_path: Path, tool_call_id: str) -> ActionIntent | None:
    return MeetActionLedger.load(_d6_record(tmp_path), "meeting-d6").get(tool_call_id)


async def test_duplicate_tool_call_id_does_not_repeat_effect(tmp_path: Path) -> None:
    acted: list[str] = []

    async def act() -> MeetActionResult:
        acted.append("act")
        return MeetActionResult(outcome="succeeded", detail="")

    first = await _run_d6_action(tmp_path, tool_call_id="call-d6-1", act=act)
    second = await _run_d6_action(tmp_path, tool_call_id="call-d6-1", act=act)
    assert acted == ["act"]
    assert first.duplicate is False
    assert second.duplicate is True


async def test_observe_comes_from_readback_not_act_result(tmp_path: Path) -> None:
    async def act() -> MeetActionResult:
        return MeetActionResult(outcome="succeeded", detail="")

    async def readback_says_no() -> bool:
        return False

    result = await _run_d6_action(
        tmp_path, tool_call_id="call-d6-2", act=act, observe=readback_says_no
    )
    assert result.succeeded is False
    intent = _load_d6_intent(tmp_path, "call-d6-2")
    assert intent is not None
    assert intent.state != "observed_done"
    assert intent.observed_after is False


async def test_finished_actions_journal_their_phase_timings(tmp_path: Path) -> None:
    record = tmp_path / "meeting-phases" / "record.jsonl"
    journal = RecordingJournal("meeting-phases")
    observations = [False, True]

    async def observe() -> bool:
        return observations.pop(0) if observations else True

    async def act() -> MeetActionResult:
        return MeetActionResult(outcome="succeeded", detail="chat typed")

    result = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-phases", tool_call_id="call-p"),
        call=MeetActionCall(kind="send_chat", params="hello", chars=5),
        effects=MeetActionEffects(act, observe),
        journal=journal,
    )

    assert result.succeeded
    finished = journal.events("meet_action_finished")
    assert len(finished) == 1
    assert finished[0]["act_ms"] >= 0
    assert finished[0]["observe_ms"] >= 0
    assert finished[0]["queue_ms"] >= 0


async def test_a_failed_act_keeps_its_detail_in_the_journal(tmp_path: Path) -> None:
    record = tmp_path / "meeting-detail" / "record.jsonl"
    journal = RecordingJournal("meeting-detail")

    async def observe() -> bool:
        return False

    async def act() -> MeetActionResult:
        return MeetActionResult(outcome="failed", detail="chat input never appeared")

    result = await execute_meet_action(
        record,
        address=MeetActionAddress(meeting_id="meeting-detail", tool_call_id="call-d"),
        call=MeetActionCall(kind="send_chat", params="hello", chars=5),
        effects=MeetActionEffects(act, observe),
        journal=journal,
    )

    assert result.outcome == "failed"
    assert "chat input never appeared" in result.detail
    failed = journal.events("meet_action_failed")
    assert failed[0]["detail"] == "chat input never appeared"
