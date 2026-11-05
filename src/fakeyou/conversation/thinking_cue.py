import asyncio
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import cycle
from typing import Protocol, runtime_checkable

from fakeyou.foundation.resources import read_package_text
from fakeyou.observability.journal import JournalSink

THINKING_CUE_LINES: dict[str, tuple[str, ...]] = {
    language: tuple(lines)
    for language, lines in json.loads(read_package_text("spoken_lines.json"))[
        "thinking_cue"
    ].items()
}

OPENER_LINES: dict[str, tuple[str, ...]] = {
    language: tuple(lines)
    for language, lines in json.loads(read_package_text("spoken_lines.json"))["opener"].items()
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
    lines_by_language: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    current_language: Callable[[], str] | None = None
    opener_lines: tuple[str, ...] = ()
    opener_by_language: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: OPENER_LINES)
    _rotation: dict[str, Iterator[str]] = field(init=False, repr=False, default_factory=dict)
    _opener_rotation: dict[str, Iterator[str]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a thinking cue needs at least one line")
        if self.after_seconds < 0:
            raise ValueError("a thinking cue delay cannot be negative")

    def language(self) -> str:
        if self.current_language is None:
            return ""
        return self.current_language()

    def _next(
        self,
        selected: Mapping[str, tuple[str, ...]],
        fallback: tuple[str, ...],
        rotation: dict[str, Iterator[str]],
    ) -> str:
        language = self.language()
        lines = selected.get(language) or fallback
        if not lines:
            raise ValueError("a thinking cue needs at least one line")
        cycle_for = rotation.get(language)
        if cycle_for is None:
            cycle_for = cycle(lines)
            rotation[language] = cycle_for
        return next(cycle_for)

    def next_line(self) -> str:
        return self._next(self.lines_by_language, self.lines, self._rotation)

    def next_opener_line(self) -> str:
        language = self.language()
        fallback = self.opener_by_language.get(language) or self.opener_lines or self.lines
        return self._next(self.opener_by_language, fallback, self._opener_rotation)

    def prepared_lines(self) -> tuple[str, ...]:
        language = self.language()
        opener = self.opener_by_language.get(language) or self.opener_lines
        return (*opener, *self.lines)


def thinking_cue_for(
    language: str,
    after_seconds: float | None,
    current_language: Callable[[], str] | None = None,
) -> ThinkingCue | None:
    if after_seconds is None:
        return None
    return ThinkingCue(
        THINKING_CUE_LINES[language],
        after_seconds,
        lines_by_language=THINKING_CUE_LINES,
        current_language=current_language,
        opener_lines=OPENER_LINES[language],
    )


@dataclass(slots=True)
class CueState:
    played: bool = False


@dataclass(frozen=True, slots=True)
class CueTurn:
    transcript_id: str
    blocked: Callable[[], str | None]
    state: CueState = field(default_factory=CueState)
    source: str = "meeting_audio"


async def speak_opener(
    cue: ThinkingCue, speaker: CueSpeaker, journal: JournalSink, turn: CueTurn
) -> None:
    if turn.source == "meeting_attention":
        return
    reason = turn.blocked()
    if reason is None and speaker.is_speaking:
        reason = "speaking"
    if reason is not None:
        _record_skip(journal, turn, reason)
        return
    journal.record("thinking_cue_requested", source="agent", transcript_id=turn.transcript_id)
    if not await speaker.play_cue(cue.next_opener_line()):
        _record_skip(journal, turn, "unavailable")
        return
    turn.state.played = True


async def cue_when_slow(
    cue: ThinkingCue, speaker: CueSpeaker, journal: JournalSink, turn: CueTurn
) -> None:
    await asyncio.sleep(cue.after_seconds)
    if turn.source == "meeting_attention":
        _record_skip(journal, turn, "attention_lane")
        return
    reason = turn.blocked()
    if reason is None and speaker.is_speaking:
        reason = "speaking"
    if reason is not None:
        _record_skip(journal, turn, reason)
        return
    journal.record("thinking_cue_requested", source="agent", transcript_id=turn.transcript_id)
    if not await speaker.play_cue(cue.next_line()):
        _record_skip(journal, turn, "unavailable")
        return
    turn.state.played = True


def _record_skip(journal: JournalSink, turn: CueTurn, reason: str) -> None:
    journal.record(
        "thinking_cue_skipped", source="agent", transcript_id=turn.transcript_id, reason=reason
    )
