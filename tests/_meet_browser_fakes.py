from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypedDict, Unpack, override

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

LOBBY = '<input placeholder="Your name"><button>Ask to join</button>'
IN_CALL = '<button aria-label="Leave call"></button><button>Mute microphone</button>'
DENIED = "<h1>You can't join this video call</h1>"
EXPLICIT_DENIED = "<h1>Someone in the call denied your request to join</h1>"
REMOVED = "<h1>You've been removed from the call</h1>"
ENDED = "<h1>The call has ended</h1>"
WAITING = LOBBY + "<div>Asking to be let in...</div>"
LEAVE_AND_WAITING = IN_CALL + "<div>Asking to be let in...</div>"

DIALOG_SELECTOR = 'text="Continue without microphone and camera"'
NAME_SELECTOR = "placeholder:Your name"
ASK_SELECTOR = 'text="Ask to join"'
JOIN_NOW_SELECTOR = 'text="Join now"'
LEAVE_SELECTOR = 'button[aria-label="Leave call"]'
MIC_ON_SELECTOR = (
    'button[aria-label^="Turn off microphone" i],button[aria-label^="Mute microphone" i]'
)
MIC_OFF_SELECTOR = (
    'button[aria-label^="Turn on microphone" i],button[aria-label^="Unmute microphone" i]'
)
CAM_ON_SELECTOR = 'button[aria-label^="Turn off camera" i]'
CAM_OFF_SELECTOR = 'button[aria-label^="Turn on camera" i]'

NO_CAPTCHA = False
LIVE_CAPTCHA = True


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def type(self, text: str, delay: float = 0) -> None:
        del delay
        self._page.typed.append(text)
        if text in self._page.type_failures:
            raise PlaywrightError(f"typing failed: {text}")

    async def insert_text(self, text: str) -> None:
        await self.type(text)

    async def press(self, key: str) -> None:
        self._page.typed.append(f"key:{key}")


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self.selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, index: int) -> FakeLocator:
        del index
        return FakeLocator(self._page, self.selector)

    async def count(self) -> int:
        scripted = self._page.counts.get(self.selector, [1])
        if len(scripted) > 1:
            return scripted.pop(0)
        return scripted[0]

    async def evaluate(self, expression: str, arg: Any = None, **kwargs: Any) -> Any:
        del arg
        del expression
        del kwargs
        self._page.locator_evaluated.append(self.selector)
        scripted = self._page.locator_evaluates.get(self.selector, [""])
        if len(scripted) > 1:
            result = scripted.pop(0)
        else:
            result = scripted[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def wait_for(self, **kwargs: Any) -> None:
        del kwargs
        if self.selector in self._page.wait_failures:
            raise PlaywrightError(f"wait failed: {self.selector}")
        remaining = self._page.wait_flaky.get(self.selector, 0)
        if remaining > 0:
            self._page.wait_flaky[self.selector] = remaining - 1
            raise PlaywrightError(f"not visible yet: {self.selector}")
        self._page.waited.append(self.selector)

    async def is_visible(self) -> bool:
        value = self._page.visible.get(self.selector, True)
        if isinstance(value, Exception):
            raise value
        return value

    async def evaluate_all(self, expression: str) -> list[Any]:
        del expression
        return []

    async def click(self, **kwargs: Any) -> None:
        del kwargs
        self._page.clicked.append(self.selector)
        remaining = self._page.click_flaky.get(self.selector, 0)
        if remaining > 0:
            self._page.click_flaky[self.selector] = remaining - 1
            raise PlaywrightError(f"click failed: {self.selector}")
        if self.selector in self._page.click_failures:
            raise PlaywrightError(f"click failed: {self.selector}")
        revealed = self._page.reveal_on_click.get(self.selector)
        if revealed is not None:
            self._page.visible[revealed] = True


class PageScript(TypedDict, total=False):
    counts: dict[str, list[int]]
    locator_evaluates: dict[str, list[Any]]
    click_failures: Iterable[str]
    click_flaky: dict[str, int]
    wait_flaky: dict[str, int]
    type_failures: Iterable[str]
    goto_failures: int
    final_url: str
    visible: dict[str, bool | Exception]
    reveal_on_click: dict[str, str]
    wait_failures: Iterable[str]
    on_content: Callable[[int], None]


class FakePage(Page):
    def __init__(
        self,
        contents: Sequence[str | Exception] = (),
        evaluates: Sequence[Any] = (),
        **script: Unpack[PageScript],
    ) -> None:
        self._on_content = script.get("on_content")
        self._reads = 0
        self._contents = list(contents)
        self._evaluates = list(evaluates)
        self.counts = {key: list(value) for key, value in script.get("counts", {}).items()}
        self.locator_evaluates = {
            key: list(value) for key, value in script.get("locator_evaluates", {}).items()
        }
        self.locator_evaluated: list[str] = []
        self.click_failures = set(script.get("click_failures", ()))
        self.click_flaky = dict(script.get("click_flaky", {}))
        self.wait_flaky = dict(script.get("wait_flaky", {}))
        self.visible = dict(script.get("visible", {}))
        self.reveal_on_click = dict(script.get("reveal_on_click", {}))
        self.wait_failures = set(script.get("wait_failures", ()))
        self.type_failures = set(script.get("type_failures", ()))
        self._goto_failures = script.get("goto_failures", 0)
        self._final_url = script.get("final_url")
        self.visited: list[str] = []
        self.init_scripts: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[str] = []
        self.waited: list[str] = []
        self.evaluate_calls: list[str] = []
        self.listeners: dict[str, list[Any]] = {}
        self.page_closed = False
        self._keyboard: Any = FakeKeyboard(self)

    @override
    def on(self, event: Any, f: Any) -> None:
        self.listeners.setdefault(str(event), []).append(f)

    @override
    def remove_listener(self, event: Any, f: Any) -> None:
        handlers = self.listeners.get(str(event), [])
        if f in handlers:
            handlers.remove(f)

    @override
    def is_closed(self) -> bool:
        return self.page_closed

    def emit(self, event: str) -> None:
        for handler in list(self.listeners.get(event, [])):
            handler(self)

    def crash(self) -> None:
        self.page_closed = True
        self.emit("close")

    def crash_renderer(self) -> None:
        self.emit("crash")

    def shut(self) -> None:
        if not self.page_closed:
            self.page_closed = True
            self.emit("close")

    @property
    @override
    def keyboard(self) -> Any:
        return self._keyboard

    @property
    @override
    def url(self) -> str:
        if self._final_url is not None:
            return self._final_url
        return self.visited[-1] if self.visited else "about:blank"

    @override
    async def add_init_script(self, script: str | None = None, **kwargs: Any) -> Any:
        if script is not None:
            self.init_scripts.append(script)

    @override
    async def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)
        if self._goto_failures > 0:
            self._goto_failures -= 1
            raise PlaywrightError("navigation timeout")
        return None

    @override
    async def content(self) -> str:
        if not self._contents:
            raise AssertionError("page content read more times than scripted")
        result = self._contents.pop(0)
        self._reads += 1
        if self._on_content is not None:
            self._on_content(self._reads)
        if isinstance(result, Exception):
            raise result
        return result

    @override
    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        self.evaluate_calls.append(expression)
        if not self._evaluates:
            raise AssertionError("page evaluate called more times than scripted")
        result = self._evaluates.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @override
    def locator(self, selector: str, **kwargs: Any) -> Any:
        return FakeLocator(self, selector)

    @override
    def get_by_placeholder(self, text: str | re.Pattern[str], **kwargs: Any) -> Any:
        return FakeLocator(self, f"placeholder:{text}")

    @override
    def get_by_role(self, role: str, **kwargs: Any) -> Any:
        return FakeLocator(self, f"role:{role}")


class HangingPage(FakePage):
    def __init__(
        self,
        contents: Sequence[str | Exception] = (),
        evaluates: Sequence[Any] = (),
        *,
        hang_content: bool = True,
        hang_evaluate: bool = True,
        hang_seconds: float = 2.0,
        **script: Unpack[PageScript],
    ) -> None:
        super().__init__(contents, evaluates, **script)
        self._hang_content = hang_content
        self._hang_evaluate = hang_evaluate
        self._hang_seconds = hang_seconds
        self.cancelled = False

    async def _hang(self) -> None:
        try:
            await asyncio.sleep(self._hang_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("hanging page call returned")

    @override
    async def content(self) -> str:
        if not self._hang_content:
            return await super().content()
        await self._hang()
        raise AssertionError("unreachable")

    @override
    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        if not self._hang_evaluate:
            return await super().evaluate(expression, arg)
        self.evaluate_calls.append(expression)
        await self._hang()
        raise AssertionError("unreachable")


class FakePageSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.opened = 0
        self.closed = 0
        self.open_kwargs: list[dict[str, Any]] = []

    @asynccontextmanager
    async def open(self, **kwargs: Any) -> AsyncIterator[FakePage]:
        self.opened += 1
        self.open_kwargs.append(kwargs)
        try:
            yield self.page
        finally:
            self.page.shut()
            self.closed += 1


class FakeWarmupPage:
    def __init__(self, fail_on: Iterable[str] = ()) -> None:
        self.visited: list[str] = []
        self.fail_on = set(fail_on)
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        del kwargs
        self.visited.append(url)
        if url in self.fail_on:
            raise PlaywrightError(f"warmup failed for {url}")

    async def wait_for_timeout(self, delay: float) -> None:
        del delay
        return None

    async def close(self) -> None:
        self.closed = True


class FakeWarmupContext:
    def __init__(self, page: FakeWarmupPage) -> None:
        self.page = page

    async def new_page(self) -> FakeWarmupPage:
        return self.page


class FakeBrowserContext:
    def __init__(
        self,
        *,
        fail_on_init: bool = False,
        cancel_on_init: bool = False,
        fail_on_new_page: bool = False,
    ) -> None:
        self.fail_on_init = fail_on_init
        self.cancel_on_init = cancel_on_init
        self.fail_on_new_page = fail_on_new_page
        self.closed = 0

    async def add_init_script(self, script: str) -> None:
        del script
        if self.cancel_on_init:
            raise asyncio.CancelledError
        if self.fail_on_init:
            raise RuntimeError("init script failed")

    async def new_page(self) -> FakeWarmupPage:
        if self.fail_on_new_page:
            raise RuntimeError("new page failed")
        return FakeWarmupPage()

    async def close(self) -> None:
        self.closed += 1


def meet_page(
    contents: Sequence[str | Exception] = (),
    evaluates: Sequence[Any] = (),
    **script: Unpack[PageScript],
) -> FakePage:
    from fakeyou.meet.watchdog import DIALOG_SELECTOR as WATCHDOG_DIALOG_SELECTOR

    counts = {
        DIALOG_SELECTOR: [0],
        WATCHDOG_DIALOG_SELECTOR: [0],
        NAME_SELECTOR: [1],
        ASK_SELECTOR: [1],
    }
    counts.update(script.get("counts", {}))
    script["counts"] = counts
    return FakePage(contents, evaluates, **script)
