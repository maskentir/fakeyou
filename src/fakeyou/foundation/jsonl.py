import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class JsonlRead:
    rows: list[dict[str, Any]]
    skipped: int


def read_jsonl(path: Path) -> JsonlRead:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return JsonlRead(rows=[], skipped=0)
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            skipped += 1
    return JsonlRead(rows=rows, skipped=skipped)
