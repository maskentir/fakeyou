import fcntl
import json
import os
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.catalog.errors import ErrorCode, FakeYouError

IngestionState = Literal["not_requested", "pending", "completed", "interrupted", "failed"]
LockPurpose = Literal["meeting", "ingestion", "forget", "debrief", "diarization"]

INGESTION_FILE_NAME = "ingestion.json"

LOCK_STALE_AFTER_SECONDS = 60.0


class LeaseError(FakeYouError):
    pass


class RecordLockError(LeaseError):
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
    episode_uuids: dict[int, str] = Field(default_factory=dict)
    corrected_at: str | None = None
    correction_episodes: list[str] = Field(default_factory=list)
    correction_windows: dict[int, str] = Field(default_factory=dict)
    correction_run: str | None = None


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
    if purpose not in ("meeting", "ingestion", "forget", "debrief", "diarization"):
        return None
    if not isinstance(token, str) or not token:
        return None
    host = data.get("host")
    if host is not None and (not isinstance(host, str) or not host):
        return None
    heartbeat: datetime | None = None
    raw_heartbeat = data.get("heartbeat")
    if raw_heartbeat is not None:
        if not isinstance(raw_heartbeat, str):
            return None
        try:
            heartbeat = datetime.fromisoformat(raw_heartbeat)
        except ValueError:
            return None
        if heartbeat.tzinfo is None or heartbeat.utcoffset() is None:
            return None
    return {"host": host, "pid": pid, "purpose": purpose, "token": token, "heartbeat": heartbeat}


LeaseState = Literal["absent", "live", "stale", "corrupt"]


def describe_lease(
    path: Path, *, stale_after_seconds: float = LOCK_STALE_AFTER_SECONDS
) -> LeaseState:
    if not path.exists():
        return "absent"
    owner = _lock_owner(path)
    if owner is None:
        return "corrupt"
    if _owner_recoverable(owner, stale_after_seconds):
        return "stale"
    return "live"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


@contextmanager
def record_lock(
    record_path: Path,
    *,
    purpose: LockPurpose,
    stale_after_seconds: float = LOCK_STALE_AFTER_SECONDS,
) -> Iterator[str]:
    try:
        with file_lease(
            lock_path(record_path), purpose=purpose, stale_after_seconds=stale_after_seconds
        ) as token:
            yield token
    except LeaseError as error:
        if error.code is ErrorCode.RECORD_LOCK_CORRUPT:
            raise RecordLockError(
                ErrorCode.RECORD_LOCK_CORRUPT, path=lock_path(record_path)
            ) from None
        raise RecordLockError(ErrorCode.RECORD_LOCKED, path=lock_path(record_path)) from None


@contextmanager
def _acquisition_guard(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise LeaseError(ErrorCode.LEASE_LOCKED, path=path) from None
    try:
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


@contextmanager
def file_lease(
    path: Path, *, purpose: LockPurpose, stale_after_seconds: float = LOCK_STALE_AFTER_SECONDS
) -> Iterator[str]:
    token = uuid.uuid4().hex
    host = socket.gethostname()
    owner_payload = {"host": host, "pid": os.getpid(), "purpose": purpose, "token": token}
    with _acquisition_guard(path) as fd:
        raw = _read_fd(fd)
        owner = _lock_owner(path) if raw else None
        if raw and owner is None:
            raise LeaseError(ErrorCode.RECORD_LOCK_CORRUPT, path=path) from None
        if owner is not None and not _owner_recoverable(owner, stale_after_seconds):
            raise LeaseError(ErrorCode.LEASE_LOCKED, path=path) from None
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, _lease_payload(owner_payload))
            os.fsync(fd)
        except OSError:
            raise LeaseError(ErrorCode.LEASE_LOCKED, path=path) from None
        try:
            current = os.stat(path)
        except FileNotFoundError:
            raise LeaseError(ErrorCode.LEASE_LOCKED, path=path) from None
        written = os.fstat(fd)
        if (current.st_ino, current.st_dev) != (written.st_ino, written.st_dev):
            raise LeaseError(ErrorCode.LEASE_LOCKED, path=path) from None
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_refresh_lease,
        args=(path, owner_payload, token, stale_after_seconds, stop),
        daemon=True,
    )
    heartbeat.start()
    try:
        yield token
    finally:
        stop.set()
        heartbeat.join(timeout=5.0)
        owner = _lock_owner(path)
        if owner is not None and owner["token"] == token:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def _lease_payload(owner: dict[str, Any]) -> bytes:
    return json.dumps({**owner, "heartbeat": datetime.now(UTC).isoformat()}).encode()


def _owner_recoverable(owner: dict[str, Any], stale_after_seconds: float) -> bool:
    if owner["host"] is None:
        return not _process_alive(owner["pid"])
    heartbeat: datetime | None = owner["heartbeat"]
    if heartbeat is None:
        return True
    if (datetime.now(UTC) - heartbeat).total_seconds() > stale_after_seconds:
        return True
    return owner["host"] == socket.gethostname() and not _process_alive(owner["pid"])


def _refresh_lease(
    path: Path,
    owner: dict[str, Any],
    token: str,
    stale_after_seconds: float,
    stop: threading.Event,
) -> None:
    interval = max(stale_after_seconds / 3, 0.05)
    while not stop.wait(interval):
        current = _lock_owner(path)
        if current is None or current["token"] != token:
            return
        if stop.is_set():
            return
        try:
            write_atomically(path, _lease_payload(owner))
        except Exception:
            continue
