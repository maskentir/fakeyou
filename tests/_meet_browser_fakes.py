from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from typing import Any, TypedDict, Unpack, override

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from fakeyou.meet.meet import (
    ACCESSIBLE_NAME_PROBE,
    MIC_DISABLED_SELECTOR,
    MIC_ENABLED_SELECTOR,
    VISIBLE_BODY_SELECTOR,
    VISIBLE_CONTROLS_SELECTOR,
    control_selector,
)

LOBBY = '<input placeholder="Your name"><button>Ask to join</button>'
IN_CALL = '<button aria-label="Leave call"></button><button>Mute microphone</button>'
DENIED = "<h1>You can't join this video call</h1>"
CALL_ENDED = '<div data-call-ended="true"></div><h1>You can\'t join this video call</h1>'
EXPLICIT_DENIED = "<h1>Someone in the call denied your request to join</h1>"
REMOVED = "<h1>You've been removed from the call</h1>"
ENDED = "<h1>The call has ended</h1>"
WAITING = LOBBY + "<div>Asking to be let in...</div>"
LEAVE_AND_WAITING = IN_CALL + "<div>Asking to be let in...</div>"

DIALOG_SELECTOR = "text=Continue without microphone"
NAME_SELECTOR = "placeholder:Your name"
ASK_SELECTOR = control_selector("Ask to join")
JOIN_NOW_SELECTOR = control_selector("Join now")
GUEST_MODE_SELECTOR = control_selector("Join as guest")
ASK_WITHOUT_CAMERA_SELECTOR = control_selector("Ask to join without camera")
CONTROL_NAMES = {
    ASK_SELECTOR: "Ask to join",
    JOIN_NOW_SELECTOR: "Join now",
    GUEST_MODE_SELECTOR: "Join as guest",
    ASK_WITHOUT_CAMERA_SELECTOR: "Ask to join without camera",
}
LEAVE_SELECTOR = 'button[aria-label="Leave call"]'
MIC_ON_SELECTOR = MIC_ENABLED_SELECTOR
MIC_OFF_SELECTOR = MIC_DISABLED_SELECTOR
CAM_ON_SELECTOR = 'button[aria-label^="Turn off camera" i]'
CAM_OFF_SELECTOR = 'button[aria-label^="Turn on camera" i]'

NO_CAPTCHA = False
LIVE_CAPTCHA = True


_INVISIBLE_TAGS = frozenset({"script", "style", "template", "head", "title", "noscript"})
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


_BLOCKS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "button",
        "div",
        "dl",
        "dt",
        "dd",
        "fieldset",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "td",
        "ul",
    }
)


class RenderedDom(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.controls: list[str] = []
        self._line: list[str] = []
        self._stack: list[tuple[str, bool, list[str] | None, str]] = []

    def _hidden(self) -> bool:
        return any(hidden for _tag, hidden, _texts, _label in self._stack)

    def _flush(self) -> None:
        if self._line:
            self.chunks.append(" ".join(self._line))
            self._line = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCKS:
            self._flush()
        if tag in _VOID:
            return
        values = {name: value or "" for name, value in attrs}
        style = "".join(values.get("style", "").casefold().split())
        hidden = (
            tag in _INVISIBLE_TAGS
            or "hidden" in values
            or "display:none" in style
            or "visibility:hidden" in style
        )
        control = tag == "button" or values.get("role") == "button"
        self._stack.append((tag, hidden, [] if control else None, values.get("aria-label", "")))

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCKS:
            self._flush()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                hidden_above = any(entry[1] for entry in self._stack[: index + 1])
                _tag, _hidden, texts, label = self._stack[index]
                if texts is not None and not hidden_above:
                    self.controls.append((label or " ".join(texts)).strip())
                del self._stack[index:]
                return

    @override
    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or self._hidden():
            return
        self._line.append(text)
        for _tag, _hidden, texts, _label in self._stack:
            if texts is not None:
                texts.append(text)

    @override
    def close(self) -> None:
        super().close()
        self._flush()


def rendered(content: str | None) -> RenderedDom:
    dom = RenderedDom()
    dom.feed(content or "")
    dom.close()
    return dom


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


class FakeNode:
    def __init__(self, key: str, name: str) -> None:
        self.key = key
        self.name = name


class FakeElementHandle:
    def __init__(self, page: FakePage, selector: str, node: FakeNode | None) -> None:
        self._page = page
        self._selector = selector
        self._node = node

    @property
    def node_key(self) -> str:
        return self._node.key if self._node is not None else ""

    def _detached(self) -> bool:
        if self._node is None:
            return False
        current = self._page.nodes.get(self._selector, [])
        return not any(entry is self._node for entry in current)

    async def evaluate(self, expression: str, arg: Any = None, **kwargs: Any) -> Any:
        if expression == ACCESSIBLE_NAME_PROBE:
            if self._node is not None:
                return self._node.name
            return self._page.names.get(self._selector, CONTROL_NAMES.get(self._selector, ""))
        return await FakeLocator(self._page, self._selector).evaluate(expression, arg, **kwargs)

    async def is_visible(self) -> bool:
        if self._detached():
            return False
        value = self._page.visible.get(self._selector, True)
        if isinstance(value, Exception):
            raise value
        return value

    async def is_enabled(self) -> bool:
        if self._detached():
            return False
        return self._selector not in self._page.disabled

    async def click(self, **kwargs: Any) -> None:
        del kwargs
        if self._detached():
            raise PlaywrightError(f"element is not attached: {self._selector}")
        self._page.record_click(self._selector, self.node_key)

    async def hover(self, **kwargs: Any) -> None:
        del kwargs
        if self._detached():
            raise PlaywrightError(f"element is not attached: {self._selector}")


class FakeLocator:
    def __init__(self, page: FakePage, selector: str, index: int | None = None) -> None:
        self._page = page
        self.selector = selector
        self._index = index

    @property
    def first(self) -> FakeLocator:
        return self

    def filter(self, **kwargs: Any) -> FakeLocator:
        del kwargs
        return self

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(self._page, self.selector, index)

    def _attached_node(self) -> FakeNode | None:
        if self._index is None:
            return None
        nodes = self._page.nodes.get(self.selector)
        if nodes is None or not 0 <= self._index < len(nodes):
            return None
        return nodes[self._index]

    async def element_handle(self) -> FakeElementHandle | None:
        node = self._attached_node()
        if node is not None:
            return FakeElementHandle(self._page, self.selector, node)
        if self.selector in self._page.nodes:
            return None
        return FakeElementHandle(self._page, self.selector, None)

    async def count(self) -> int:
        if self._index is not None:
            if self.selector in self._page.nodes:
                return 1 if self._attached_node() is not None else 0
            return 1
        nodes = self._page.nodes.get(self.selector)
        if nodes is not None:
            return len(nodes)
        scripted = self._page.counts.get(self.selector, [1])
        if len(scripted) > 1:
            return scripted.pop(0)
        return scripted[0]

    async def evaluate(self, expression: str, arg: Any = None, **kwargs: Any) -> Any:
        del arg
        del kwargs
        if expression == ACCESSIBLE_NAME_PROBE:
            node = self._attached_node()
            if node is not None:
                return node.name
            return self._page.names.get(self.selector, CONTROL_NAMES.get(self.selector, ""))
        del expression
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
        if self._index is not None and self.selector in self._page.nodes:
            return bool(value) and self._attached_node() is not None
        return value

    async def is_editable(self) -> bool:
        return self.selector not in self._page.read_only

    async def is_enabled(self) -> bool:
        if self._index is not None and self.selector in self._page.nodes:
            return self._attached_node() is not None and self.selector not in self._page.disabled
        return self.selector not in self._page.disabled

    async def evaluate_all(self, expression: str) -> list[Any]:
        del expression
        scripted = self._page.label_script.get(self.selector)
        if scripted:
            return scripted.pop(0) if len(scripted) > 1 else scripted[0]
        if self.selector in self._page.labels:
            return self._page.labels[self.selector]
        if self.selector == VISIBLE_BODY_SELECTOR:
            return rendered(self._page.last_content).chunks
        if self.selector == VISIBLE_CONTROLS_SELECTOR:
            return rendered(self._page.last_content).controls
        return []

    async def click(self, **kwargs: Any) -> None:
        del kwargs
        node = self._attached_node()
        if self._index is not None and self.selector in self._page.nodes and node is None:
            raise PlaywrightError(f"no element at index: {self.selector}")
        self._page.record_click(self.selector, node.key if node is not None else "")


class PageScript(TypedDict, total=False):
    counts: dict[str, list[int]]
    locator_evaluates: dict[str, list[Any]]
    labels: dict[str, list[str]]
    label_script: dict[str, list[list[str]]]
    click_failures: Iterable[str]
    click_flaky: dict[str, int]
    wait_flaky: dict[str, int]
    type_failures: Iterable[str]
    goto_failures: int
    final_url: str
    visible: dict[str, bool | Exception]
    names: dict[str, str]
    nodes: dict[str, list[str]]
    disabled: Iterable[str]
    reveal_on_click: dict[str, str]
    wait_failures: Iterable[str]
    read_only: Iterable[str]
    names: dict[str, str]
    nodes: dict[str, list[str]]
    disabled: Iterable[str]
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
        self.last_content: str | None = None
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
        self.names = dict(script.get("names", {}))
        self.nodes: dict[str, list[FakeNode]] = {}
        self._node_counter = 0
        for selector, names in script.get("nodes", {}).items():
            self.nodes[selector] = [self._next_node(selector, name) for name in names]
        self.disabled = set(script.get("disabled", ()))
        self.labels = dict(script.get("labels", {}))
        self.label_script = {
            key: [list(value) for value in values]
            for key, values in script.get("label_script", {}).items()
        }
        self.reveal_on_click = dict(script.get("reveal_on_click", {}))
        self.wait_failures = set(script.get("wait_failures", ()))
        self.read_only = set(script.get("read_only", ()))
        self.names = dict(script.get("names", {}))
        self.nodes: dict[str, list[FakeNode]] = {}
        self._node_counter = 0
        for selector, names in script.get("nodes", {}).items():
            self.nodes[selector] = [self._next_node(selector, name) for name in names]
        self.disabled = set(script.get("disabled", ()))
        self.type_failures = set(script.get("type_failures", ()))
        self._goto_failures = script.get("goto_failures", 0)
        self._final_url = script.get("final_url")
        self.visited: list[str] = []
        self.init_scripts: list[str] = []
        self.clicked: list[str] = []
        self.clicked_nodes: list[tuple[str, str]] = []
        self.typed: list[str] = []
        self.waited: list[str] = []
        self.evaluate_calls: list[str] = []
        self.listeners: dict[str, list[Any]] = {}
        self.page_closed = False
        self._keyboard: Any = FakeKeyboard(self)

    def _next_node(self, selector: str, name: str) -> FakeNode:
        node = FakeNode(f"{selector}#{self._node_counter}", name)
        self._node_counter += 1
        return node

    def dom_insert(self, selector: str, position: int, name: str) -> str:
        node = self._next_node(selector, name)
        self.nodes.setdefault(selector, []).insert(position, node)
        return node.key

    def dom_remove(self, selector: str, key: str) -> None:
        self.nodes[selector] = [entry for entry in self.nodes.get(selector, []) if entry.key != key]

    def record_click(self, selector: str, node_key: str) -> None:
        self.clicked.append(selector)
        self.clicked_nodes.append((selector, node_key))
        remaining = self.click_flaky.get(selector, 0)
        if remaining > 0:
            self.click_flaky[selector] = remaining - 1
            raise PlaywrightError(f"click failed: {selector}")
        if selector in self.click_failures:
            raise PlaywrightError(f"click failed: {selector}")
        revealed = self.reveal_on_click.get(selector)
        if revealed is not None:
            self.visible[revealed] = True

    @override
    def on(self, event: Any, f: Any) -> None:
        if inspect.ismethod(f):
            setattr(f.__self__, f"_pw_impl_instance_{f.__name__}", f)
        else:
            f._pw_impl_instance_ = f
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
        if isinstance(result, str):
            self.last_content = result
        if self._on_content is not None:
            self._on_content(self._reads)
        if isinstance(result, Exception):
            raise result
        return result

    @override
    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        self.evaluate_calls.append(expression)
        if not self._evaluates:
            if "enumerateDevices" in expression:
                return ["audioinput", "videoinput", "audiooutput"]
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
        GUEST_MODE_SELECTOR: [0],
        ASK_WITHOUT_CAMERA_SELECTOR: [0],
    }
    counts.update(script.get("counts", {}))
    script["counts"] = counts
    return FakePage(contents, evaluates, **script)
