import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.journal import Journal

RESUMED_MATCH = 0.7
EXTRA_WORDS_FOR_MORE = 3


class Answering(Protocol):
    async def ready_to_answer(self, text: str) -> bool: ...

    async def receive_final(self, event: TranscriptEvent) -> object: ...

    async def withdraw(self, *, reason: str) -> None: ...


def _words(text: str) -> list[str]:
    return "".join(ch for ch in text.casefold() if ch.isalnum() or ch.isspace()).split()


def says_more(text: str, committed: str) -> bool:
    before, after = _words(committed), _words(text)
    if len(after) <= len(before):
        return False
    return SequenceMatcher(None, before, after[: len(before)]).ratio() >= RESUMED_MATCH


@dataclass(slots=True)
class _Commit:
    event: TranscriptEvent
    text: str
    resumed: bool = False


class EarlyTurns:
    def __init__(
        self,
        answering: Answering,
        *,
        hold_seconds: float,
        build_event: Callable[[str], TranscriptEvent],
        journal: Journal,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._answering = answering
        self._hold_seconds = hold_seconds
        self._build_event = build_event
        self._journal = journal
        self._sleep = sleep
        self._text = ""
        self._version = 0
        self._timer: asyncio.Task[None] | None = None
        self._starting: asyncio.Task[None] | None = None
        self._commit: _Commit | None = None

    async def on_interim(self, text: str) -> None:
        self._text = text
        self._restart()
        commit = self._commit
        if commit is not None and not commit.resumed and says_more(text, commit.text):
            await self._cancel(commit, reason="speech_resumed")

    def on_speech(self, *, active: bool) -> None:
        self._restart()
        if active or not self._text.strip() or self._commit is not None:
            return
        self._timer = asyncio.create_task(self._after_hold(self._version, self._text))

    async def settle(self, final: TranscriptEvent) -> bool:
        commit, self._commit = self._commit, None
        self._text = ""
        self._restart()
        if commit is None:
            return False
        if (
            not commit.resumed
            and says_more(final.text, commit.text)
            and (len(_words(final.text)) - len(_words(commit.text)) >= EXTRA_WORDS_FOR_MORE)
        ):
            await self._cancel(commit, reason="final_says_more")
        if commit.resumed:
            return False
        self._journal.record(
            "early_turn_verified",
            source="agent",
            transcript_id=final.id,
            early_transcript_id=commit.event.id,
            early_words=len(_words(commit.text)),
            final_words=len(_words(final.text)),
        )
        return True

    async def close(self) -> None:
        tasks = [task for task in (self._timer, self._starting) if task is not None]
        self._timer = self._starting = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _restart(self) -> None:
        self._version += 1
        timer, self._timer = self._timer, None
        if timer is not None and not timer.done():
            timer.cancel()

    async def _after_hold(self, version: int, text: str) -> None:
        await self._sleep(self._hold_seconds)
        if version != self._version:
            return
        if not await self._answering.ready_to_answer(text):
            return
        if version != self._version or self._commit is not None:
            return
        event = self._build_event(text)
        self._commit = _Commit(event, text)
        self._starting, self._timer = self._timer, None
        self._journal.record(
            "early_turn_started",
            source="agent",
            transcript_id=event.id,
            words=len(_words(text)),
            hold_ms=round(self._hold_seconds * 1000),
        )
        await self._answering.receive_final(event)

    async def _cancel(self, commit: _Commit, *, reason: str) -> None:
        commit.resumed = True
        self._journal.record(
            "early_turn_cancelled",
            source="agent",
            transcript_id=commit.event.id,
            reason=reason,
        )
        await self._answering.withdraw(reason=reason)
