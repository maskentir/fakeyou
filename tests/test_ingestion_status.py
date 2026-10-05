import json
import os
import socket
import threading
import time
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
        assert owner["pid"] == os.getpid()
        assert owner["purpose"] == "meeting"
        assert owner["token"] == token
        assert owner["host"]
        assert datetime.fromisoformat(owner["heartbeat"]) <= datetime.now(UTC)
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


def test_record_lock_refuses_a_foreign_host_with_a_fresh_heartbeat(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": "other-container",
                "pid": 999999999,
                "purpose": "meeting",
                "token": "foreign",
                "heartbeat": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RecordLockError, match="locked"):
        with record_lock(record, purpose="ingestion", stale_after_seconds=60.0):
            pytest.fail("fresh foreign lease must not be taken over")
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "foreign"


def test_record_lock_takes_over_a_foreign_host_with_a_stale_heartbeat(
    tmp_path: Path,
) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": "other-container",
                "pid": 42,
                "purpose": "meeting",
                "token": "stale",
                "heartbeat": datetime(2026, 10, 3, 8, 0, tzinfo=UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with record_lock(record, purpose="ingestion", stale_after_seconds=60.0) as token:
        owner = json.loads(path.read_text(encoding="utf-8"))
        assert owner["token"] == token
        assert owner["token"] != "stale"
    assert not path.exists()


def test_record_lock_keeps_its_heartbeat_fresh_while_held(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with record_lock(record, purpose="meeting", stale_after_seconds=0.3):
        first = json.loads(lock_path(record).read_text(encoding="utf-8"))
        assert first["heartbeat"]
        time.sleep(0.5)
        second = json.loads(lock_path(record).read_text(encoding="utf-8"))
        assert second["token"] == first["token"]
        assert datetime.fromisoformat(second["heartbeat"]) > datetime.fromisoformat(
            first["heartbeat"]
        )

    assert not lock_path(record).exists()


def test_record_lock_takes_over_a_live_pid_with_a_stale_heartbeat(
    tmp_path: Path,
) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "purpose": "meeting",
                "token": "stale",
                "heartbeat": datetime(2026, 10, 3, 8, 0, tzinfo=UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with record_lock(record, purpose="ingestion", stale_after_seconds=60.0) as token:
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == token
    assert not path.exists()


def test_record_lock_refuses_a_contender_past_the_original_deadline_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with record_lock(record, purpose="meeting", stale_after_seconds=0.3):
        time.sleep(0.6)
        monkeypatch.setattr(
            "fakeyou.ingestion_status.socket.gethostname", lambda: "other-container"
        )
        with pytest.raises(RecordLockError, match="locked"):
            with record_lock(record, purpose="ingestion", stale_after_seconds=0.3):
                pytest.fail("renewed lease must refuse a contender past the deadline")


def test_record_lock_takes_over_a_foreign_host_without_a_heartbeat(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"host": "other-container", "pid": 42, "purpose": "meeting", "token": "stale"}),
        encoding="utf-8",
    )

    with record_lock(record, purpose="ingestion", stale_after_seconds=60.0) as token:
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == token
    assert not path.exists()


def test_record_lock_lets_exactly_one_racer_take_a_stale_lease(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": "other-container",
                "pid": 42,
                "purpose": "meeting",
                "token": "stale",
                "heartbeat": datetime(2026, 10, 3, 8, 0, tzinfo=UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    barrier = threading.Barrier(2)
    winners: list[str] = []
    failures: list[str] = []

    def race() -> None:
        barrier.wait(timeout=10)
        try:
            with record_lock(record, purpose="meeting", stale_after_seconds=60.0) as token:
                winners.append(token)
                time.sleep(0.2)
        except RecordLockError:
            failures.append("locked")

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert [thread.is_alive() for thread in threads] == [False, False]

    assert len(winners) == 1
    assert failures == ["locked"]
    assert not path.exists()


def test_record_lock_retries_the_heartbeat_after_a_transient_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    real_write = __import__(
        "fakeyou.ingestion_status", fromlist=["write_atomically"]
    ).write_atomically
    calls: list[str] = []

    def fail_once(path, data):
        calls.append("write")
        if len(calls) == 1:
            raise OSError("transient write error")
        return real_write(path, data)

    with record_lock(record, purpose="meeting", stale_after_seconds=0.3):
        first = json.loads(lock_path(record).read_text(encoding="utf-8"))["heartbeat"]
        monkeypatch.setattr("fakeyou.ingestion_status.write_atomically", fail_once)
        time.sleep(0.6)
        second = json.loads(lock_path(record).read_text(encoding="utf-8"))["heartbeat"]
        assert len(calls) >= 2
        assert datetime.fromisoformat(second) > datetime.fromisoformat(first)

    assert not lock_path(record).exists()


def test_record_lock_refuses_a_corrupt_lock_file(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(RecordLockError, match="locked"):
        with record_lock(record, purpose="forget"):
            pytest.fail("corrupt lock must fail closed")
    assert path.read_text(encoding="utf-8") == "{corrupt"
