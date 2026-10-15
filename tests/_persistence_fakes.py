from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.observability.journal import Journal


class RecordingJournal(Journal):
    def __init__(self, meeting_id: str = "meeting-1", **kwargs: Any) -> None:
        self.lines: list[str] = []
        super().__init__(meeting_id, emit=self.lines.append, **kwargs)

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        rows = [json.loads(line) for line in self.lines]
        return [row for row in rows if name is None or row["event"] == name]


class FakeSaverSession:
    def __init__(self) -> None:
        self.saver = InMemorySaver()

    @asynccontextmanager
    async def factory(self) -> AsyncIterator[InMemorySaver]:
        yield self.saver


def write_journal(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


FINISHED_ROWS: list[dict[str, Any]] = [
    {
        "event": "join_requested",
        "meeting_id": "meeting-9",
        "at": "2026-10-05T02:00:00+00:00",
        "source": "runtime",
    },
    {
        "event": "session_finished",
        "meeting_id": "meeting-9",
        "at": "2026-10-05T02:10:00+00:00",
        "source": "cli",
        "phase": "ended",
        "terminal_reason": "ended_by_host",
        "exit_code": 0,
    },
]
