import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakeyou.brief import context_path
from fakeyou.ingestion_status import (
    IngestionStatus,
    RecordLockError,
    hash_meeting_record,
    lock_path,
    read_status,
    record_lock,
    status_path,
    write_status,
)

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)


def pending_status(**overrides) -> IngestionStatus:
    values: dict = {
        "meeting_id": "meeting-9",
        "group_id": "twin_group",
        "status": "pending",
        "windows": 2,
        "resumed_from": 0,
        "ingested": 0,
        "record_sha256": None,
        "updated_at": NOW,
        "reason": None,
    }
    values.update(overrides)
    return IngestionStatus.model_validate(values)


def test_status_roundtrips_through_an_atomic_write(tmp_path: Path) -> None:
    path = status_path(tmp_path / "meeting-9" / "record.jsonl")

    assert path == tmp_path / "meeting-9" / "ingestion.json"
    write_status(path, pending_status())

    assert read_status(path) == pending_status()
    assert sorted(path.parent.iterdir()) == [path]


def test_status_rejects_extra_fields_negative_counts_and_naive_time() -> None:
    with pytest.raises(ValueError):
        IngestionStatus.model_validate({**pending_status().model_dump(), "note": "x"})
    with pytest.raises(ValueError):
        pending_status(windows=-1)
    with pytest.raises(ValueError):
        pending_status(resumed_from=-1)
    with pytest.raises(ValueError):
        pending_status(ingested=-1)
    with pytest.raises(ValueError):
        pending_status(updated_at=datetime(2026, 10, 3, 8, 0))


def test_read_status_returns_none_when_missing_or_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.json"

    assert read_status(path) is None
    path.write_text("{not json", encoding="utf-8")
    assert read_status(path) is None
    path.write_text(json.dumps({"meeting_id": "meeting-9"}), encoding="utf-8")
    assert read_status(path) is None
    path.write_text(
        json.dumps({**pending_status().model_dump(mode="json"), "extra": True}), encoding="utf-8"
    )
    assert read_status(path) is None


def test_atomic_status_failure_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ingestion.json"
    path.write_text("previous", encoding="utf-8")

    def reject_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr("os.replace", reject_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_status(path, pending_status())
    assert path.read_text(encoding="utf-8") == "previous"
    assert [child.name for child in tmp_path.iterdir()] == ["ingestion.json"]


def test_status_fsync_failure_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ingestion.json"
    path.write_text("previous", encoding="utf-8")

    def reject_fsync(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr("os.fsync", reject_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        write_status(path, pending_status())
    assert path.read_text(encoding="utf-8") == "previous"
    assert [child.name for child in tmp_path.iterdir()] == ["ingestion.json"]


def test_hash_meeting_record_matches_sha256(tmp_path: Path) -> None:
    import hashlib

    record = tmp_path / "record.jsonl"
    record.write_bytes(b'{"kind":"transcript"}\n')

    assert hash_meeting_record(record) == hashlib.sha256(b'{"kind":"transcript"}\n').hexdigest()
    with pytest.raises(OSError):
        hash_meeting_record(tmp_path / "absent.jsonl")


def test_meeting_context_shares_the_record_directory_while_the_lock_stays_outside(
    tmp_path: Path,
) -> None:
    record = tmp_path / "meetings" / "meeting-9" / "record.jsonl"

    assert context_path(record).parent == status_path(record).parent == record.parent
    assert record.parent not in lock_path(record).parents


def test_record_lock_releases_its_own_token(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with record_lock(record, purpose="meeting") as token:
        assert isinstance(token, str) and token
        owner = json.loads(lock_path(record).read_text(encoding="utf-8"))
        assert owner == {"pid": os.getpid(), "purpose": "meeting", "token": token}
    assert not lock_path(record).exists()


def test_record_lock_refuses_a_second_writer_while_held(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with record_lock(record, purpose="meeting"):
        with pytest.raises(RecordLockError, match="locked"):
            with record_lock(record, purpose="ingestion"):
                pytest.fail("second writer must not acquire the lock")
        assert lock_path(record).exists()
    assert not lock_path(record).exists()


def test_record_lock_keeps_a_foreign_token_on_release(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with record_lock(record, purpose="ingestion"):
        lock_path(record).write_text(
            json.dumps({"pid": os.getpid(), "purpose": "meeting", "token": "foreign"}),
            encoding="utf-8",
        )
    assert json.loads(lock_path(record).read_text(encoding="utf-8"))["token"] == "foreign"


def test_record_lock_recovers_only_a_proven_dead_owner(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": 999999999, "purpose": "meeting", "token": "stale"}), encoding="utf-8"
    )

    with record_lock(record, purpose="ingestion"):
        assert json.loads(path.read_text(encoding="utf-8"))["token"] != "stale"
    assert not path.exists()

    path.write_text(
        json.dumps({"pid": os.getpid(), "purpose": "meeting", "token": "live"}), encoding="utf-8"
    )
    with pytest.raises(RecordLockError, match="locked"):
        with record_lock(record, purpose="ingestion"):
            pytest.fail("live owner must not be recovered")
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "live"


def test_record_lock_refuses_a_corrupt_lock_file(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(RecordLockError, match="locked"):
        with record_lock(record, purpose="forget"):
            pytest.fail("corrupt lock must fail closed")
    assert path.read_text(encoding="utf-8") == "{corrupt"
