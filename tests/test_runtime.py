import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.lifecycle import create_lifecycle_graph, initial_lifecycle_state
from fakeyou.runtime import SessionConfig, run_meeting_session

LOBBY = '<input placeholder="Your name"><button>Ask to join</button>'
IN_CALL = '<button aria-label="Leave call"></button><button>Mute microphone</button>'
DENIED = "<h1>You can't join this video call</h1>"
EXPLICIT_DENIED = "<h1>Someone in the call denied your request to join</h1>"
REMOVED = "<h1>You've been removed from the call</h1>"
ENDED = "<h1>The call has ended</h1>"

DIALOG_SELECTOR = 'text="Continue without microphone and camera"'
NAME_SELECTOR = "placeholder:Your name"
ASK_SELECTOR = 'text="Ask to join"'
LEAVE_SELECTOR = 'button[aria-label="Leave call"]'

NO_CAPTCHA = False

LINK = "https://meet.google.com/abc-defg-hij"


class _FakeKeyboard:
    def __init__(self, page):
        self._page = page

    async def type(self, text, delay=0):
        self._page.typed.append(text)


class _FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        scripted = self._page.counts.get(self.selector, [1])
        if len(scripted) > 1:
            return scripted.pop(0)
        return scripted[0]

    async def wait_for(self, **kwargs):
        self._page.waited.append(self.selector)

    async def click(self, **kwargs):
        self._page.clicked.append(self.selector)


class _FakePage:
    def __init__(self, contents=(), evaluates=(), counts=None, on_content=None):
        self._contents = list(contents)
        self._evaluates = list(evaluates)
        self.counts = {key: list(value) for key, value in (counts or {}).items()}
        self._on_content = on_content
        self._reads = 0
        self.visited = []
        self.clicked = []
        self.typed = []
        self.waited = []
        self.keyboard = _FakeKeyboard(self)

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        return None

    async def content(self):
        if not self._contents:
            raise AssertionError("page content read more times than scripted")
        result = self._contents.pop(0)
        self._reads += 1
        if self._on_content is not None:
            self._on_content(self._reads)
        if isinstance(result, Exception):
            raise result
        return result

    async def evaluate(self, expression):
        if not self._evaluates:
            raise AssertionError("page evaluate called more times than scripted")
        result = self._evaluates.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def get_by_placeholder(self, text):
        return _FakeLocator(self, f"placeholder:{text}")


class _FakePageSession:
    def __init__(self, page):
        self.page = page
        self.opened = 0
        self.closed = 0
        self.open_kwargs = []

    def open(self, **kwargs):
        self.opened += 1
        self.open_kwargs.append(kwargs)
        session = self

        @asynccontextmanager
        async def _session():
            try:
                yield session.page
            finally:
                session.closed += 1

        return _session()


class _FakeSaverSession:
    def __init__(self):
        self.saver = InMemorySaver()

    def factory(self):
        saver = self.saver

        @asynccontextmanager
        async def _session():
            yield saver

        return _session()


class _BlockingTerminalSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_started = asyncio.Event()
        self.release_terminal = asyncio.Event()
        self.terminal_persisted = asyncio.Event()
        self.terminal_cancelled = asyncio.Event()
        self._blocked = False

    def factory(self):
        saver = self

        @asynccontextmanager
        async def _session():
            yield saver

        return _session()

    async def aput(self, config, checkpoint, metadata, new_versions):
        values = checkpoint.get("channel_values", {})
        if (
            not self._blocked
            and values.get("phase") == "ended"
            and values.get("terminal_reason") == "owner_stop"
        ):
            self._blocked = True
            self.terminal_started.set()
            try:
                await self.release_terminal.wait()
            except asyncio.CancelledError:
                self.terminal_cancelled.set()
                raise
        result = await super().aput(config, checkpoint, metadata, new_versions)
        if self._blocked and values.get("phase") == "ended":
            self.terminal_persisted.set()
        return result


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def _config(**overrides):
    values = {
        "owner_id": "owner-1",
        "twin_id": "twin-1",
        "meeting_id": "meeting-1",
        "link": LINK,
        "twin_name": "Twin",
    }
    values.update(overrides)
    return SessionConfig(**values)


def _page(contents=(), **overrides):
    counts = {
        DIALOG_SELECTOR: [0],
        NAME_SELECTOR: [1],
        ASK_SELECTOR: [1],
    }
    counts.update(overrides.pop("counts", {}))
    return _FakePage(contents, counts=counts, **overrides)


async def _run(config, browser, saver, clock, stop=None):
    return await run_meeting_session(
        config,
        stop=stop,
        open_page=browser.open,
        saver_factory=saver.factory,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


async def test_run_meeting_session_aborts_join_when_stop_requested() -> None:
    browser = _FakePageSession(_page([]))
    saver = _FakeSaverSession()
    stop = asyncio.Event()
    stop.set()

    result = await _run(_config(headless=True), browser, saver, _FakeClock(), stop)

    assert result.phase == "ended"
    assert result.terminal_reason == "owner_stop"
    assert browser.opened == 0
    assert browser.closed == 0


async def test_run_meeting_session_stops_and_leaves_after_admission() -> None:
    stop = asyncio.Event()
    page = _page(
        [IN_CALL, IN_CALL, IN_CALL, ENDED],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = _FakePageSession(page)

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock(), stop)

    assert result.phase == "ended"
    assert result.terminal_reason == "admitted_then_stopped"


async def test_run_meeting_session_reports_denial() -> None:
    browser = _FakePageSession(_page([EXPLICIT_DENIED]))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "denied_by_host"
    assert browser.closed == 1


async def test_run_meeting_session_reports_platform_block() -> None:
    browser = _FakePageSession(_page([DENIED], evaluates=[NO_CAPTCHA]))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "blocked_by_platform"


async def test_run_meeting_session_reports_lobby_timeout() -> None:
    browser = _FakePageSession(_page([LOBBY, LOBBY, LOBBY, LOBBY]))

    result = await _run(
        _config(lobby_timeout_seconds=5.0, join_poll_interval_seconds=2.0),
        browser,
        _FakeSaverSession(),
        _FakeClock(),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "lobby_timeout"


async def test_run_meeting_session_reports_join_failure() -> None:
    browser = _FakePageSession(_page([], counts={NAME_SELECTOR: [0]}))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"
    assert "name field" in result.detail


async def test_run_meeting_session_maps_join_crash_to_browser_failure() -> None:
    browser = _FakePageSession(_page([RuntimeError("cdp gone")]))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"


async def test_run_meeting_session_reports_removal() -> None:
    browser = _FakePageSession(_page([IN_CALL, IN_CALL, REMOVED]))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "removed_by_host"


async def test_run_meeting_session_reports_meeting_ended() -> None:
    browser = _FakePageSession(_page([IN_CALL, IN_CALL, ENDED]))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "meeting_ended"


async def test_run_meeting_session_enforces_duration_limit() -> None:
    browser = _FakePageSession(_page([IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL, ENDED]))

    result = await _run(
        _config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        browser,
        _FakeSaverSession(),
        _FakeClock(),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit"


async def test_run_meeting_session_recovers_from_transient_lobby() -> None:
    browser = _FakePageSession(_page([IN_CALL, LOBBY, IN_CALL, REMOVED]))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "removed_by_host"


async def test_run_meeting_session_enforces_duration_limit_while_disconnected() -> None:
    page = _page([IN_CALL, LOBBY, LOBBY, LOBBY])
    browser = _FakePageSession(page)

    result = await _run(
        _config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        browser,
        _FakeSaverSession(),
        _FakeClock(),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit"
    assert ASK_SELECTOR in page.clicked
    assert LEAVE_SELECTOR not in page.clicked


async def test_run_meeting_session_maps_leave_failure_to_browser_failure() -> None:
    stop = asyncio.Event()
    page = _page(
        [IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 1 else None,
    )
    browser = _FakePageSession(page)

    result = await _run(
        _config(leave_timeout_seconds=3.0), browser, _FakeSaverSession(), _FakeClock(), stop
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"


async def test_run_meeting_session_maps_observation_crash_to_browser_failure() -> None:
    browser = _FakePageSession(_page([IN_CALL, RuntimeError("boom")]))

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"


async def test_run_meeting_session_returns_ended_result_without_reopening_browser() -> None:
    saver = _FakeSaverSession()
    first = _FakePageSession(_page([DENIED], evaluates=[NO_CAPTCHA]))

    result = await _run(_config(), first, saver, _FakeClock())

    assert result.terminal_reason == "blocked_by_platform"
    second = _FakePageSession(_page([LOBBY]))
    again = await _run(_config(), second, saver, _FakeClock())

    assert again.phase == "ended"
    assert again.terminal_reason == "blocked_by_platform"
    assert second.opened == 0


async def test_run_meeting_session_rejects_resume_of_interrupted_session() -> None:
    saver = _FakeSaverSession()
    graph = create_lifecycle_graph(checkpointer=saver.saver)
    graph.invoke(
        {
            **initial_lifecycle_state("owner-1", "twin-1", "meeting-1"),
            "event": {"kind": "join_requested", "attribution": "user_action", "detail": "t"},
        },
        {"configurable": {"thread_id": "twin-1:meeting-1"}},
    )
    browser = _FakePageSession(_page([LOBBY]))

    with pytest.raises(ValueError, match="cannot resume"):
        await _run(_config(), browser, saver, _FakeClock())

    assert browser.opened == 0


async def test_browser_startup_failure_is_persisted_as_terminal() -> None:
    saver = _FakeSaverSession()

    @asynccontextmanager
    async def fail_open(**kwargs):
        raise RuntimeError("browser launch failed")
        yield

    result = await run_meeting_session(_config(), open_page=fail_open, saver_factory=saver.factory)

    assert result.terminal_reason == "browser_failure"
    state = await create_lifecycle_graph(saver.saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert state.values["phase"] == "ended"
    assert state.values["external_effects"] == []


async def test_stop_arriving_with_browser_startup_does_not_request_join() -> None:
    stop = asyncio.Event()
    page = _page()
    browser = _FakePageSession(page)
    saver = _FakeSaverSession()

    @asynccontextmanager
    async def open_then_stop(**kwargs):
        async with browser.open(**kwargs) as opened:
            stop.set()
            yield opened

    result = await run_meeting_session(
        _config(), stop=stop, open_page=open_then_stop, saver_factory=saver.factory
    )

    assert result.terminal_reason == "owner_stop"
    assert page.visited == []
    assert page.clicked == []
    assert browser.closed == 1


async def test_leave_crash_is_persisted_and_browser_closed() -> None:
    stop = asyncio.Event()
    page = _page(
        [IN_CALL, RuntimeError("browser vanished while leaving")],
        on_content=lambda reads: stop.set() if reads == 1 else None,
    )
    browser = _FakePageSession(page)

    result = await _run(_config(), browser, _FakeSaverSession(), _FakeClock(), stop)

    assert result.terminal_reason == "browser_failure"
    assert browser.closed == 1


async def test_session_rejects_checkpoint_owned_by_another_owner() -> None:
    saver = _FakeSaverSession()
    first = _FakePageSession(_page([DENIED], evaluates=[NO_CAPTCHA]))
    await _run(_config(), first, saver, _FakeClock())
    other = _FakePageSession(_page())

    with pytest.raises(ValueError, match="identity"):
        await _run(_config(owner_id="other-owner"), other, saver, _FakeClock())

    assert other.opened == 0


async def test_stop_cancels_pending_navigation_and_closes_browser() -> None:
    stop = asyncio.Event()
    navigation_started = asyncio.Event()
    navigation_cancelled = asyncio.Event()
    page = _page()

    async def slow_navigation(*args, **kwargs):
        navigation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            navigation_cancelled.set()

    page.goto = slow_navigation
    browser = _FakePageSession(page)
    async with asyncio.TaskGroup() as tasks:
        running = tasks.create_task(
            _run(_config(), browser, _FakeSaverSession(), _FakeClock(), stop)
        )
        await asyncio.wait_for(navigation_started.wait(), timeout=1)
        stop.set()
        result = await asyncio.wait_for(running, timeout=1)

    assert result.terminal_reason == "owner_stop"
    assert navigation_cancelled.is_set()
    assert page.clicked == []
    assert browser.closed == 1


async def test_repeated_cancellation_preserves_terminal_checkpoint() -> None:
    admitted = asyncio.Event()
    page = _page(
        [IN_CALL, IN_CALL, "left"],
        on_content=lambda reads: admitted.set() if reads == 2 else None,
    )
    browser = _FakePageSession(page)
    saver = _BlockingTerminalSaver()

    async def paced_sleep(seconds: float) -> None:
        await asyncio.sleep(min(seconds, 0.01))

    running = asyncio.create_task(
        run_meeting_session(
            _config(monitor_interval_seconds=0.01),
            open_page=browser.open,
            saver_factory=saver.factory,
            sleep=paced_sleep,
            monotonic=time.monotonic,
        )
    )
    try:
        await asyncio.wait_for(admitted.wait(), timeout=1)
        running.cancel()
        await asyncio.wait_for(saver.terminal_started.wait(), timeout=1)
        assert not running.done()
        running.cancel()
        await asyncio.sleep(0)
        saver.release_terminal.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
        await asyncio.wait_for(saver.terminal_persisted.wait(), timeout=1)
        assert not saver.terminal_cancelled.is_set()
    finally:
        saver.release_terminal.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    state = await create_lifecycle_graph(saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert state.values["phase"] == "ended"
    assert state.values["terminal_reason"] == "owner_stop"
    assert browser.closed == 1


async def test_cancellation_during_leave_preserves_cancellation_and_terminal_checkpoint() -> None:
    stop = asyncio.Event()
    leaving = asyncio.Event()
    page = _page()
    saver = _FakeSaverSession()

    async def content():
        if not stop.is_set():
            stop.set()
            return IN_CALL
        leaving.set()
        await asyncio.Event().wait()

    page.content = content
    browser = _FakePageSession(page)
    running = asyncio.create_task(_run(_config(), browser, saver, _FakeClock(), stop))
    try:
        await asyncio.wait_for(leaving.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    saved = await create_lifecycle_graph(saver.saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert saved.values["phase"] == "ended"
    assert saved.values["terminal_reason"] == "owner_stop"
    assert saved.values["evidence"][-1]["event"] == "cancelled"
    assert page.clicked.count(LEAVE_SELECTOR) == 1
    assert browser.closed == 1


@pytest.mark.parametrize("field", ["owner_id", "twin_id", "meeting_id"])
@pytest.mark.parametrize("value", ["", "../another-profile", "contains:separator"])
def test_session_rejects_invalid_identity(field, value) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "lobby_timeout_seconds",
        "join_poll_interval_seconds",
        "session_duration_seconds",
        "monitor_interval_seconds",
        "leave_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_session_rejects_invalid_timing(field, value) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})
