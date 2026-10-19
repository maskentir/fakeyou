import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import Page

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.meet_action_ledger import (
    MeetActionAddress,
    MeetActionCall,
    MeetActionEffects,
    MeetActionKind,
    execute_meet_action,
)
from fakeyou.meet.meet_actions import (
    DEFAULT_MEET_ACTION_SIGNALS,
    MeetActionResult,
    MeetActionSignals,
    lower_hand,
    observe_chat_echoed,
    observe_hand_lowered,
    observe_hand_raised,
    raise_hand,
    send_chat,
)
from fakeyou.memory.memory import PREFETCH_EVIDENCE_PREFIX
from fakeyou.observability.faults import until_stopped
from fakeyou.observability.journal import Journal

MEET_CHAT_MAX_CHARS = 500
_MEET_CHAT_WINDOW_SECONDS = 60.0


class MeetActionDispatcher:
    def __init__(
        self,
        record_path: Path,
        *,
        meeting_id: str,
        enabled: bool,
        chat_per_minute: int,
        journal: Journal | None = None,
    ) -> None:
        self._record_path = record_path
        self._meeting_id = meeting_id
        self._enabled = enabled
        self._chat_per_minute = chat_per_minute
        self._chat_sent: list[float] = []
        self._journal = journal
        self._signals = DEFAULT_MEET_ACTION_SIGNALS
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._monotonic: Callable[[], float] = time.monotonic
        self._page: Page | None = None
        self._stop: asyncio.Event | None = None
        self._generation = 0
        self._closed = False
        self._wake = asyncio.Event()
        self._pending: dict[asyncio.Task[None], tuple[MeetActionKind, str]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def closed(self) -> bool:
        return self._closed

    def request(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> str:
        if kind == "send_chat":
            self._check_chat(action_id, text)
        task = asyncio.create_task(self._execute(kind, action_id, text, self._generation))
        self._pending[task] = (kind, action_id)
        task.add_done_callback(self._finish)
        return action_id

    def _check_chat(self, action_id: str, text: str) -> None:
        if len(text) > MEET_CHAT_MAX_CHARS:
            self._reject("send_chat", action_id, "chat_too_long")
            raise FakeYouError(ErrorCode.MEET_CHAT_TOO_LONG, limit=MEET_CHAT_MAX_CHARS)
        if PREFETCH_EVIDENCE_PREFIX in text:
            self._reject("send_chat", action_id, "chat_evidence_rejected")
            raise FakeYouError(ErrorCode.MEET_CHAT_EVIDENCE_REJECTED)
        now = self._monotonic()
        self._chat_sent = [
            sent for sent in self._chat_sent if now - sent < _MEET_CHAT_WINDOW_SECONDS
        ]
        if len(self._chat_sent) >= self._chat_per_minute:
            self._reject("send_chat", action_id, "chat_rate_limited")
            raise FakeYouError(ErrorCode.MEET_CHAT_RATE_LIMITED, limit=self._chat_per_minute)
        self._chat_sent.append(now)

    def bind_page(
        self,
        page: Page,
        *,
        sleep: Callable[[float], Awaitable[None]],
        monotonic: Callable[[], float],
        stop: asyncio.Event,
    ) -> None:
        self._page = page
        self._sleep = sleep
        self._monotonic = monotonic
        self._stop = stop
        self._wake.set()

    def cancel_pending(self) -> None:
        self._generation += 1
        for task, (kind, action_id) in list(self._pending.items()):
            self._pending.pop(task, None)
            if task.done():
                continue
            self._reject(kind, action_id, "stale_cancelled")
            task.cancel()
        self._wake.set()

    def close(self) -> None:
        self._closed = True
        self.cancel_pending()

    async def drain(self) -> None:
        await asyncio.gather(*list(self._pending), return_exceptions=True)

    def _finish(self, task: asyncio.Task[None]) -> None:
        meta = self._pending.pop(task, None)
        if meta is None or task.cancelled():
            return
        error = task.exception()
        if error is not None:
            kind, action_id = meta
            self._fail(kind, action_id, type(error).__name__)

    def _reject(self, kind: MeetActionKind, action_id: str, reason: str) -> None:
        if self._journal is not None:
            self._journal.meet_action_rejected(action=kind, action_id=action_id, reason=reason)

    def _fail(self, kind: MeetActionKind, action_id: str, reason: str) -> None:
        if self._journal is not None:
            self._journal.meet_action_failed(
                action=kind, action_id=action_id, reason=reason, attempts=1
            )

    async def _execute(
        self, kind: MeetActionKind, action_id: str, text: str, generation: int
    ) -> None:
        await self._wake.wait()
        page = self._page
        stop = self._stop
        if generation != self._generation or self._closed or page is None or stop is None:
            self._reject(kind, action_id, "stale_cancelled")
            return
        if stop.is_set():
            self._reject(kind, action_id, "stale_cancelled")
            return
        if kind == "send_chat":
            params = text
            chars: int | None = len(text)
        else:
            params = kind
            chars = None
        box: dict[str, MeetActionResult | None] = {"result": None}

        async def act() -> MeetActionResult:
            if kind == "send_chat":
                result = await send_chat(page, text, self._signals, self._sleep, self._monotonic)
            elif kind == "raise_hand":
                result = await raise_hand(page, self._signals, self._sleep, self._monotonic)
            else:
                result = await lower_hand(page, self._signals, self._sleep, self._monotonic)
            box["result"] = result
            return result

        async def observe() -> bool:
            return await readback_effect(
                kind, page, text, self._signals, acted=box["result"] is not None
            )

        try:
            outcome = await until_stopped(
                execute_meet_action(
                    self._record_path,
                    MeetActionAddress(self._meeting_id, action_id),
                    MeetActionCall(kind, params, chars),
                    MeetActionEffects(act, observe),
                    self._journal,
                ),
                stop,
            )
        except FakeYouError as error:
            self._fail(kind, action_id, type(error).__name__)
            return
        if outcome is None:
            self._reject(kind, action_id, "stale_cancelled")


async def readback_effect(
    kind: MeetActionKind,
    page: Page,
    text: str,
    signals: MeetActionSignals,
    *,
    acted: bool,
) -> bool:
    if kind == "send_chat":
        return acted and await observe_chat_echoed(page, text, signals)
    if kind == "raise_hand":
        return await observe_hand_raised(page, signals)
    return await observe_hand_lowered(page, signals)
