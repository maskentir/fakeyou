import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import Page

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.meet_action_ledger import (
    LedgerError,
    LedgerResult,
    MeetActionAddress,
    MeetActionCall,
    MeetActionEffects,
    MeetActionKind,
    MeetActionLedger,
    execute_meet_action,
)
from fakeyou.meet.meet_actions import (
    DEFAULT_MEET_ACTION_SIGNALS,
    MeetActionResult,
    MeetActionSignals,
    leave_call,
    lower_hand,
    mute_microphone,
    observe_chat_echoed,
    observe_hand_lowered,
    observe_hand_raised,
    observe_muted,
    observe_unmuted,
    raise_hand,
    send_chat,
    send_reaction,
    unmute_microphone,
)
from fakeyou.memory.memory import PREFETCH_EVIDENCE_PREFIX
from fakeyou.observability.faults import capture_error, until_stopped
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
        self.on_chat_sent: Callable[[str, str], None] | None = None
        self.on_leave: Callable[[], None] | None = None
        self._signals = DEFAULT_MEET_ACTION_SIGNALS
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._monotonic: Callable[[], float] = time.monotonic
        self._page: Page | None = None
        self._stop: asyncio.Event | None = None
        self._generation = 0
        self._closed = False
        self._wake = asyncio.Event()
        self._pending: dict[asyncio.Task[LedgerResult | None], tuple[MeetActionKind, str]] = {}
        self._tasks_by_id: dict[str, asyncio.Task[LedgerResult | None]] = {}
        self._leave_release: asyncio.Event | None = None
        self._leave_action_id: str | None = None
        self._left = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def left(self) -> bool:
        return self._left

    @property
    def leave_scheduled(self) -> bool:
        return self._leave_action_id is not None

    def status(self, action_id: str) -> str:
        try:
            ledger = MeetActionLedger.load(self._record_path, self._meeting_id)
        except (LedgerError, OSError):
            return "unknown"
        intent = ledger.get(action_id)
        return intent.state if intent is not None else "unknown"

    def request(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> str:
        if kind == "send_chat":
            self._check_chat(action_id, text)
        return self._start(kind, action_id, text)

    def _start(
        self,
        kind: MeetActionKind,
        action_id: str,
        text: str,
        *,
        leave_release: asyncio.Event | None = None,
    ) -> str:
        task = asyncio.create_task(
            self._execute(kind, action_id, text, self._generation, leave_release=leave_release)
        )
        self._pending[task] = (kind, action_id)
        self._tasks_by_id[action_id] = task
        task.add_done_callback(self._finish)
        return action_id

    async def settle(
        self, kind: MeetActionKind, *, action_id: str, text: str = ""
    ) -> LedgerResult | None:
        self.request(kind, action_id=action_id, text=text)
        task = self._tasks_by_id[action_id]
        try:
            outcome, _error = await capture_error(lambda: asyncio.shield(task))
        except asyncio.CancelledError:
            task.cancel()
            raise
        return outcome

    def schedule_leave(self, *, action_id: str) -> str:
        self.cancel_leave()
        release = asyncio.Event()
        self._leave_release = release
        self._leave_action_id = action_id
        return self._start("leave", action_id, "", leave_release=release)

    def release_leave(self) -> None:
        if self._leave_release is not None:
            self._leave_release.set()

    def cancel_leave(self) -> None:
        action_id = self._leave_action_id
        self._leave_release = None
        self._leave_action_id = None
        if action_id is None:
            return
        task = self._tasks_by_id.pop(action_id, None)
        if task is None or task.done():
            return
        self._pending.pop(task, None)
        self._reject("leave", action_id, "stale_cancelled")
        task.cancel()

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
        self._leave_release = None
        self._leave_action_id = None
        for task, (kind, action_id) in list(self._pending.items()):
            self._pending.pop(task, None)
            self._tasks_by_id.pop(action_id, None)
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

    def _finish(self, task: asyncio.Task[LedgerResult | None]) -> None:
        meta = self._pending.pop(task, None)
        if meta is not None:
            self._tasks_by_id.pop(meta[1], None)
            if meta[1] == self._leave_action_id:
                self._leave_release = None
                self._leave_action_id = None
        if meta is None or task.cancelled():
            return
        error = task.exception()
        if error is not None:
            kind, action_id = meta
            self._fail(kind, action_id, type(error).__name__)

    def _reject(self, kind: MeetActionKind, action_id: str, reason: str) -> None:
        if self._journal is not None:
            self._journal.meet_action_rejected(action=kind, action_id=action_id, reason=reason)

    def _settle(
        self, kind: MeetActionKind, action_id: str, text: str, outcome: LedgerResult | None
    ) -> None:
        if outcome is None:
            self._reject(kind, action_id, "stale_cancelled")
        elif kind == "send_chat" and outcome.succeeded and self.on_chat_sent is not None:
            self.on_chat_sent(action_id, text)
        elif kind == "leave" and outcome.succeeded:
            self._left = True
            if self.on_leave is not None:
                self.on_leave()

    def _fail(self, kind: MeetActionKind, action_id: str, reason: str) -> None:
        if self._journal is not None:
            self._journal.meet_action_failed(
                action=kind, action_id=action_id, reason=reason, attempts=1
            )

    async def _act(self, kind: MeetActionKind, page: Page, text: str) -> MeetActionResult:
        signals, sleep, monotonic = self._signals, self._sleep, self._monotonic
        match kind:
            case "send_chat":
                return await send_chat(page, text, signals, sleep, monotonic)
            case "raise_hand":
                return await raise_hand(page, signals, sleep, monotonic)
            case "lower_hand":
                return await lower_hand(page, signals, sleep, monotonic)
            case "react":
                return await send_reaction(page, text, signals, sleep, monotonic)
            case "mute":
                return await mute_microphone(page, signals, sleep, monotonic)
            case "unmute":
                return await unmute_microphone(page, signals, sleep, monotonic)
            case "leave":
                return await leave_call(page, signals, sleep, monotonic)

    async def _execute(
        self,
        kind: MeetActionKind,
        action_id: str,
        text: str,
        generation: int,
        *,
        leave_release: asyncio.Event | None = None,
    ) -> LedgerResult | None:
        await self._wake.wait()
        page = self._page
        stop = self._stop
        if generation != self._generation or self._closed or page is None or stop is None:
            self._reject(kind, action_id, "stale_cancelled")
            return None
        if leave_release is not None:
            released = await until_stopped(leave_release.wait(), stop)
            if released is None:
                self._reject(kind, action_id, "stale_cancelled")
                return None
        if generation != self._generation or self._closed or stop.is_set():
            self._reject(kind, action_id, "stale_cancelled")
            return None
        if kind in ("send_chat", "react"):
            params = text
            chars: int | None = len(text) if kind == "send_chat" else None
        else:
            params = kind
            chars = None
        box: dict[str, MeetActionResult | None] = {"result": None}

        async def act() -> MeetActionResult:
            result = await self._act(kind, page, text)
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
            return None
        self._settle(kind, action_id, text, outcome)
        return outcome


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
    if kind == "mute":
        return await observe_muted(page, signals)
    if kind == "unmute":
        return await observe_unmuted(page, signals)
    if kind in ("react", "leave"):
        return acted
    return await observe_hand_lowered(page, signals)


class MeetChatChannel:
    def __init__(self, dispatcher: MeetActionDispatcher) -> None:
        self._dispatcher = dispatcher

    async def send(self, text: str, *, transcript_id: str) -> bool:
        try:
            outcome = await self._dispatcher.settle("send_chat", action_id=transcript_id, text=text)
        except FakeYouError:
            return False
        return outcome is not None and outcome.succeeded
