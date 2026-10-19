from collections.abc import Iterator
from contextlib import contextmanager

from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.db.engine import open_database
from fakeyou.db.schema import require_current
from fakeyou.db.voiceprint_store import VoiceprintRepository, VoiceprintStore
from fakeyou.observability.journal import Journal, JournalFile, journal_path

_VOICEPRINT_JOURNAL_ID = parse_session_id("voiceprint", field="journal_id")


@contextmanager
def open_voiceprint_store(
    settings: Settings, store: VoiceprintRepository | None
) -> Iterator[VoiceprintRepository]:
    if store is not None:
        yield store
        return
    with open_database(settings) as engine:
        require_current(engine)
        yield VoiceprintStore(engine)


@contextmanager
def open_voiceprint_journal(settings: Settings, journal: Journal | None) -> Iterator[Journal]:
    if journal is not None:
        yield journal
        return
    owned = Journal(
        _VOICEPRINT_JOURNAL_ID,
        file=JournalFile(journal_path(settings.paths.journal_dir, _VOICEPRINT_JOURNAL_ID)),
    )
    try:
        yield owned
    finally:
        owned.close()
