import json
import shutil
import sys
import time
from pathlib import Path
from typing import NoReturn

from fakeyou.cli.errors import CliError
from fakeyou.settings import Settings
from fakeyou.visual_retention import PurgeReport, purge_visual_files

DAY_SECONDS = 86400


def _reject(message: str) -> NoReturn:
    print(f"fakeyou: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _directory_bytes(directory: Path) -> int:
    return sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())


def _audio_mtime(audio_dir: Path) -> float:
    sidecar = audio_dir / "slots.json"
    if sidecar.is_file():
        return sidecar.stat().st_mtime
    return audio_dir.stat().st_mtime


def audio_purge_expired(*, root: Path | None, days: int | None, settings: Settings) -> None:
    resolved_days = settings.audio.retention_days if days is None else days
    if resolved_days <= 0:
        raise CliError("--days must be positive")
    resolved_root = settings.paths.record_dir if root is None else root
    if not resolved_root.is_dir():
        _reject(f"audio root is missing: {resolved_root}")
    cutoff = time.time() - resolved_days * DAY_SECONDS
    removed: list[str] = []
    kept = 0
    freed_bytes = 0
    for meeting in sorted(resolved_root.iterdir()):
        audio_dir = meeting / "audio"
        if not audio_dir.is_dir():
            continue
        if _audio_mtime(audio_dir) < cutoff:
            freed_bytes += _directory_bytes(audio_dir)
            shutil.rmtree(audio_dir)
            removed.append(meeting.name)
        else:
            kept += 1
    print(json.dumps({"removed": removed, "kept": kept, "freed_bytes": freed_bytes}))


def visual_purge_expired(*, root: Path | None, days: int | None, settings: Settings) -> PurgeReport:
    raw_days = settings.visual.retention_raw_days if days is None else days
    if raw_days <= 0:
        raise CliError("--days must be positive")
    resolved_root = settings.paths.record_dir if root is None else root
    if not resolved_root.is_dir():
        _reject(f"visual root is missing: {resolved_root}")
    now = time.time()
    raw_cutoff = now - raw_days * DAY_SECONDS
    text_cutoff = now - settings.visual.retention_text_days * DAY_SECONDS
    total = PurgeReport()
    for meeting in sorted(resolved_root.iterdir()):
        visual_dir = meeting / "visual"
        if not visual_dir.is_dir():
            continue
        partial = purge_visual_files(
            visual_dir, raw_cutoff=raw_cutoff, text_cutoff=text_cutoff, prefix=meeting.name
        )
        total.removed.extend(partial.removed)
        total.kept += partial.kept
        total.freed_bytes += partial.freed_bytes
    print(
        json.dumps({"removed": total.removed, "kept": total.kept, "freed_bytes": total.freed_bytes})
    )
    return total
