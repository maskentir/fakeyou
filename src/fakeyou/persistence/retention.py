import shutil
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintRepository
from fakeyou.foundation.visual_retention import PurgeReport, purge_visual_files
from fakeyou.observability.journal import Journal
from fakeyou.persistence.layout import AUDIO_DIR_NAME, SLOTS_FILE_NAME, VISUAL_DIR_NAME
from fakeyou.persistence.voiceprint_access import open_voiceprint_journal, open_voiceprint_store

DAY_SECONDS = 86400


def purge_audio_expired(*, root: Path | None, days: int | None, settings: Settings) -> PurgeReport:
    resolved_days = settings.audio.retention_days if days is None else days
    if resolved_days <= 0:
        raise FakeYouError(ErrorCode.AUDIO_RETENTION_DAYS_NOT_POSITIVE)
    resolved_root = _record_root(root, settings)
    if not resolved_root.is_dir():
        raise FakeYouError(ErrorCode.AUDIO_ROOT_MISSING, path=resolved_root)
    cutoff = time.time() - resolved_days * DAY_SECONDS
    report = PurgeReport()
    try:
        for meeting, audio_dir in _meeting_subdirectories(resolved_root, AUDIO_DIR_NAME):
            if _audio_mtime(audio_dir) < cutoff:
                report.freed_bytes += _directory_bytes(audio_dir)
                shutil.rmtree(audio_dir)
                report.removed.append(meeting)
            else:
                report.kept += 1
    except OSError as error:
        raise FakeYouError(ErrorCode.AUDIO_PURGE_FAILED, reason=type(error).__name__) from error
    return report


def purge_visual_expired(*, root: Path | None, days: int | None, settings: Settings) -> PurgeReport:
    raw_days = settings.perception.retention_raw_days if days is None else days
    if raw_days <= 0:
        raise FakeYouError(ErrorCode.VISUAL_RETENTION_DAYS_NOT_POSITIVE)
    resolved_root = _record_root(root, settings)
    if not resolved_root.is_dir():
        raise FakeYouError(ErrorCode.VISUAL_ROOT_MISSING, path=resolved_root)
    now = time.time()
    raw_cutoff = now - raw_days * DAY_SECONDS
    text_cutoff = now - settings.perception.retention_text_days * DAY_SECONDS
    total = PurgeReport()
    try:
        for meeting, visual_dir in _meeting_subdirectories(resolved_root, VISUAL_DIR_NAME):
            partial = purge_visual_files(
                visual_dir, raw_cutoff=raw_cutoff, text_cutoff=text_cutoff, prefix=meeting
            )
            total.removed.extend(partial.removed)
            total.kept += partial.kept
            total.freed_bytes += partial.freed_bytes
    except OSError as error:
        raise FakeYouError(ErrorCode.VISUAL_PURGE_FAILED, reason=type(error).__name__) from error
    return total


def purge_voiceprints_expired(
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
    journal: Journal | None = None,
) -> list[str]:
    with (
        open_voiceprint_store(settings, store) as owned_store,
        open_voiceprint_journal(settings, journal) as owned_journal,
    ):
        removed = owned_store.purge_expired(
            datetime.now(UTC), settings.identity.voiceprint_retention_days
        )
        for person_id in removed:
            owned_journal.record("voiceprint_expired", source="cli", person_id=person_id)
    return removed


def _record_root(root: Path | None, settings: Settings) -> Path:
    return settings.paths.record_dir if root is None else root


def _meeting_subdirectories(root: Path, name: str) -> Iterator[tuple[str, Path]]:
    for meeting in sorted(root.iterdir()):
        directory = meeting / name
        if directory.is_dir():
            yield meeting.name, directory


def _directory_bytes(directory: Path) -> int:
    return sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())


def _audio_mtime(audio_dir: Path) -> float:
    sidecar = audio_dir / SLOTS_FILE_NAME
    if sidecar.is_file():
        return sidecar.stat().st_mtime
    return audio_dir.stat().st_mtime
