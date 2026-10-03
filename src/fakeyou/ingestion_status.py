import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

IngestionState = Literal["not_requested", "pending", "completed", "interrupted", "failed"]
LockPurpose = Literal["meeting", "ingestion", "forget", "debrief"]

INGESTION_FILE_NAME = "ingestion.json"


class RecordLockError(Exception):
    pass


class IngestionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    meeting_id: str
    group_id: str
    status: IngestionState
    windows: int = Field(default=0, ge=0)
    resumed_from: int = Field(default=0, ge=0)
    ingested: int = Field(default=0, ge=0)
    record_sha256: str | None = None
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None


def status_path(record_path: Path) -> Path:
    return record_path.parent / INGESTION_FILE_NAME


def lock_path(record_path: Path) -> Path:
    return record_path.parent.parent / f"{record_path.parent.name}.lock"


def hash_meeting_record(record_path: Path) -> str:
    return sha256(record_path.read_bytes()).hexdigest()


def read_status(path: Path) -> IngestionStatus | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return IngestionStatus.model_validate_json(raw)
    except ValueError:
        return None


def write_status(path: Path, status: IngestionStatus) -> None:
    write_atomically(path, status.model_dump_json().encode("utf-8"))


def write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _lock_owner(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    purpose = data.get("purpose")
    token = data.get("token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if purpose not in ("meeting", "ingestion", "forget", "debrief"):
        return None
    if not isinstance(token, str) or not token:
        return None
    return {"pid": pid, "purpose": purpose, "token": token}


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


@contextmanager
def record_lock(record_path: Path, *, purpose: LockPurpose) -> Iterator[str]:
    path = lock_path(record_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = json.dumps({"pid": os.getpid(), "purpose": purpose, "token": token}).encode()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = _lock_owner(path)
            if owner is None or _process_alive(owner["pid"]):
                raise RecordLockError(f"record is locked: {path}") from None
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            continue
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        break
    else:
        raise RecordLockError(f"record is locked: {path}") from None
    try:
        yield token
    finally:
        owner = _lock_owner(path)
        if owner is not None and owner["token"] == token:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
