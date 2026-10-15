import fcntl
import json
import os
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.foundation.timeparse import is_aware, require_iso_datetime

LockPurpose = Literal["meeting", "ingestion", "forget", "debrief", "diarization"]
LeaseState = Literal["absent", "live", "stale", "corrupt"]

LOCK_STALE_AFTER_SECONDS = 60.0
LEASE_HEARTBEAT_MAX_FAILURES = 3


class LeaseError(FakeYouError):
    pass


def lock_owner(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return _parse_owner(raw)


def _parse_owner(raw: bytes) -> dict[str, Any] | None:
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
    try:
        heartbeat = _parse_heartbeat(data.get("heartbeat"))
    except (TypeError, ValueError):
        return None
    return {"host": host, "pid": pid, "purpose": purpose, "token": token, "heartbeat": heartbeat}


def _parse_heartbeat(raw_heartbeat: Any) -> datetime | None:
    if raw_heartbeat is None:
        return None
    if not isinstance(raw_heartbeat, str):
        raise TypeError("heartbeat must be an iso string")
    heartbeat = require_iso_datetime(raw_heartbeat)
    if not is_aware(heartbeat):
        raise ValueError("heartbeat must carry timezone")
    return heartbeat


def describe_lease(
    path: Path, *, stale_after_seconds: float = LOCK_STALE_AFTER_SECONDS
) -> LeaseState:
    if not path.exists():
        return "absent"
    owner = lock_owner(path)
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
def leased(
    lease: AbstractContextManager[str], translate: Callable[[LeaseError], Exception]
) -> Iterator[str]:
    stack = ExitStack()
    try:
        token = stack.enter_context(lease)
    except LeaseError as error:
        raise translate(error) from error
    with stack:
        yield token


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
        owner = lock_owner(path) if raw else None
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
    held: list[bool] = []

    def heartbeat_loop() -> None:
        held.append(refresh_lease(path, owner_payload, token, stale_after_seconds, stop))

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()
    try:
        yield token
    finally:
        stop.set()
        heartbeat.join(timeout=5.0)
        owner = lock_owner(path)
        if owner is not None and owner["token"] == token:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    if held == [False]:
        raise LeaseError(ErrorCode.LEASE_LOST, path=path)


def _lease_payload(owner: dict[str, Any]) -> bytes:
    return json.dumps({**owner, "heartbeat": datetime.now(UTC).isoformat()}).encode()


def _owner_recoverable(owner: dict[str, Any], stale_after_seconds: float) -> bool:
    if owner["host"] is None or owner["host"] == socket.gethostname():
        return not _process_alive(owner["pid"])
    heartbeat: datetime | None = owner["heartbeat"]
    if heartbeat is None:
        return True
    return (datetime.now(UTC) - heartbeat).total_seconds() > stale_after_seconds


def refresh_lease(
    path: Path,
    owner: dict[str, Any],
    token: str,
    stale_after_seconds: float,
    stop: threading.Event,
) -> bool:
    interval = max(stale_after_seconds / 3, 0.05)
    failures = 0
    while not stop.wait(interval):
        try:
            if not refresh_in_place(path, owner, token):
                return False
        except OSError:
            failures += 1
            if failures >= LEASE_HEARTBEAT_MAX_FAILURES:
                return False
            continue
        failures = 0
    return True


def refresh_in_place(path: Path, owner: dict[str, Any], token: str) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            current = _parse_owner(_read_fd(fd))
            if current is None or current["token"] != token:
                return False
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, _lease_payload(owner))
            os.fsync(fd)
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
