from dataclasses import dataclass, field
from pathlib import Path

VISUAL_RAW_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
VISUAL_TEXT_SUFFIXES = frozenset({".jsonl", ".json", ".txt"})


@dataclass(slots=True)
class PurgeReport:
    removed: list[str] = field(default_factory=list)
    kept: int = 0
    freed_bytes: int = 0


def _kind_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in VISUAL_RAW_SUFFIXES:
        return "raw"
    if suffix in VISUAL_TEXT_SUFFIXES:
        return "text"
    return None


def purge_visual_files(
    directory: Path, *, raw_cutoff: float, text_cutoff: float, prefix: str
) -> PurgeReport:
    report = PurgeReport()
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        kind = _kind_for(path)
        if kind is None:
            report.kept += 1
            continue
        cutoff = raw_cutoff if kind == "raw" else text_cutoff
        stat = path.stat()
        if stat.st_mtime >= cutoff:
            report.kept += 1
            continue
        report.freed_bytes += stat.st_size
        path.unlink()
        report.removed.append(f"{prefix}/{path.name}" if prefix else path.name)
    return report
