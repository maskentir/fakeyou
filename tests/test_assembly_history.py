import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

import fakeyou.orchestration.assembly as assembly_module
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import MeetingResponder
from fakeyou.conversation.session import SessionConfig
from fakeyou.orchestration.assembly import AgentWiring, TurnBehavior
from fakeyou.persistence.record import MeetingRecord


def _capture(created: dict[str, Any]) -> Any:
    def fake_agent(_model: Any, **kwargs: Any) -> Any:
        created.update(kwargs)
        created["middleware"] = [type(item).__name__ for item in kwargs["middleware"]]
        return None

    return fake_agent


async def test_assembly_reads_history_from_the_meeting_record(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}
    monkeypatch.setattr(assembly_module, "create_meeting_agent", _capture(created))
    config = SessionConfig(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        meeting_id=SessionId("history-wiring"),
        link="https://meet.google.com/abc-defg-hij",
        twin_name="History Twin",
    )
    record = MeetingRecord(tmp_path / "record.jsonl")

    async with assembly_module.open_conversation(
        settings,
        config,
        agent=AgentWiring(history=record),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
    ) as turns:
        responder = turns.responder

    record.close()
    assert isinstance(responder, MeetingResponder)
    assert responder.history is record
    assert responder.history_max_tokens == settings.conversation.history_max_tokens
    assert "checkpointer" not in created
    assert not {"HistoryTrimMiddleware", "OrphanToolCallMiddleware"} & set(created["middleware"])


async def test_assembly_teardown_budgets_a_hanging_close(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.conversation.turn_lifecycle import TurnLifecycle

    async def hanging_close(self: Any) -> None:
        del self
        await asyncio.sleep(30.0)

    monkeypatch.setattr(TurnLifecycle, "close", hanging_close)
    config = SessionConfig(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        meeting_id=SessionId("history-teardown"),
        link="https://meet.google.com/abc-defg-hij",
        twin_name="History Twin",
    )

    async def run() -> None:
        async with assembly_module.open_conversation(
            settings,
            config,
            behavior=TurnBehavior(on_transition=lambda *_args: None),
        ):
            pass

    started = time.monotonic()
    await asyncio.wait_for(run(), timeout=20.0)
    assert time.monotonic() - started < 15.0
