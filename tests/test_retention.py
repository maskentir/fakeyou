from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.foundation.visual_retention import PurgeReport
from fakeyou.persistence.retention import (
    purge_audio_expired,
    purge_visual_expired,
    purge_voiceprints_expired,
)
from tests._error_support import message_pattern
from tests._persistence_fakes import RecordingJournal
from tests._voiceprint_fakes import InMemoryVoiceprints
from tests._voiceprint_support import stored_record

DAY = 86400.0


def aged_directory(directory: Path, age_days: float, payload: bytes) -> None:
    directory.mkdir(parents=True)
    (directory / "slot-0.wav").write_bytes(payload)
    stamp = time.time() - age_days * DAY
    for path in [*directory.rglob("*"), directory]:
        os.utime(path, (stamp, stamp))


def test_audio_purge_returns_removed_meetings_and_kept_count(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "meetings"
    aged_directory(root / "meeting-old" / "audio", 40.0, b"\x00\x01" * 80)
    aged_directory(root / "meeting-new" / "audio", 1.0, b"\x00\x01" * 80)

    report = purge_audio_expired(root=root, days=None, settings=settings)

    assert isinstance(report, PurgeReport)
    assert report.removed == ["meeting-old"]
    assert report.kept == 1
    assert report.freed_bytes == 160
    assert not (root / "meeting-old" / "audio").exists()
    assert (root / "meeting-new" / "audio").exists()


def test_audio_purge_rejects_non_positive_days(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="positive") as rejected:
        purge_audio_expired(root=tmp_path, days=0, settings=settings)

    assert rejected.value.code is ErrorCode.AUDIO_RETENTION_DAYS_NOT_POSITIVE


def test_visual_purge_returns_report_with_prefixed_removals(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "meetings"
    visual = root / "meeting-old" / "visual"
    visual.mkdir(parents=True)
    frame = visual / "shot-old.jpg"
    frame.write_bytes(b"\xff\xd8old-frame")
    stamp = time.time() - 9.0 * DAY
    os.utime(frame, (stamp, stamp))

    report = purge_visual_expired(root=root, days=None, settings=settings)

    assert report.removed == ["meeting-old/shot-old.jpg"]
    assert report.freed_bytes == len(b"\xff\xd8old-frame")
    assert not frame.exists()


def test_voiceprint_purge_returns_removed_ids_and_journals_each(settings: Settings) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0), last_used_at=datetime(2026, 1, 1, tzinfo=UTC)))
    store.save(stored_record("bob", (0.0, 1.0), last_used_at=datetime.now(UTC)))
    journal = RecordingJournal()

    removed = purge_voiceprints_expired(settings=settings, store=store, journal=journal)

    assert removed == ["alice"]
    assert [row["person_id"] for row in journal.events("voiceprint_expired")] == ["alice"]
    assert [record.person_id for record in store.load_all()] == ["bob"]


def test_voiceprint_purge_without_store_requires_database_url(settings: Settings) -> None:
    settings.database.url = None

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.DATABASE_URL_MISSING)):
        purge_voiceprints_expired(settings=settings)
