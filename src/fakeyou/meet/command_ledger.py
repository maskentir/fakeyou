from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.ingestion_status import RecordLockError
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.lease import LeaseError, file_lease, leased
from fakeyou.session.commands import (
    TERMINAL_COMMAND_STATES,
    CommandKind,
    CommandSpec,
    CommandState,
    Verification,
    recovered,
    transition,
)

LEDGER_VERSION: Literal[2] = 2


class LedgerError(FakeYouError):
    pass


class CommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1)
    kind: CommandKind
    attempt_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    actor: str
    browser_epoch: int = Field(ge=0)
    verification: Verification
    text: str = ""
    state: CommandState = "accepted"
    dispatches: int = Field(default=0, ge=0)
    boundary_at: AwareDatetime | None = None
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str = ""

    @property
    def spec(self) -> CommandSpec:
        return CommandSpec(self.kind, self.text)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_COMMAND_STATES


class CommandLedgerFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2] = LEDGER_VERSION
    meeting_id: str
    commands: dict[str, CommandRecord] = Field(default_factory=dict)


def ledger_path(record_path: Path) -> Path:
    return MeetingDir.from_record_path(record_path).actions


def _ledger_locked(_error: LeaseError, *, path: Path) -> RecordLockError:
    return RecordLockError(ErrorCode.ACTION_LEDGER_LOCKED, path=path)


@contextmanager
def _locked(record_path: Path) -> Iterator[str]:
    path = MeetingDir.from_record_path(record_path).actions_lock
    with leased(file_lease(path, purpose="meeting"), partial(_ledger_locked, path=path)) as token:
        yield token


class CommandLedger:
    def __init__(self, record_path: Path, meeting_id: str) -> None:
        self._record_path = record_path
        self._meeting_id = meeting_id

    def _load(self) -> CommandLedgerFile:
        path = ledger_path(self._record_path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return CommandLedgerFile(meeting_id=self._meeting_id)
        except OSError as error:
            raise LedgerError(ErrorCode.ACTION_LEDGER_UNREADABLE, path=path) from error
        try:
            data = CommandLedgerFile.model_validate_json(raw)
        except ValueError as error:
            raise LedgerError(ErrorCode.ACTION_LEDGER_UNREADABLE, path=path) from error
        if data.meeting_id != self._meeting_id:
            raise LedgerError(ErrorCode.ACTION_LEDGER_MEETING_MISMATCH, path=path)
        return data

    def _save(self, data: CommandLedgerFile) -> None:
        write_atomically(ledger_path(self._record_path), data.model_dump_json().encode("utf-8"))

    def get(self, command_id: str) -> CommandRecord | None:
        with _locked(self._record_path):
            return self._load().commands.get(command_id)

    def records(self) -> tuple[CommandRecord, ...]:
        with _locked(self._record_path):
            return tuple(self._load().commands.values())

    def accept(self, record: CommandRecord) -> CommandRecord:
        with _locked(self._record_path):
            data = self._load()
            existing = data.commands.get(record.command_id)
            if existing is not None:
                return existing
            self._save(
                data.model_copy(update={"commands": {**data.commands, record.command_id: record}})
            )
            return record

    def move(self, command_id: str, to: CommandState, *, detail: str = "") -> CommandRecord:
        with _locked(self._record_path):
            data = self._load()
            current = data.commands.get(command_id)
            if current is None:
                raise LedgerError(ErrorCode.ACTION_LEDGER_INTENT_UNKNOWN, action_id=command_id)
            now = datetime.now(UTC)
            update: dict[str, object] = {
                "state": transition(current.kind, current.state, to),
                "updated_at": now,
                "detail": detail or current.detail,
            }
            if to == "dispatching":
                update["boundary_at"] = now
                update["dispatches"] = current.dispatches + 1
            moved = current.model_copy(update=update)
            self._save(data.model_copy(update={"commands": {**data.commands, command_id: moved}}))
            return moved

    def recover(self, attempt_id: str) -> tuple[CommandRecord, ...]:
        with _locked(self._record_path):
            data = self._load()
            closed = {
                command_id: record.model_copy(
                    update={
                        "state": recovered(record.state),
                        "updated_at": datetime.now(UTC),
                        "detail": "previous_attempt",
                    }
                )
                for command_id, record in data.commands.items()
                if record.attempt_id != attempt_id and not record.terminal
            }
            if closed:
                self._save(data.model_copy(update={"commands": {**data.commands, **closed}}))
            return tuple(closed.values())
