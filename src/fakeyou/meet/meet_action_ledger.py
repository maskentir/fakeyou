from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.meet_actions import ActionOutcome, MeetActionResult
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.ingestion_status import RecordLockError
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.lease import LeaseError, file_lease, leased

MAX_ACTION_ATTEMPTS = 3

MeetActionKind = Literal[
    "raise_hand", "lower_hand", "send_chat", "react", "mute", "unmute", "leave"
]
ActionState = Literal["requested", "acted", "observed_done", "refused", "failed"]

TERMINAL_ACTION_STATES = frozenset({"observed_done", "refused", "failed"})

ActEffect = Callable[[], Awaitable[MeetActionResult]]
ObserveEffect = Callable[[], Awaitable[bool]]


class LedgerError(FakeYouError):
    pass


class ActionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    kind: MeetActionKind
    params_hash: str
    authorized: bool
    state: ActionState = "requested"
    observed_before: bool | None = None
    observed_after: bool | None = None
    attempt: int = Field(default=1, ge=1)
    chars: int | None = Field(default=None, ge=0)
    at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class ActionLedgerFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meeting_id: str
    next_seq: int = Field(default=0, ge=0)
    intents: dict[str, ActionIntent] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LedgerResult:
    action_id: str
    outcome: ActionOutcome
    detail: str
    attempts: int
    duplicate: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"

    @property
    def refused(self) -> bool:
        return self.outcome == "refused"

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


def actions_path(record_path: Path) -> Path:
    return MeetingDir.from_record_path(record_path).actions


def ledger_lock_path(record_path: Path) -> Path:
    return MeetingDir.from_record_path(record_path).actions_lock


def _ledger_locked(_error: LeaseError, *, path: Path) -> RecordLockError:
    return RecordLockError(ErrorCode.ACTION_LEDGER_LOCKED, path=path)


@contextmanager
def ledger_lock(record_path: Path) -> Iterator[str]:
    path = ledger_lock_path(record_path)
    with leased(file_lease(path, purpose="meeting"), partial(_ledger_locked, path=path)) as token:
        yield token


def action_id_for(meeting_id: str, seq: int, tool_call_id: str | None = None) -> str:
    if tool_call_id:
        return tool_call_id
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{meeting_id}:{seq}"))


def params_hash_for(meeting_id: str, action_id: str, params: str) -> str:
    return sha256(f"{meeting_id}\x00{action_id}\x00{params}".encode()).hexdigest()


_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _loop_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[loop] = lock
    return lock


def _terminal_outcome(state: ActionState) -> ActionOutcome:
    if state == "observed_done":
        return "succeeded"
    if state == "refused":
        return "refused"
    return "failed"


class MeetActionLedger:
    def __init__(self, record_path: Path, data: ActionLedgerFile) -> None:
        self._record_path = record_path
        self._data = data

    @classmethod
    def load(cls, record_path: Path, meeting_id: str) -> MeetActionLedger:
        path = actions_path(record_path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls(record_path, ActionLedgerFile(meeting_id=meeting_id))
        except OSError as error:
            raise LedgerError(ErrorCode.ACTION_LEDGER_UNREADABLE, path=path) from error
        try:
            data = ActionLedgerFile.model_validate_json(raw)
        except ValueError as error:
            raise LedgerError(ErrorCode.ACTION_LEDGER_UNREADABLE, path=path) from error
        if data.meeting_id != meeting_id:
            raise LedgerError(ErrorCode.ACTION_LEDGER_MEETING_MISMATCH, path=path)
        return cls(record_path, data)

    @property
    def meeting_id(self) -> str:
        return self._data.meeting_id

    def get(self, action_id: str) -> ActionIntent | None:
        return self._data.intents.get(action_id)

    def pending(self) -> list[ActionIntent]:
        return sorted(
            (
                intent
                for intent in self._data.intents.values()
                if intent.state not in TERMINAL_ACTION_STATES
            ),
            key=lambda intent: intent.action_id,
        )

    def claim_seq(self) -> int:
        seq = self._data.next_seq
        self._data = self._data.model_copy(update={"next_seq": seq + 1})
        return seq

    def reserve_seq(self, seq: int) -> None:
        if seq >= self._data.next_seq:
            self._data = self._data.model_copy(update={"next_seq": seq + 1})

    def record(
        self,
        action_id: str,
        kind: MeetActionKind,
        params_hash: str,
        *,
        authorized: bool,
        chars: int | None = None,
    ) -> ActionIntent:
        existing = self._data.intents.get(action_id)
        if existing is not None:
            return existing
        intent = ActionIntent(
            action_id=action_id,
            kind=kind,
            params_hash=params_hash,
            authorized=authorized,
            chars=chars,
        )
        self._data = self._data.model_copy(
            update={"intents": {**self._data.intents, action_id: intent}}
        )
        return intent

    def mark(
        self,
        action_id: str,
        state: ActionState,
        *,
        observed_before: bool | None = None,
        observed_after: bool | None = None,
        attempt: int | None = None,
    ) -> ActionIntent:
        existing = self._data.intents.get(action_id)
        if existing is None:
            raise LedgerError(ErrorCode.ACTION_LEDGER_INTENT_UNKNOWN, action_id=action_id)
        if existing.state in TERMINAL_ACTION_STATES:
            return existing
        update: dict[str, object] = {"state": state}
        if observed_before is not None:
            update["observed_before"] = observed_before
        if observed_after is not None:
            update["observed_after"] = observed_after
        if attempt is not None:
            update["attempt"] = attempt
        marked = existing.model_copy(update=update)
        self._data = self._data.model_copy(
            update={"intents": {**self._data.intents, action_id: marked}}
        )
        return marked

    def save(self) -> None:
        path = actions_path(self._record_path)
        write_atomically(path, self._data.model_dump_json().encode("utf-8"))


@dataclass(frozen=True, slots=True)
class MeetActionAddress:
    meeting_id: str
    tool_call_id: str | None = None
    seq: int | None = None


@dataclass(frozen=True, slots=True)
class MeetActionCall:
    kind: MeetActionKind
    params: str = ""
    chars: int | None = None
    authorized: bool = True
    actor: str = "twin"


@dataclass(frozen=True, slots=True)
class MeetActionEffects:
    act: ActEffect
    observe: ObserveEffect


def _resolve_action_id(ledger: MeetActionLedger, address: MeetActionAddress) -> str:
    if address.tool_call_id:
        return address.tool_call_id
    if address.seq is not None:
        ledger.reserve_seq(address.seq)
        return action_id_for(address.meeting_id, address.seq)
    return action_id_for(address.meeting_id, ledger.claim_seq())


def _duplicate_result(
    existing: ActionIntent | None, action_id: str, journal: Journal | None
) -> LedgerResult | None:
    if existing is None or existing.state not in TERMINAL_ACTION_STATES:
        return None
    if journal is not None:
        journal.meet_action_rejected(
            action=existing.kind,
            action_id=action_id,
            reason="duplicate_suppressed",
        )
    return LedgerResult(
        action_id=action_id,
        outcome=_terminal_outcome(existing.state),
        detail=f"already {existing.state}; first terminal wins",
        attempts=existing.attempt,
        duplicate=True,
    )


class _MeetActionExecutor:
    def __init__(
        self,
        ledger: MeetActionLedger,
        journal: Journal | None,
        action_id: str,
        started: float,
        effects: MeetActionEffects,
    ) -> None:
        self._ledger = ledger
        self._journal = journal
        self._action_id = action_id
        self._started = started
        self._effects = effects
        self._act_ms = 0.0
        self._observe_ms = 0.0
        self._queue_ms = 0.0

    def note_queue_wait(self) -> None:
        self._queue_ms = self._elapsed_ms()

    def _phases(self) -> dict[str, float]:
        return {
            "act_ms": self._act_ms,
            "observe_ms": self._observe_ms,
            "queue_ms": self._queue_ms,
        }

    async def _observe(self) -> bool:
        started = time.monotonic()
        try:
            return await self._effects.observe()
        finally:
            self._observe_ms += (time.monotonic() - started) * 1000

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._started) * 1000

    def record_intent(
        self, existing: ActionIntent | None, address: MeetActionAddress, call: MeetActionCall
    ) -> ActionIntent:
        if existing is not None:
            return existing
        intent = self._ledger.record(
            self._action_id,
            call.kind,
            params_hash_for(address.meeting_id, self._action_id, call.params),
            authorized=call.authorized,
            chars=call.chars,
        )
        self._ledger.save()
        if self._journal is not None:
            self._journal.meet_action_requested(
                action=call.kind,
                action_id=self._action_id,
                actor=call.actor,
                attempt=1,
                chars=call.chars,
            )
        return intent

    async def read_before(self, intent: ActionIntent) -> tuple[ActionIntent, bool]:
        if intent.observed_before is None:
            before = await self._observe()
            intent = self._ledger.mark(self._action_id, intent.state, observed_before=before)
            self._ledger.save()
            return intent, before
        return intent, intent.observed_before

    def adopt_effective(self, current: ActionIntent) -> LedgerResult:
        marked = self._ledger.mark(current.action_id, "observed_done")
        self._ledger.save()
        if self._journal is not None:
            self._journal.meet_action_observed(
                action=marked.kind,
                action_id=self._action_id,
                observed_state="effective",
                chars=marked.chars,
            )
            self._journal.meet_action_finished(
                action=marked.kind,
                action_id=self._action_id,
                observed=True,
                attempts=marked.attempt,
                duration_ms=self._elapsed_ms(),
                **self._phases(),
            )
        return LedgerResult(
            action_id=self._action_id,
            outcome="succeeded",
            detail="effective state observed",
            attempts=marked.attempt,
        )

    def record_failure(self, current: ActionIntent, reason: str, detail: str) -> LedgerResult:
        marked = self._ledger.mark(current.action_id, "failed")
        self._ledger.save()
        if self._journal is not None:
            self._journal.meet_action_failed(
                action=marked.kind,
                action_id=self._action_id,
                reason=reason,
                attempts=marked.attempt,
                duration_ms=self._elapsed_ms(),
                detail=detail,
                **self._phases(),
            )
        return LedgerResult(
            action_id=self._action_id,
            outcome="failed",
            detail=detail,
            attempts=marked.attempt,
        )

    def refuse(self, intent: ActionIntent, reason: str, detail: str) -> LedgerResult:
        refused = self._ledger.mark(intent.action_id, "refused")
        self._ledger.save()
        if self._journal is not None:
            self._journal.meet_action_rejected(
                action=refused.kind,
                action_id=self._action_id,
                reason=reason,
            )
        return LedgerResult(
            action_id=self._action_id,
            outcome="refused",
            detail=detail,
            attempts=refused.attempt,
        )

    async def _fault_outcome(
        self, intent: ActionIntent, fault: ProviderFault, attempt: int
    ) -> LedgerResult | tuple[ActionIntent, int]:
        if fault.retryable and attempt < MAX_ACTION_ATTEMPTS:
            attempt += 1
            intent = self._ledger.mark(intent.action_id, intent.state, attempt=attempt)
            self._ledger.save()
            if await self._observe():
                return self.adopt_effective(intent)
            return intent, attempt
        if fault.retryable:
            return self.record_failure(intent, fault.kind, "retryable fault exhausted")
        return self.record_failure(intent, fault.kind, f"{fault.kind} is not retryable")

    async def _effect_outcome(
        self, intent: ActionIntent, effect: MeetActionResult, attempt: int
    ) -> LedgerResult:
        if effect.refused:
            return self.refuse(intent, "not_admitted", "not admitted to the call")
        if effect.failed:
            return self.record_failure(intent, "act_failed", effect.detail)
        intent = self._ledger.mark(intent.action_id, "acted", attempt=attempt)
        self._ledger.save()
        after = await self._observe()
        intent = self._ledger.mark(intent.action_id, intent.state, observed_after=after)
        self._ledger.save()
        if after:
            return self.adopt_effective(intent)
        return self.record_failure(intent, "unobserved", "readback did not confirm effect")

    async def drive(self, intent: ActionIntent) -> LedgerResult:
        attempt = intent.attempt
        while True:
            if self._journal is not None:
                self._journal.meet_action_started(
                    action=intent.kind, action_id=self._action_id, attempt=attempt
                )

            async def act() -> MeetActionResult:
                act_started = time.monotonic()
                try:
                    return await self._effects.act()
                finally:
                    self._act_ms += (time.monotonic() - act_started) * 1000

            effect, error = await capture_error(act)
            if isinstance(error, ProviderFault):
                outcome = await self._fault_outcome(intent, error, attempt)
                if isinstance(outcome, LedgerResult):
                    return outcome
                intent, attempt = outcome
                continue
            if error is not None:
                return self.record_failure(intent, type(error).__name__, "act raised")
            assert effect is not None
            return await self._effect_outcome(intent, effect, attempt)


async def execute_meet_action(
    record_path: Path,
    address: MeetActionAddress,
    call: MeetActionCall,
    effects: MeetActionEffects,
    journal: Journal | None = None,
) -> LedgerResult:
    started = time.monotonic()
    async with _loop_lock():
        with ledger_lock(record_path):
            ledger = MeetActionLedger.load(record_path, address.meeting_id)
            action_id = _resolve_action_id(ledger, address)
            existing = ledger.get(action_id)
            duplicate = _duplicate_result(existing, action_id, journal)
            if duplicate is not None:
                return duplicate
            executor = _MeetActionExecutor(ledger, journal, action_id, started, effects)
            executor.note_queue_wait()
            intent = executor.record_intent(existing, address, call)
            intent, before = await executor.read_before(intent)
            if before:
                return executor.adopt_effective(intent)
            if not intent.authorized:
                return executor.refuse(intent, "authority_denied", "persona lacks authority")
            return await executor.drive(intent)


async def reconcile_ledger(
    record_path: Path,
    meeting_id: str,
    observers: dict[MeetActionKind, ObserveEffect],
    journal: Journal | None = None,
) -> dict[str, int]:
    async with _loop_lock():
        with ledger_lock(record_path):
            ledger = MeetActionLedger.load(record_path, meeting_id)
            adopted = 0
            pending = 0
            for intent in ledger.pending():
                observer = observers.get(intent.kind)
                if observer is None:
                    pending += 1
                    continue
                if await observer():
                    marked = ledger.mark(intent.action_id, "observed_done", observed_after=True)
                    adopted += 1
                    if journal is not None:
                        journal.meet_action_observed(
                            action=marked.kind,
                            action_id=marked.action_id,
                            observed_state="effective",
                            chars=marked.chars,
                        )
                        journal.meet_action_finished(
                            action=marked.kind,
                            action_id=marked.action_id,
                            observed=True,
                            attempts=marked.attempt,
                        )
                else:
                    pending += 1
            if adopted:
                ledger.save()
            return {"adopted": adopted, "pending": pending}
