from __future__ import annotations

from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.observability.journal import journal_path
from fakeyou.persistence.voiceprint_access import open_voiceprint_journal, open_voiceprint_store
from tests._error_support import message_pattern
from tests._persistence_fakes import RecordingJournal
from tests._voiceprint_fakes import InMemoryVoiceprints


def test_open_store_yields_the_injected_store_without_a_database(settings: Settings) -> None:
    settings.database.url = None
    store = InMemoryVoiceprints()

    with open_voiceprint_store(settings, store) as opened:
        assert opened is store


def test_open_store_without_injection_requires_a_database_url(settings: Settings) -> None:
    settings.database.url = None

    with (
        pytest.raises(FakeYouError, match=message_pattern(ErrorCode.DATABASE_URL_MISSING)),
        open_voiceprint_store(settings, None),
    ):
        pass


def test_open_journal_yields_the_injected_journal_and_leaves_it_open(settings: Settings) -> None:
    journal = RecordingJournal()

    with open_voiceprint_journal(settings, journal) as opened:
        opened.record("voiceprint_cleared", source="cli", person_id="alice")

    assert opened is journal
    assert [row["person_id"] for row in journal.events("voiceprint_cleared")] == ["alice"]


def test_open_journal_without_injection_writes_the_voiceprint_journal(
    settings: Settings, tmp_path: Path
) -> None:
    settings.paths.journal_dir = tmp_path / "journals"
    settings.paths.journal_dir.mkdir()
    journal_id = parse_session_id("voiceprint", field="journal_id")

    with open_voiceprint_journal(settings, None) as journal:
        journal.record("voiceprint_cleared", source="cli", person_id="alice")

    written = journal_path(settings.paths.journal_dir, journal_id).read_text()
    assert '"voiceprint_cleared"' in written
    assert '"alice"' in written
