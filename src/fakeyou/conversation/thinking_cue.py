import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import cycle
from typing import Protocol, runtime_checkable

from fakeyou.conversation.turn_events import JournalSink
from fakeyou.foundation.resources import read_package_text

THINKING_CUE_LINES: dict[str, tuple[str, ...]] = {
    language: tuple(lines)
    for language, lines in json.loads(read_package_text("spoken_lines.json"))[
        "thinking_cue"
    ].items()
}


@runtime_checkable
class CueSpeaker(Protocol):
    @property
    def is_speaking(self) -> bool: ...

    async def play_cue(self, text: str) -> bool: ...


@dataclass(slots=True)
class ThinkingCue:
    lines: tuple[str, ...]
    after_seconds: float
    _rotation: Iterator[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a thinking cue needs at least one line")
        if self.after_seconds < 0:
            raise ValueError("a thinking cue delay cannot be negative")
        self._rotation = cycle(self.lines)

    def next_line(self) -> str:
        return next(self._rotation)


def thinking_cue_for(language: str, after_seconds: float | None) -> ThinkingCue | None:
    if after_seconds is None:
        return None
    return ThinkingCue(THINKING_CUE_LINES[language], after_seconds)


@dataclass(frozen=True, slots=True)
class CueTurn:
    transcript_id: str
    blocked: Callable[[], str | None]


async def cue_when_slow(
    cue: ThinkingCue, speaker: CueSpeaker, journal: JournalSink, turn: CueTurn
) -> None:
    await asyncio.sleep(cue.after_seconds)
    reason = turn.blocked()
    if reason is None and speaker.is_speaking:
        reason = "speaking"
    if reason is not None:
        _record_skip(journal, turn, reason)
        return
    journal.record("thinking_cue_requested", source="agent", transcript_id=turn.transcript_id)
    if not await speaker.play_cue(cue.next_line()):
        _record_skip(journal, turn, "unavailable")


def _record_skip(journal: JournalSink, turn: CueTurn, reason: str) -> None:
    journal.record(
        "thinking_cue_skipped", source="agent", transcript_id=turn.transcript_id, reason=reason
    )
