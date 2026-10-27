from pathlib import Path

import pytest

from fakeyou.meet.command_ledger import CommandLedger, CommandRecord, LedgerError, ledger_path
from fakeyou.session.commands import CommandTransitionError


def record_path(tmp_path: Path) -> Path:
    return tmp_path / "meeting-9" / "record.jsonl"


def chat(command_id: str = "c1") -> CommandRecord:
    return CommandRecord(
        command_id=command_id,
        kind="send_chat",
        attempt_id="a1",
        plan_id="p1",
        actor="dev-ayu",
        browser_epoch=1,
        verification="protocol_echo",
        text="Deck is up.",
    )


def test_the_boundary_is_on_disk_before_the_effect(tmp_path: Path) -> None:
    ledger = CommandLedger(record_path(tmp_path), "meeting-9")
    ledger.accept(chat())
    ledger.move("c1", "ready")

    ledger.move("c1", "dispatching")
    reread = CommandLedger(record_path(tmp_path), "meeting-9").get("c1")

    assert reread is not None
    assert reread.state == "dispatching"
    assert reread.boundary_at is not None
    assert reread.dispatches == 1


def test_accepting_the_same_command_twice_keeps_the_first(tmp_path: Path) -> None:
    ledger = CommandLedger(record_path(tmp_path), "meeting-9")
    ledger.accept(chat())
    ledger.move("c1", "refused", detail="chat_rate_limited")

    again = ledger.accept(chat())

    assert again.state == "refused"
    assert again.detail == "chat_rate_limited"


def test_a_terminal_command_cannot_be_moved(tmp_path: Path) -> None:
    ledger = CommandLedger(record_path(tmp_path), "meeting-9")
    ledger.accept(chat())
    ledger.move("c1", "cancelled_before_boundary")

    with pytest.raises(CommandTransitionError):
        ledger.move("c1", "dispatching")


def test_a_ledger_from_another_meeting_or_schema_is_refused(tmp_path: Path) -> None:
    CommandLedger(record_path(tmp_path), "meeting-9").accept(chat())

    with pytest.raises(LedgerError):
        CommandLedger(record_path(tmp_path), "meeting-7").records()
    ledger_path(record_path(tmp_path)).write_text('{"meeting_id":"meeting-9","intents":{}}')
    with pytest.raises(LedgerError):
        CommandLedger(record_path(tmp_path), "meeting-9").records()
