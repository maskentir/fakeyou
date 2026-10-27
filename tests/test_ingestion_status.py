import asyncio
import fcntl
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import fakeyou.persistence.lease as lease
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.ingest_status import (
    IngestOutcome,
    IngestSummary,
    finish_ingest,
    ingest_state,
    terminal_outcome,
)
from fakeyou.memory.ingestion import IngestResult
from fakeyou.observability.faults import FaultReporter
from fakeyou.persistence.brief import context_path
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    RecordLockError,
    finalized_prefix,
    hash_meeting_record,
    hash_record_prefix,
    lock_path,
    read_status,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.persistence.lease import LeaseError, lock_owner
from tests._error_support import message_pattern
from tests._persistence_fakes import RecordingJournal


def test_old_status_json_without_new_fields_loads_with_defaults(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.json"
    payload = {
        "meeting_id": "meeting-9",
        "group_id": "twin_group",
        "status": "completed",
        "windows": 3,
        "resumed_from": 0,
        "ingested": 3,
        "record_sha256": None,
        "updated_at": NOW.isoformat(),
        "reason": None,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = read_status(path)

    assert status is not None
    assert status.episode_uuids == {}
    assert status.corrected_at is None
    assert status.correction_episodes == []


def test_correction_log_roundtrips_without_touching_window_map(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.json"
    status = pending_status(
        episode_uuids={0: "uuid-a", 1: "uuid-b"},
        corrected_at="2026-10-09T12:00:00+00:00",
        correction_episodes=["uuid-fix"],
    )
    write_status(path, status)

    loaded = read_status(path)

    assert loaded is not None
    assert loaded.episode_uuids == {0: "uuid-a", 1: "uuid-b"}
    assert loaded.corrected_at == "2026-10-09T12:00:00+00:00"
    assert loaded.correction_episodes == ["uuid-fix"]


NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)


def pending_status(**overrides: Any) -> IngestionStatus:
    values: dict[str, Any] = {
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
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        IngestionStatus.model_validate({**pending_status().model_dump(), "note": "x"})
    with pytest.raises(ValueError, match="Input should be greater than or equal to 0"):
        pending_status(windows=-1)
    with pytest.raises(ValueError, match="Input should be greater than or equal to 0"):
        pending_status(resumed_from=-1)
    with pytest.raises(ValueError, match="Input should be greater than or equal to 0"):
        pending_status(ingested=-1)
    with pytest.raises(ValueError, match="Input should have timezone info"):
        pending_status(updated_at=datetime(2026, 10, 3, 8, 0))


def test_read_status_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_status(tmp_path / "ingestion.json") is None


def test_read_status_raises_the_catalog_error_for_corrupt_content(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.json"
    unknown_field = {**pending_status().model_dump(mode="json"), "extra": True}

    for content in (
        "{not json",
        json.dumps({"meeting_id": "meeting-9"}),
        json.dumps(unknown_field),
    ):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(
            FakeYouError, match=message_pattern(ErrorCode.INGESTION_STATUS_CORRUPT)
        ) as caught:
            read_status(path)
        assert caught.value.code is ErrorCode.INGESTION_STATUS_CORRUPT


def test_atomic_status_failure_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ingestion.json"
    path.write_text("previous", encoding="utf-8")

    def reject_replace(_source: Any, target: Any) -> None:
        del target
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

    def reject_fsync(_fd: int) -> None:
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
    with pytest.raises(OSError, match="absent.jsonl"):
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
        assert isinstance(token, str)
        assert token
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
        with pytest.raises(RecordLockError, match="locked") as error:
            with record_lock(record, purpose="ingestion"):
                pytest.fail("second writer must not acquire the lock")
        assert error.value.code is ErrorCode.RECORD_LOCKED
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
    with pytest.raises(RecordLockError, match="locked") as error:
        with record_lock(record, purpose="ingestion"):
            pytest.fail("live owner must not be recovered")
    assert error.value.code is ErrorCode.RECORD_LOCKED
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

    with pytest.raises(RecordLockError, match="locked") as error:
        with record_lock(record, purpose="ingestion", stale_after_seconds=60.0):
            pytest.fail("fresh foreign lease must not be taken over")
    assert error.value.code is ErrorCode.RECORD_LOCKED
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
        first = lock_owner(lock_path(record))
        assert first is not None
        time.sleep(0.5)
        second = lock_owner(lock_path(record))
        assert second is not None
        assert second["token"] == first["token"]
        assert second["heartbeat"] > first["heartbeat"]

    assert not lock_path(record).exists()


def test_record_lock_refuses_a_live_same_host_owner_even_with_a_stale_heartbeat(
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

    with pytest.raises(RecordLockError, match=message_pattern(ErrorCode.RECORD_LOCKED)) as caught:
        with record_lock(record, purpose="ingestion", stale_after_seconds=60.0):
            pytest.fail("a live same-host owner must keep the lock")
    assert caught.value.code is ErrorCode.RECORD_LOCKED
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "stale"


def test_record_lock_reports_a_lease_lost_after_repeated_heartbeat_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    failed = threading.Event()
    attempts: list[str] = []

    def always_fail(path: Path, owner: dict[str, Any], token: str) -> None:
        del path, owner
        attempts.append(token)
        if len(attempts) >= lease.LEASE_HEARTBEAT_MAX_FAILURES:
            failed.set()
        raise OSError("disk gone")

    monkeypatch.setattr(lease, "refresh_in_place", always_fail)

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.LEASE_LOST)) as caught:
        with record_lock(record, purpose="meeting", stale_after_seconds=0.03):
            assert failed.wait(timeout=5)
    assert caught.value.code is ErrorCode.LEASE_LOST


def _hold_while_another_writer_takes_over(record: Path) -> None:
    with record_lock(record, purpose="meeting", stale_after_seconds=0.03):
        lock_path(record).write_text(
            json.dumps(
                {
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "purpose": "ingestion",
                    "token": "foreign",
                    "heartbeat": datetime.now(UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        time.sleep(0.3)


def test_record_lock_reports_a_lease_taken_over_while_held(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.LEASE_LOST)) as caught:
        _hold_while_another_writer_takes_over(record)
    assert caught.value.code is ErrorCode.LEASE_LOST
    assert json.loads(lock_path(record).read_text(encoding="utf-8"))["token"] == "foreign"


def test_record_lock_refuses_a_contender_past_the_original_deadline_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with record_lock(record, purpose="meeting", stale_after_seconds=1.5):
        time.sleep(2.0)
        monkeypatch.setattr(
            "fakeyou.persistence.lease.socket.gethostname", lambda: "other-container"
        )
        with pytest.raises(RecordLockError, match="locked") as error:
            with record_lock(record, purpose="ingestion", stale_after_seconds=1.5):
                pytest.fail("renewed lease must refuse a contender past the deadline")
        assert error.value.code is ErrorCode.RECORD_LOCKED


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
    failures: list[ErrorCode] = []

    def race() -> None:
        barrier.wait(timeout=10)
        try:
            with record_lock(record, purpose="meeting", stale_after_seconds=60.0) as token:
                winners.append(token)
                time.sleep(0.2)
        except RecordLockError as error:
            failures.append(error.code)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert [thread.is_alive() for thread in threads] == [False, False]

    assert len(winners) == 1
    assert failures == [ErrorCode.RECORD_LOCKED]
    assert not path.exists()


def test_stale_takeover_blocks_second_acquirer_until_release(tmp_path: Path) -> None:
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

    with record_lock(record, purpose="meeting", stale_after_seconds=60.0):
        with pytest.raises(
            RecordLockError, match=message_pattern(ErrorCode.RECORD_LOCKED)
        ) as error:
            with record_lock(record, purpose="meeting", stale_after_seconds=60.0):
                pass
        assert error.value.code is ErrorCode.RECORD_LOCKED
    with record_lock(record, purpose="meeting", stale_after_seconds=60.0):
        assert json.loads(path.read_text(encoding="utf-8"))["token"] != "stale"
    assert not path.exists()


def test_lease_acquisition_fails_while_lock_file_held(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(
            RecordLockError, match=message_pattern(ErrorCode.RECORD_LOCKED)
        ) as error:
            with record_lock(record, purpose="meeting", stale_after_seconds=60.0):
                pass
        assert error.value.code is ErrorCode.RECORD_LOCKED
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_record_lock_retries_the_heartbeat_after_a_transient_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    real_refresh = lease.refresh_in_place
    calls: list[str] = []

    def fail_once(path: Path, owner: dict[str, Any], token: str) -> bool:
        calls.append("refresh")
        if len(calls) == 1:
            raise OSError("transient write error")
        return real_refresh(path, owner, token)

    with record_lock(record, purpose="meeting", stale_after_seconds=0.3):
        first = json.loads(lock_path(record).read_text(encoding="utf-8"))["heartbeat"]
        monkeypatch.setattr(lease, "refresh_in_place", fail_once)
        time.sleep(0.6)
        second = json.loads(lock_path(record).read_text(encoding="utf-8"))["heartbeat"]
        assert len(calls) >= 2
        assert datetime.fromisoformat(second) > datetime.fromisoformat(first)

    assert not lock_path(record).exists()


def test_describe_lease_reports_absent_live_stale_and_corrupt(tmp_path: Path) -> None:
    from fakeyou.persistence.lease import describe_lease

    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)

    assert describe_lease(path) == "absent"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": "other-container",
                "pid": 42,
                "purpose": "meeting",
                "token": "live",
                "heartbeat": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    assert describe_lease(path, stale_after_seconds=60.0) == "live"

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
    assert describe_lease(path, stale_after_seconds=60.0) == "stale"

    path.write_text("{corrupt", encoding="utf-8")
    assert describe_lease(path, stale_after_seconds=60.0) == "corrupt"


def test_record_lock_refuses_a_corrupt_lock_file(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"
    path = lock_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(RecordLockError, match="corrupt") as error:
        with record_lock(record, purpose="forget"):
            pytest.fail("corrupt lock must fail closed")
    assert error.value.code is ErrorCode.RECORD_LOCK_CORRUPT
    assert path.read_text(encoding="utf-8") == "{corrupt"


def test_errors_raised_inside_a_record_lock_body_keep_their_own_code(tmp_path: Path) -> None:
    record = tmp_path / "meeting-record.jsonl"

    with (
        pytest.raises(LeaseError, match=message_pattern(ErrorCode.LEASE_LOCKED)) as caught,
        record_lock(record, purpose="debrief"),
    ):
        raise LeaseError(ErrorCode.LEASE_LOCKED, path=tmp_path)

    assert caught.value.code is ErrorCode.LEASE_LOCKED


def test_refresh_lease_reraises_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()

    def boom(path: Path, owner: dict[str, Any], token: str) -> None:
        del path, owner, token
        stop.set()
        raise KeyError("bug")

    monkeypatch.setattr(lease, "refresh_in_place", boom)

    with pytest.raises(KeyError, match="bug"):
        lease.refresh_lease(tmp_path / "lease.json", {"token": "token"}, "token", 0.03, stop)


def test_refresh_lease_retries_os_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    calls: list[str] = []

    def flaky(path: Path, owner: dict[str, Any], token: str) -> None:
        del path, owner, token
        calls.append("refresh")
        stop.set()
        raise OSError("disk full")

    monkeypatch.setattr(lease, "refresh_in_place", flaky)
    assert lease.refresh_lease(tmp_path / "lease.json", {"token": "token"}, "token", 0.03, stop)

    assert len(calls) == 1


def test_read_status_reports_corrupt_bytes_to_journal(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.json"
    path.write_text("{not json", encoding="utf-8")
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.INGESTION_STATUS_CORRUPT)):
        read_status(path, journal=journal)

    [row] = journal.events("ingestion_status_rejected")
    assert row["error_type"] == "ValidationError"
    assert row["path"] == str(path)


def test_terminal_outcome_keeps_the_stored_progress_of_an_existing_status() -> None:
    existing = IngestionStatus(
        meeting_id="meeting-1",
        group_id="twin_group",
        status="pending",
        windows=4,
        resumed_from=1,
        ingested=2,
        record_sha256="stored",
    )

    outcome = terminal_outcome("failed", "record_changed", "fresh", existing)

    assert outcome == IngestOutcome(
        state="failed",
        windows=4,
        resumed_from=1,
        ingested=2,
        facts=0,
        completed=False,
        record_sha256="stored",
        reason="record_changed",
    )


def test_terminal_outcome_without_a_status_uses_zero_progress_and_the_fallback_hash() -> None:
    outcome = terminal_outcome("interrupted", "stop_requested", "fresh")

    assert outcome == IngestOutcome(
        state="interrupted",
        windows=0,
        resumed_from=0,
        ingested=0,
        facts=0,
        completed=False,
        record_sha256="fresh",
        reason="stop_requested",
    )


@pytest.mark.parametrize(
    ("completed", "stop_requested", "expected"),
    [
        (True, False, ("completed", None)),
        (False, True, ("interrupted", "stop_requested")),
        (False, False, ("failed", "episode_failed")),
    ],
)
def test_ingest_state_maps_the_result_and_the_stop_signal(
    *, completed: bool, stop_requested: bool, expected: tuple[str, str | None]
) -> None:
    result = IngestResult(windows=1, resumed_from=0, ingested=0, facts=0, completed=completed)
    stop = asyncio.Event()
    if stop_requested:
        stop.set()

    assert ingest_state(result, stop) == expected


def test_finish_ingest_persists_the_outcome_and_summarises_it(tmp_path: Path) -> None:
    status_file = tmp_path / "ingestion.json"
    write_status(
        status_file,
        IngestionStatus(
            meeting_id="meeting-1",
            group_id="twin_group",
            status="pending",
            episode_uuids={0: "uuid-a"},
        ),
    )
    faults = FaultReporter(journal=RecordingJournal())

    summary = finish_ingest(
        status_file,
        IngestOutcome(
            state="completed",
            windows=2,
            resumed_from=0,
            ingested=2,
            facts=3,
            completed=True,
            record_sha256="abc",
            reason=None,
            episode_uuids={1: "uuid-b"},
        ),
        meeting_id="meeting-1",
        group_id="twin_group",
        faults=faults,
    )

    assert summary == IngestSummary(
        meeting_id="meeting-1",
        state="completed",
        windows=2,
        resumed_from=0,
        ingested=2,
        facts=3,
        completed=True,
        succeeded=True,
        stored=True,
    )
    status = read_status(status_file)
    assert status is not None
    assert status.episode_uuids == {0: "uuid-a", 1: "uuid-b"}


def test_finish_ingest_reports_failure_when_the_status_cannot_be_written(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    summary = finish_ingest(
        blocker / "ingestion.json",
        IngestOutcome(
            state="completed",
            windows=1,
            resumed_from=0,
            ingested=1,
            facts=1,
            completed=True,
            record_sha256="abc",
            reason=None,
        ),
        meeting_id="meeting-1",
        group_id="twin_group",
        faults=faults,
    )

    assert (summary.completed, summary.succeeded, summary.stored) == (True, False, False)
    assert len(journal.events("fault_recorded")) == 1


def test_finalized_prefix_ignores_the_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(b'{"kind": "header", "v": 4}\n{"kind": "transcript"')
    digest, count = finalized_prefix(path)
    assert count == len(b'{"kind": "header", "v": 4}\n')
    assert digest == sha256(b'{"kind": "header", "v": 4}\n').hexdigest()


def test_grown_record_keeps_the_stored_prefix_valid(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(b"one\n")
    digest, count = finalized_prefix(path)
    with path.open("ab") as handle:
        handle.write(b"two\n")
    assert hash_record_prefix(path, count) == digest


def test_finalized_prefix_of_a_record_without_newline_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(b'{"kind": "transcript"')

    assert finalized_prefix(path) == (sha256(b"").hexdigest(), 0)


def test_finalized_prefix_finds_a_newline_beyond_one_block(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    body = b"x" * 20000 + b"\n" + b"y" * 20000
    path.write_bytes(body)

    assert finalized_prefix(path) == (sha256(b"x" * 20000 + b"\n").hexdigest(), 20001)


def test_hash_record_prefix_rejects_a_record_shorter_than_the_prefix(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(b"one\n")

    with pytest.raises(ValueError, match="shorter than the hashed prefix"):
        hash_record_prefix(path, 10)


def test_status_without_record_bytes_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.json"
    write_status(path, pending_status(record_sha256="abc"))

    loaded = read_status(path)

    assert loaded is not None
    assert loaded.record_bytes is None
    assert pending_status(record_sha256="abc", record_bytes=12).record_bytes == 12


def test_finish_ingest_persists_the_prefix_byte_count(tmp_path: Path) -> None:
    path = tmp_path / "meeting-9" / "ingestion.json"
    finish_ingest(
        path,
        IngestOutcome(
            state="completed",
            windows=1,
            resumed_from=0,
            ingested=1,
            facts=0,
            completed=True,
            record_sha256="abc",
            reason=None,
            record_bytes=42,
        ),
        meeting_id="meeting-9",
        group_id="twin_group",
        faults=FaultReporter(journal=RecordingJournal()),
    )

    loaded = read_status(path)
    assert loaded is not None
    assert (loaded.record_sha256, loaded.record_bytes) == ("abc", 42)


def test_terminal_outcome_carries_the_stored_or_fallback_prefix_bytes() -> None:
    existing = pending_status(record_sha256="stored", record_bytes=7)

    assert terminal_outcome("failed", "record_changed", "fresh", existing).record_bytes == 7
    assert (
        terminal_outcome("failed", "provider_timeout", "fresh", fallback_bytes=9).record_bytes == 9
    )


def test_a_reader_never_sees_the_lease_half_written_by_the_heartbeat(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    with record_lock(record, purpose="meeting", stale_after_seconds=0.15):
        deadline = time.monotonic() + 0.5
        owners = []
        while time.monotonic() < deadline:
            owners.append(lock_owner(lock_path(record)))

    assert owners
    assert None not in owners
