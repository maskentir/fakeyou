from pathlib import Path

from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.persistence.retention import (
    PurgeReport,
    purge_audio_expired,
    purge_perception_expired,
)


def audio_purge_expired(*, root: Path | None, days: int | None, settings: Settings) -> None:
    report = purge_audio_expired(root=root, days=days, settings=settings)
    emit_json({"removed": report.removed, "kept": report.kept, "freed_bytes": report.freed_bytes})


def perception_purge_expired(
    *, root: Path | None, days: int | None, settings: Settings
) -> PurgeReport:
    report = purge_perception_expired(root=root, days=days, settings=settings)
    emit_json({"removed": report.removed, "kept": report.kept, "freed_bytes": report.freed_bytes})
    return report
