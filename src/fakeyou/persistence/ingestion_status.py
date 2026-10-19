import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.observability.journal import Journal
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.lease import (
    LOCK_STALE_AFTER_SECONDS,
    LeaseError,
    LockPurpose,
    file_lease,
    leased,
)

IngestionState = Literal["not_requested", "pending", "completed", "interrupted", "failed"]
PREFIX_BLOCK_BYTES = 8192


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
    record_bytes: int | None = Field(default=None, ge=0)
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None
    episode_uuids: dict[int, str] = Field(default_factory=dict)
    corrected_at: str | None = None
    correction_episodes: list[str] = Field(default_factory=list)
    correction_windows: dict[int, str] = Field(default_factory=dict)
    correction_run: str | None = None


def status_path(record_path: Path) -> Path:
    return MeetingDir.from_record_path(record_path).ingestion


def lock_path(record_path: Path) -> Path:
    return MeetingDir.from_record_path(record_path).lock


def hash_meeting_record(record_path: Path) -> str:
    return sha256(record_path.read_bytes()).hexdigest()


def _hash_leading_bytes(handle: BinaryIO, byte_count: int) -> str:
    digest = sha256()
    handle.seek(0)
    remaining = byte_count
    while remaining > 0:
        chunk = handle.read(min(PREFIX_BLOCK_BYTES, remaining))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _last_newline_end(handle: BinaryIO) -> int:
    end = handle.seek(0, os.SEEK_END)
    while end > 0:
        start = max(0, end - PREFIX_BLOCK_BYTES)
        handle.seek(start)
        position = handle.read(end - start).rfind(b"\n")
        if position != -1:
            return start + position + 1
        end = start
    return 0


def finalized_prefix(record_path: Path) -> tuple[str, int]:
    with record_path.open("rb") as handle:
        byte_count = _last_newline_end(handle)
        return _hash_leading_bytes(handle, byte_count), byte_count


def hash_record_prefix(record_path: Path, byte_count: int) -> str:
    with record_path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        if size < byte_count:
            raise ValueError(
                f"record has {size} bytes, shorter than the hashed prefix of {byte_count} bytes"
            )
        return _hash_leading_bytes(handle, byte_count)


def read_status(path: Path, *, journal: Journal | None = None) -> IngestionStatus | None:
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return IngestionStatus.model_validate_json(raw)
    except ValueError as error:
        if journal is not None:
            journal.record(
                "ingestion_status_rejected",
                source="memory_ingest",
                error_type=type(error).__name__,
                path=str(path),
            )
        raise FakeYouError(
            ErrorCode.INGESTION_STATUS_CORRUPT, path=path, reason=type(error).__name__
        ) from error


def write_status(path: Path, status: IngestionStatus) -> None:
    write_atomically(path, status.model_dump_json().encode("utf-8"))


def _record_lock_error(error: LeaseError, *, record_path: Path) -> RecordLockError:
    if error.code is ErrorCode.RECORD_LOCK_CORRUPT:
        return RecordLockError(ErrorCode.RECORD_LOCK_CORRUPT, path=lock_path(record_path))
    return RecordLockError(ErrorCode.RECORD_LOCKED, path=lock_path(record_path))


@contextmanager
def record_lock(
    record_path: Path,
    *,
    purpose: LockPurpose,
    stale_after_seconds: float = LOCK_STALE_AFTER_SECONDS,
) -> Iterator[str]:
    lease = file_lease(
        lock_path(record_path), purpose=purpose, stale_after_seconds=stale_after_seconds
    )
    with leased(lease, partial(_record_lock_error, record_path=record_path)) as token:
        yield token
