import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Page

import fakeyou.orchestration.meet_actions as actions_module
from fakeyou.meet.meet_action_ledger import LedgerResult
from fakeyou.orchestration.meet_actions import MeetActionDispatcher


async def test_a_successful_chat_send_is_reported_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str]] = []

    async def succeeded(*args: Any, **kwargs: Any) -> LedgerResult:
        del args, kwargs
        return LedgerResult(action_id="a1", outcome="succeeded", detail="ok", attempts=1)

    monkeypatch.setattr(actions_module, "execute_meet_action", succeeded)
    dispatcher = MeetActionDispatcher(
        tmp_path / "record.jsonl", meeting_id="m1", enabled=True, chat_per_minute=3
    )
    dispatcher.on_chat_sent = lambda action_id, text: sent.append((action_id, text))
    dispatcher.bind_page(
        cast(Page, object()),
        sleep=asyncio.sleep,
        monotonic=lambda: 0.0,
        stop=asyncio.Event(),
    )

    dispatcher.request("send_chat", action_id="a1", text="hello room")
    await dispatcher.drain()

    assert sent == [("a1", "hello room")]


async def test_a_successful_leave_is_reported_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left: list[bool] = []

    async def succeeded(*args: Any, **kwargs: Any) -> LedgerResult:
        del args, kwargs
        return LedgerResult(action_id="l1", outcome="succeeded", detail="ok", attempts=1)

    monkeypatch.setattr(actions_module, "execute_meet_action", succeeded)
    dispatcher = MeetActionDispatcher(
        tmp_path / "record.jsonl", meeting_id="m1", enabled=True, chat_per_minute=3
    )
    dispatcher.on_leave = lambda: left.append(True)
    dispatcher.bind_page(
        cast(Page, object()),
        sleep=asyncio.sleep,
        monotonic=lambda: 0.0,
        stop=asyncio.Event(),
    )

    dispatcher.request("leave", action_id="l1")
    await dispatcher.drain()

    assert left == [True]
    assert dispatcher.left


async def test_a_scheduled_leave_waits_for_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def succeeded(*args: Any, **kwargs: Any) -> LedgerResult:
        del args, kwargs
        calls.append("leave")
        return LedgerResult(action_id="l1", outcome="succeeded", detail="ok", attempts=1)

    monkeypatch.setattr(actions_module, "execute_meet_action", succeeded)
    dispatcher = MeetActionDispatcher(
        tmp_path / "record.jsonl", meeting_id="m1", enabled=True, chat_per_minute=3
    )
    dispatcher.bind_page(
        cast(Page, object()),
        sleep=asyncio.sleep,
        monotonic=lambda: 0.0,
        stop=asyncio.Event(),
    )

    dispatcher.schedule_leave(action_id="l1")
    await asyncio.sleep(0.01)

    assert calls == []
    assert not dispatcher.left

    dispatcher.release_leave()
    await dispatcher.drain()

    assert calls == ["leave"]
    assert dispatcher.left


async def test_a_cancelled_scheduled_leave_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def succeeded(*args: Any, **kwargs: Any) -> LedgerResult:
        del args, kwargs
        calls.append("leave")
        return LedgerResult(action_id="l1", outcome="succeeded", detail="ok", attempts=1)

    monkeypatch.setattr(actions_module, "execute_meet_action", succeeded)
    dispatcher = MeetActionDispatcher(
        tmp_path / "record.jsonl", meeting_id="m1", enabled=True, chat_per_minute=3
    )
    dispatcher.bind_page(
        cast(Page, object()),
        sleep=asyncio.sleep,
        monotonic=lambda: 0.0,
        stop=asyncio.Event(),
    )

    dispatcher.schedule_leave(action_id="l1")
    dispatcher.cancel_leave()
    dispatcher.release_leave()
    await dispatcher.drain()

    assert calls == []
    assert not dispatcher.left


async def test_a_failed_leave_does_not_mark_the_dispatcher_as_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed(*args: Any, **kwargs: Any) -> LedgerResult:
        del args, kwargs
        return LedgerResult(action_id="l1", outcome="failed", detail="no", attempts=1)

    monkeypatch.setattr(actions_module, "execute_meet_action", failed)
    dispatcher = MeetActionDispatcher(
        tmp_path / "record.jsonl", meeting_id="m1", enabled=True, chat_per_minute=3
    )
    dispatcher.bind_page(
        cast(Page, object()),
        sleep=asyncio.sleep,
        monotonic=lambda: 0.0,
        stop=asyncio.Event(),
    )

    dispatcher.request("leave", action_id="l1")
    await dispatcher.drain()

    assert not dispatcher.left
