from pathlib import Path

from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.foundation.visual_retention import PurgeReport
from fakeyou.persistence.retention import purge_audio_expired, purge_visual_expired


def audio_purge_expired(*, root: Path | None, days: int | None, settings: Settings) -> None:
    report = purge_audio_expired(root=root, days=days, settings=settings)
    emit_json({"removed": report.removed, "kept": report.kept, "freed_bytes": report.freed_bytes})


def visual_purge_expired(*, root: Path | None, days: int | None, settings: Settings) -> PurgeReport:
    report = purge_visual_expired(root=root, days=days, settings=settings)
    emit_json({"removed": report.removed, "kept": report.kept, "freed_bytes": report.freed_bytes})
    return report
