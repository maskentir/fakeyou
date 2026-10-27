import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from playwright.async_api import Page

from fakeyou.meet.command_ledger import CommandLedger, CommandRecord
from fakeyou.meet.meet_actions import (
    DEFAULT_MEET_ACTION_SIGNALS,
    MeetActionResult,
    MeetActionSignals,
    count_chat_echoes,
    leave_call,
    lower_hand,
    mute_microphone,
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
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.session.commands import (
    MAX_TOGGLE_ATTEMPTS,
    TOGGLES,
    CommandKind,
    CommandOutcome,
    CommandSpec,
    CommandState,
    command_id,
)

MEET_CHAT_MAX_CHARS = 500
CHAT_WINDOW_SECONDS = 60.0
ECHO_TIMEOUT_SECONDS = 10.0
ECHO_POLL_SECONDS = 0.5
SETTLE_ON_CLOSE_SECONDS = 15.0


@dataclass(slots=True)
class _Binding:
    page: Page
    sleep: Callable[[float], Awaitable[None]]
    monotonic: Callable[[], float]
    stop: asyncio.Event


def _outcome(record: CommandRecord) -> CommandOutcome:
    return CommandOutcome(record.command_id, record.kind, record.state, record.detail)


class CommandRunner:
    def __init__(
        self,
        record_path: Path,
        *,
        meeting_id: str,
        kernel: KernelPort,
        chat_per_minute: int,
        journal: Journal | None = None,
    ) -> None:
        self._ledger = CommandLedger(record_path, meeting_id)
        self._kernel = kernel
        self._chat_per_minute = chat_per_minute
        self._journal = journal
        self._signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS
        self.echo_timeout_seconds = ECHO_TIMEOUT_SECONDS
        self._chat_sent: list[float] = []
        self._binding: _Binding | None = None
        self._page_lease = asyncio.Lock()
        self._closed = False
        self._left = False
        self._inflight: set[asyncio.Task[CommandRecord]] = set()
        self.on_settled: Callable[[CommandRecord], None] | None = None
        self.on_left: Callable[[], None] | None = None

    @property
    def left(self) -> bool:
        return self._left

    @property
    def available(self) -> bool:
        world = self._kernel.state.world
        return (
            not self._closed
            and self._binding is not None
            and not self._binding.stop.is_set()
            and world.call_state == "in_call"
        )

    def bind_page(
        self,
        page: Page,
        *,
        sleep: Callable[[float], Awaitable[None]],
        monotonic: Callable[[], float],
        stop: asyncio.Event,
    ) -> None:
        self._binding = _Binding(page, sleep, monotonic, stop)

    async def close(self) -> None:
        self._closed = True
        if not self._inflight:
            return
        try:
            async with asyncio.timeout(SETTLE_ON_CLOSE_SECONDS):
                await asyncio.gather(*self._inflight, return_exceptions=True)
        except TimeoutError:
            for task in self._inflight:
                task.cancel()

    def recover(self) -> tuple[CommandRecord, ...]:
        closed = self._ledger.recover(self._kernel.attempt_id)
        for record in closed:
            self._settled(record)
        return closed

    def recent(self, limit: int = 5) -> tuple[CommandOutcome, ...]:
        records = sorted(self._ledger.records(), key=lambda record: record.updated_at)
        return tuple(_outcome(record) for record in records[-limit:])

    async def run(
        self, spec: CommandSpec, *, plan_id: str, ordinal: int, actor: str
    ) -> CommandOutcome:
        record = self._ledger.accept(
            CommandRecord(
                command_id=command_id(self._kernel.attempt_id, plan_id, ordinal, spec),
                kind=spec.kind,
                attempt_id=self._kernel.attempt_id,
                plan_id=plan_id,
                actor=actor,
                browser_epoch=self._kernel.state.world.browser_epoch,
                verification=spec.verification,
                text=spec.text,
            )
        )
        if record.terminal:
            return _outcome(record)
        if self._journal is not None:
            self._journal.meet_action_requested(
                action=spec.kind,
                action_id=record.command_id,
                actor=actor,
                chars=len(spec.text) if spec.kind == "send_chat" else None,
                plan_id=plan_id,
            )
        task = asyncio.create_task(self._leased(record))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return _outcome(await asyncio.shield(task))

    async def _leased(self, record: CommandRecord) -> CommandRecord:
        async with self._page_lease:
            return await self._drive(record)

    async def _drive(self, record: CommandRecord) -> CommandRecord:
        refusal = self._refusal(record.spec)
        if refusal is not None:
            return self._finish(record, "refused", refusal)
        blocked = self._blocked(record)
        if blocked is not None or self._binding is None:
            return self._finish(record, "cancelled_before_boundary", blocked or "no_page")
        record = self._ledger.move(record.command_id, "ready")
        if record.kind in TOGGLES:
            return await self._toggle(record, self._binding)
        return await self._once(record, self._binding)

    def _refusal(self, spec: CommandSpec) -> str | None:
        if spec.kind != "send_chat":
            return None
        if len(spec.text) > MEET_CHAT_MAX_CHARS:
            return "chat_too_long"
        if PREFETCH_EVIDENCE_PREFIX in spec.text or not spec.text.strip():
            return "chat_content_rejected"
        now = time.monotonic()
        self._chat_sent = [sent for sent in self._chat_sent if now - sent < CHAT_WINDOW_SECONDS]
        if len(self._chat_sent) >= self._chat_per_minute:
            return "chat_rate_limited"
        self._chat_sent.append(now)
        return None

    def _blocked(self, record: CommandRecord) -> str | None:
        world = self._kernel.state.world
        if self._closed or self._left:
            return "closed"
        if self._binding is None:
            return "no_page"
        if self._binding.stop.is_set():
            return "stopping"
        if world.browser_epoch != record.browser_epoch:
            return "browser_epoch_changed"
        if world.call_state != "in_call":
            return "not_in_call"
        return None

    async def _toggle(self, record: CommandRecord, binding: _Binding) -> CommandRecord:
        readback = partial(self._readback, record.kind, binding)
        reached, error = await capture_error(readback)
        if error is not None:
            return self._finish(record, "cancelled_before_boundary", "readback_failed")
        if reached:
            return self._finish(record, "effect_observed", "already_in_target_state")
        for attempt in range(MAX_TOGGLE_ATTEMPTS):
            if attempt and self._blocked(record) is not None:
                return self._finish(record, "failed_after_boundary", "blocked_before_retry")
            record = self._boundary(record)
            await capture_error(partial(self._act, record.spec, binding))
            reached, error = await capture_error(readback)
            if error is not None:
                return self._finish(record, "unknown", "readback_failed")
            if reached:
                return self._finish(record, "effect_observed", "target_state_observed")
        return self._finish(record, "failed_after_boundary", "target_state_not_reached")

    async def _once(self, record: CommandRecord, binding: _Binding) -> CommandRecord:
        before = await self._echo_count(record, binding)
        record = self._boundary(record)
        result, error = await capture_error(partial(self._act, record.spec, binding))
        if error is not None:
            return self._finish(record, "unknown", f"act_raised:{type(error).__name__}")
        assert result is not None
        if not result.succeeded:
            return self._finish(record, "failed_after_boundary", result.detail)
        if record.kind == "send_chat":
            echoed = before is not None and await self._echoed(record.text, before, binding)
            state: CommandState = "effect_observed" if echoed else "unknown"
            return self._finish(record, state, "dom_echo" if echoed else "no_echo")
        finished = self._finish(record, "effect_unobserved", "acted")
        if record.kind == "leave":
            self._left = True
            if self.on_left is not None:
                self.on_left()
        return finished

    def _boundary(self, record: CommandRecord) -> CommandRecord:
        moved = self._ledger.move(record.command_id, "dispatching")
        if self._journal is not None:
            self._journal.meet_action_started(
                action=moved.kind, action_id=moved.command_id, attempt=moved.dispatches
            )
        return moved

    async def _echo_count(self, record: CommandRecord, binding: _Binding) -> int | None:
        if record.kind != "send_chat":
            return None
        count, error = await capture_error(
            partial(count_chat_echoes, binding.page, record.text, self._signals)
        )
        return None if error is not None else count

    async def _echoed(self, text: str, before: int, binding: _Binding) -> bool:
        deadline = binding.monotonic() + self.echo_timeout_seconds
        while True:
            count, error = await capture_error(
                partial(count_chat_echoes, binding.page, text, self._signals)
            )
            if error is None and count is not None and count > before:
                return True
            if binding.monotonic() >= deadline or binding.stop.is_set():
                return False
            await binding.sleep(ECHO_POLL_SECONDS)

    async def _act(self, spec: CommandSpec, binding: _Binding) -> MeetActionResult:
        page, signals, sleep, monotonic = (
            binding.page,
            self._signals,
            binding.sleep,
            binding.monotonic,
        )
        match spec.kind:
            case "send_chat":
                return await send_chat(page, spec.text, signals, sleep, monotonic)
            case "raise_hand":
                return await raise_hand(page, signals, sleep, monotonic)
            case "lower_hand":
                return await lower_hand(page, signals, sleep, monotonic)
            case "react":
                return await send_reaction(page, spec.text, signals, sleep, monotonic)
            case "mute":
                return await mute_microphone(page, signals, sleep, monotonic)
            case "unmute":
                return await unmute_microphone(page, signals, sleep, monotonic)
            case "leave":
                return await leave_call(page, signals, sleep, monotonic)

    async def _readback(self, kind: CommandKind, binding: _Binding) -> bool:
        page, signals = binding.page, self._signals
        match kind:
            case "raise_hand":
                return await observe_hand_raised(page, signals)
            case "lower_hand":
                return await observe_hand_lowered(page, signals)
            case "mute":
                return await observe_muted(page, signals)
            case "unmute":
                return await observe_unmuted(page, signals)
            case _:
                return False

    def _finish(self, record: CommandRecord, state: CommandState, detail: str) -> CommandRecord:
        finished = self._ledger.move(record.command_id, state, detail=detail)
        self._settled(finished)
        return finished

    def _settled(self, record: CommandRecord) -> None:
        journal = self._journal
        if journal is not None:
            if record.state in ("effect_observed", "effect_unobserved"):
                journal.meet_action_finished(
                    action=record.kind,
                    action_id=record.command_id,
                    observed=record.state == "effect_observed",
                    attempts=max(1, record.dispatches),
                    state=record.state,
                )
            elif record.state in ("refused", "cancelled_before_boundary"):
                journal.meet_action_rejected(
                    action=record.kind,
                    action_id=record.command_id,
                    reason=record.detail,
                    state=record.state,
                )
            else:
                journal.meet_action_failed(
                    action=record.kind,
                    action_id=record.command_id,
                    reason=record.detail,
                    attempts=max(1, record.dispatches),
                    state=record.state,
                )
        if self.on_settled is not None:
            self.on_settled(record)
