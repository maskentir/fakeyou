from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.ingestion_status import LeaseError, RecordLockError, file_lease, write_atomically
from fakeyou.journal import Journal
from fakeyou.meet_actions import ActionOutcome, MeetActionResult
from fakeyou.provider_faults import ProviderFault

ACTIONS_FILE_NAME = "actions.json"
LEDGER_LOCK_FILE_NAME = "actions.lock"

MAX_ACTION_ATTEMPTS = 3

MeetActionKind = Literal["raise_hand", "lower_hand", "send_chat"]
ActionState = Literal["requested", "acted", "observed_done", "refused", "failed"]

TERMINAL_ACTION_STATES = frozenset({"observed_done", "refused", "failed"})

ActEffect = Callable[[], Awaitable[MeetActionResult]]
ObserveEffect = Callable[[], Awaitable[bool]]


class LedgerError(Exception):
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
    return record_path.parent / ACTIONS_FILE_NAME


def ledger_lock_path(record_path: Path) -> Path:
    return record_path.parent / LEDGER_LOCK_FILE_NAME


@contextmanager
def ledger_lock(record_path: Path) -> Iterator[str]:
    try:
        with file_lease(ledger_lock_path(record_path), purpose="meeting") as token:
            yield token
    except LeaseError:
        path = ledger_lock_path(record_path)
        raise RecordLockError(f"action ledger is locked: {path}") from None


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
            raise LedgerError(f"unreadable action ledger: {path}") from error
        try:
            data = ActionLedgerFile.model_validate_json(raw)
        except ValueError as error:
            raise LedgerError(f"unreadable action ledger: {path}") from error
        if data.meeting_id != meeting_id:
            raise LedgerError(f"action ledger meeting mismatch: {path}")
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
        observed_before: bool | None = None,
        observed_after: bool | None = None,
        attempt: int | None = None,
    ) -> ActionIntent:
        existing = self._data.intents.get(action_id)
        if existing is None:
            raise LedgerError(f"unknown action intent: {action_id}")
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


async def execute_meet_action(
    record_path: Path,
    *,
    meeting_id: str,
    kind: MeetActionKind,
    act: ActEffect,
    observe: ObserveEffect,
    tool_call_id: str | None = None,
    seq: int | None = None,
    params: str = "",
    chars: int | None = None,
    authorized: bool = True,
    journal: Journal | None = None,
    actor: str = "twin",
) -> LedgerResult:
    started = time.monotonic()
    async with _loop_lock():
        with ledger_lock(record_path):
            ledger = MeetActionLedger.load(record_path, meeting_id)
            if tool_call_id:
                action_id = tool_call_id
            elif seq is not None:
                action_id = action_id_for(meeting_id, seq)
                ledger.reserve_seq(seq)
            else:
                action_id = action_id_for(meeting_id, ledger.claim_seq())
            existing = ledger.get(action_id)
            if existing is not None and existing.state in TERMINAL_ACTION_STATES:
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
            if existing is None:
                intent = ledger.record(
                    action_id,
                    kind,
                    params_hash_for(meeting_id, action_id, params),
                    authorized,
                    chars,
                )
                ledger.save()
                if journal is not None:
                    journal.meet_action_requested(
                        action=kind,
                        action_id=action_id,
                        actor=actor,
                        attempt=1,
                        chars=chars,
                    )
            else:
                intent = existing

            def elapsed_ms() -> float:
                return (time.monotonic() - started) * 1000

            def adopt_effective(current: ActionIntent) -> LedgerResult:
                marked = ledger.mark(current.action_id, "observed_done")
                ledger.save()
                if journal is not None:
                    journal.meet_action_observed(
                        action=marked.kind,
                        action_id=action_id,
                        observed_state="effective",
                        chars=marked.chars,
                    )
                    journal.meet_action_finished(
                        action=marked.kind,
                        action_id=action_id,
                        observed=True,
                        attempts=marked.attempt,
                        duration_ms=elapsed_ms(),
                    )
                return LedgerResult(
                    action_id=action_id,
                    outcome="succeeded",
                    detail="effective state observed",
                    attempts=marked.attempt,
                )

            def record_failure(current: ActionIntent, reason: str, detail: str) -> LedgerResult:
                marked = ledger.mark(current.action_id, "failed")
                ledger.save()
                if journal is not None:
                    journal.meet_action_failed(
                        action=marked.kind,
                        action_id=action_id,
                        reason=reason,
                        attempts=marked.attempt,
                        duration_ms=elapsed_ms(),
                    )
                return LedgerResult(
                    action_id=action_id,
                    outcome="failed",
                    detail=detail,
                    attempts=marked.attempt,
                )

            if intent.observed_before is None:
                before = await observe()
                intent = ledger.mark(action_id, intent.state, observed_before=before)
                ledger.save()
            else:
                before = intent.observed_before
            if before:
                return adopt_effective(intent)
            if not intent.authorized:
                refused = ledger.mark(action_id, "refused")
                ledger.save()
                if journal is not None:
                    journal.meet_action_rejected(
                        action=refused.kind,
                        action_id=action_id,
                        reason="authority_denied",
                    )
                return LedgerResult(
                    action_id=action_id,
                    outcome="refused",
                    detail="persona lacks authority",
                    attempts=refused.attempt,
                )
            attempt = intent.attempt
            while True:
                if journal is not None:
                    journal.meet_action_started(
                        action=intent.kind, action_id=action_id, attempt=attempt
                    )
                try:
                    effect = await act()
                except ProviderFault as fault:
                    if fault.retryable and attempt < MAX_ACTION_ATTEMPTS:
                        attempt += 1
                        intent = ledger.mark(action_id, intent.state, attempt=attempt)
                        ledger.save()
                        if await observe():
                            return adopt_effective(intent)
                        continue
                    if fault.retryable:
                        return record_failure(intent, fault.kind, "retryable fault exhausted")
                    return record_failure(intent, fault.kind, f"{fault.kind} is not retryable")
                except Exception as error:
                    return record_failure(intent, type(error).__name__, "act raised")
                if effect.refused:
                    refused = ledger.mark(action_id, "refused")
                    ledger.save()
                    if journal is not None:
                        journal.meet_action_rejected(
                            action=refused.kind,
                            action_id=action_id,
                            reason="not_admitted",
                        )
                    return LedgerResult(
                        action_id=action_id,
                        outcome="refused",
                        detail="not admitted to the call",
                        attempts=refused.attempt,
                    )
                if effect.failed:
                    return record_failure(intent, "act_failed", "act reported failure")
                intent = ledger.mark(action_id, "acted", attempt=attempt)
                ledger.save()
                after = await observe()
                intent = ledger.mark(action_id, intent.state, observed_after=after)
                ledger.save()
                if after:
                    return adopt_effective(intent)
                return record_failure(intent, "unobserved", "readback did not confirm effect")


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
