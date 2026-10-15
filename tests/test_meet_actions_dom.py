import asyncio
import time
from pathlib import Path
from typing import Any, Self, cast

import pytest

import fakeyou.meet.meet_actions as meet_actions_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet_actions import MeetActionSignals, lower_hand, raise_hand, send_chat
from fakeyou.memory.memory import PREFETCH_EVIDENCE_PREFIX
from fakeyou.orchestration.runtime import MeetActionDispatcher
from tests._error_support import message_pattern
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import IN_CALL, FakePage, meet_page
from tests._meet_support import (
    CHAT_BUTTON_XPATH,
    CHAT_INPUT_XPATH,
    CHAT_TEXT,
    ECHO_CONTENT,
    FIXTURE_HTML,
    FIXTURE_URL,
    LOWER_XPATH,
    RAISE_XPATH,
    button_xpath,
    task5_dispatcher,
    with_enter,
)
from tests._persistence_fakes import RecordingJournal

ECHO_ITEM_XPATH = button_xpath("pin message")
TEXT_WITHOUT_PIN = IN_CALL + f"<div>{CHAT_TEXT}</div>"


async def test_send_chat_rejects_overlong_messages(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-chat")
    dispatcher, _record = task5_dispatcher(tmp_path, "meeting-chat", journal=journal)

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.MEET_CHAT_TOO_LONG)) as error:
        dispatcher.request("send_chat", action_id="call-long-1", text="x" * 501)

    assert error.value.code is ErrorCode.MEET_CHAT_TOO_LONG
    assert journal.events("meet_action_rejected")[0]["reason"] == "chat_too_long"


async def test_send_chat_rejects_memory_evidence_prefix(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-chat")
    dispatcher, _record = task5_dispatcher(tmp_path, "meeting-chat", journal=journal)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.MEET_CHAT_EVIDENCE_REJECTED),
    ) as error:
        dispatcher.request(
            "send_chat", action_id="call-evidence-1", text=PREFETCH_EVIDENCE_PREFIX + "{}"
        )

    assert error.value.code is ErrorCode.MEET_CHAT_EVIDENCE_REJECTED
    assert journal.events("meet_action_rejected")[0]["reason"] == "chat_evidence_rejected"


async def test_send_chat_enforces_per_minute_rate(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-chat")
    record = tmp_path / "meeting-chat" / "record.jsonl"
    dispatcher = MeetActionDispatcher(
        record, meeting_id="meeting-chat", enabled=True, journal=journal, chat_per_minute=2
    )
    clock = FakeClock()
    dispatcher.bind_page(
        meet_page([IN_CALL]),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        stop=asyncio.Event(),
    )

    dispatcher.request("send_chat", action_id="call-rate-1", text="one")
    dispatcher.request("send_chat", action_id="call-rate-2", text="two")
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEET_CHAT_RATE_LIMITED)
    ) as error:
        dispatcher.request("send_chat", action_id="call-rate-3", text="three")
    assert error.value.code is ErrorCode.MEET_CHAT_RATE_LIMITED
    clock.now += 61.0
    dispatcher.request("send_chat", action_id="call-rate-4", text="four")
    dispatcher.close()


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

    def counting(selector: str) -> Any:
        locate_calls.append(selector)
        return real_locator(selector)

    page_any = cast(Any, page)
    page_any.locator = counting

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [RAISE_XPATH, RAISE_XPATH, RAISE_XPATH]
    assert locate_calls.count(RAISE_XPATH) == 3


async def test_send_chat_opens_panel_types_and_succeeds_on_echo() -> None:
    page = meet_page([IN_CALL, ECHO_CONTENT], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    pressed = with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [CHAT_BUTTON_XPATH, CHAT_INPUT_XPATH]
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]
    assert CHAT_TEXT not in result.detail


async def test_send_chat_fails_without_echo_and_never_resends() -> None:
    page = meet_page([IN_CALL] + [IN_CALL] * 11)
    clock = FakeClock()
    pressed = with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert "not observed" in result.detail
    assert CHAT_TEXT not in result.detail
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]


async def test_send_chat_fails_when_pin_item_missing() -> None:
    page = meet_page([IN_CALL] + [TEXT_WITHOUT_PIN] * 11, counts={ECHO_ITEM_XPATH: [0] * 11})
    clock = FakeClock()
    with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert page.typed == [CHAT_TEXT]


async def test_send_chat_rejects_empty_text_without_touching_dom() -> None:
    page = meet_page([])
    clock = FakeClock()

    with pytest.raises(ValueError, match="empty"):
        await send_chat(page, "   ", sleep=clock.sleep, monotonic=clock.monotonic)

    assert page.clicked == []
    assert page.typed == []


async def test_custom_signals_drive_selectors() -> None:
    signals = MeetActionSignals(raise_hand="lever la main", lower_hand="baisser la main")
    page = meet_page([IN_CALL], visible={button_xpath("baisser la main"): False})
    clock = FakeClock()

    result = await raise_hand(page, signals, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [button_xpath("lever la main")]


@pytest.mark.browser
async def test_hand_and_chat_actions_against_fixture_page() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
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


class _ExplodingLocator:
    def __init__(self, real: Any, *, method: str) -> None:
        self._real = real
        self._method = method

    @property
    def first(self) -> Self:
        return self

    async def count(self) -> int:
        if self._method == "count":
            raise KeyError("locator bug")
        return cast(int, await self._real.count())

    async def wait_for(self, **kwargs: Any) -> None:
        if self._method == "wait_for":
            raise KeyError("locator bug")
        await self._real.wait_for(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _explode_locator(page: FakePage, method: str, selector: str | None = None) -> None:
    real_locator = page.locator

    def exploding(target: str, **kwargs: Any) -> Any:
        real = real_locator(target, **kwargs)
        if selector is not None and target != selector:
            return real
        return _ExplodingLocator(real, method=method)

    page_any = cast(Any, page)
    page_any.locator = exploding


async def test_send_chat_echo_count_bug_propagates() -> None:
    page = meet_page([IN_CALL] + [ECHO_CONTENT] * 12, visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    with_enter(page)
    _explode_locator(page, "count")

    with pytest.raises(KeyError, match="locator bug"):
        await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)


async def test_send_chat_panel_wait_bug_propagates() -> None:
    page = meet_page([IN_CALL], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    _explode_locator(page, "wait_for", CHAT_INPUT_XPATH)

    with pytest.raises(KeyError, match="locator bug"):
        await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)


async def test_send_chat_typing_bug_propagates() -> None:
    page = meet_page([IN_CALL])
    clock = FakeClock()
    with_enter(page)
    page_any = cast(Any, page)

    async def boom(text: str, **kwargs: Any) -> None:
        del kwargs
        del text
        raise KeyError("keyboard bug")

    page_any.keyboard.type = boom

    with pytest.raises(KeyError, match="keyboard bug"):
        await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)


async def test_echo_wait_bounds_each_content_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meet_actions_module, "_ECHO_READ_TIMEOUT_SECONDS", 0.01)
    page = meet_page([IN_CALL] + [IN_CALL] * 11)
    reads = 0

    async def content() -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            return IN_CALL
        await asyncio.sleep(5.0)
        return IN_CALL

    monkeypatch.setattr(page, "content", content)
    with_enter(page)
    clock = FakeClock()
    started = time.monotonic()

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert time.monotonic() - started < 2.0
