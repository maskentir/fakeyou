from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintStore
from fakeyou.identity.enrollment import (
    ClipVerdict,
    VoiceprintDeps,
    delete_voiceprint,
    list_voiceprints,
    verify_voiceprint_clip,
)
from tests._error_support import message_pattern
from tests._persistence_fakes import RecordingJournal
from tests._voiceprint_fakes import InMemoryVoiceprints
from tests._voiceprint_support import (
    LAST_USED_AT,
    ScriptedEmbedder,
    saved,
    stored_record,
    write_tone_clip,
)


def test_verify_high_score_journals_verified_and_touches_print(
    settings: Settings, tmp_path: Path
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    before = datetime.now(UTC)
    verdict = verify_voiceprint_clip(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert verdict.person_id == "alice"
    assert verdict.outcome == "verified"
    assert verdict.score == pytest.approx(1.0, abs=1e-6)
    [row] = journal.events("voiceprint_decided")
    assert row["person_id"] == "alice"
    assert row["outcome"] == "verified"
    assert row["score"] == pytest.approx(1.0, abs=1e-6)
    assert saved(store, "alice").last_used_at >= before


def test_verify_low_score_journals_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    verdict = verify_voiceprint_clip(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(0.0, 1.0)])),
    )

    assert verdict.outcome == "unknown"
    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert saved(store, "alice").last_used_at == LAST_USED_AT


def test_verify_close_runners_up_reports_conflict(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("bob", (0.95, 0.3122)))
    journal = RecordingJournal()

    verdict = verify_voiceprint_clip(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert verdict.outcome == "conflict"
    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "conflict"
    assert saved(store, "alice").last_used_at == LAST_USED_AT


def test_verify_negative_winner_journals_unknown_without_fault(
    settings: Settings, tmp_path: Path
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("twin-voice", (0.9, 0.4359), is_negative=True))
    journal = RecordingJournal()

    verdict = verify_voiceprint_clip(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(0.95, 0.3122)])),
    )

    assert verdict.outcome == "unknown"
    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert journal.events() == journal.events("voiceprint_decided")
    assert [record.person_id for record in store.load_all()] == ["alice", "twin-voice"]
    assert saved(store, "alice").last_used_at == LAST_USED_AT


def test_verify_named_negative_print_reports_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("twin-voice", (0.9, 0.4359), is_negative=True))
    journal = RecordingJournal()

    verdict = verify_voiceprint_clip(
        "twin-voice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(0.9, 0.4359)])),
    )

    assert verdict.person_id == "twin-voice"
    assert verdict.outcome == "unknown"
    assert saved(store, "twin-voice").last_used_at == LAST_USED_AT


def test_verify_missing_name_journals_unknown_with_zero_margin(
    settings: Settings, tmp_path: Path
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    verdict = verify_voiceprint_clip(
        "ghost",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert verdict == ClipVerdict(person_id="ghost", outcome="unknown", score=-1.0, margin=0.0)
    [row] = journal.events("voiceprint_decided")
    assert row["person_id"] == "ghost"
    assert row["outcome"] == "unknown"


def test_verify_empty_clip_rejected_as_too_short(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "empty.wav", seconds=0.0)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.VOICEPRINT_CLIP_TOO_SHORT)
    ) as error:
        verify_voiceprint_clip("alice", str(clip), settings=settings)

    assert error.value.code is ErrorCode.VOICEPRINT_CLIP_TOO_SHORT


def test_delete_present_print_clears_and_journals(settings: Settings) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    delete_voiceprint("alice", settings=settings, store=store, journal=journal)

    assert store.load_all() == []
    [row] = journal.events("voiceprint_cleared")
    assert row["person_id"] == "alice"


def test_delete_missing_print_raises_without_journal(settings: Settings) -> None:
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match='no voiceprint enrolled for "ghost"') as caught:
        delete_voiceprint("ghost", settings=settings, store=InMemoryVoiceprints(), journal=journal)

    assert caught.value.code is ErrorCode.VOICEPRINT_NOT_ENROLLED
    assert journal.events("voiceprint_cleared") == []


def test_list_returns_records_in_store_order(settings: Settings) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("twin-voice", (0.0, 1.0), is_negative=True))

    records = list_voiceprints(settings=settings, store=store)

    assert [record.person_id for record in records] == ["alice", "twin-voice"]
    assert records[1].is_negative is True


def test_list_without_store_and_without_database_url_raises(settings: Settings) -> None:
    settings.database.url = None

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_URL_MISSING),
    ) as caught:
        list_voiceprints(settings=settings, store=None)

    assert caught.value.code is ErrorCode.DATABASE_URL_MISSING


def test_list_against_unmigrated_database_raises(
    settings: Settings, blank_database_url: str
) -> None:
    settings.database.url = SecretStr(blank_database_url)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.DATABASE_SCHEMA_EMPTY)
    ) as caught:
        list_voiceprints(settings=settings, store=None)

    assert caught.value.code is ErrorCode.DATABASE_SCHEMA_EMPTY


def test_list_reads_prints_from_migrated_database(
    migrated_settings: Settings, migrated_engine: Engine
) -> None:
    VoiceprintStore(migrated_engine).save(stored_record("alice", (1.0, 0.0)))

    [record] = list_voiceprints(settings=migrated_settings, store=None)

    assert record.person_id == "alice"
    assert record.last_used_at == LAST_USED_AT
