import asyncio
from dataclasses import dataclass
from functools import partial
from typing import Literal

from fakeyou.conversation.participation_decider import Decider, DeciderTurn, ParticipationDecision
from fakeyou.conversation.participation_graph import (
    DeciderFailure,
    JevKey,
    classify_decider_failure,
    decider_window,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.turn_tracker import detect_turn_cue
from fakeyou.observability.faults import capture_error

JevRefreshOutcome = Literal["skipped", "decided", "failed"]

_CACHE_LIMIT = 16


@dataclass(frozen=True, slots=True)
class JevRefreshInput:
    event_ids: tuple[str, ...]
    turns: tuple[DeciderTurn, ...]
    cue: str
    mine: bool
    window_turns: int


def jev_cache_key(request: JevRefreshInput) -> JevKey:
    return (
        *decider_window(request.event_ids, request.window_turns),
        request.cue,
        request.mine,
    )


def turn_jev_key(event_ids: tuple[str, ...], cue: str, *, mine: bool, window_turns: int) -> JevKey:
    return jev_cache_key(
        JevRefreshInput(
            event_ids=event_ids, turns=(), cue=cue, mine=mine, window_turns=window_turns
        )
    )


class JevCache:
    def __init__(self) -> None:
        self._decisions: dict[JevKey, ParticipationDecision] = {}
        self._failures: dict[JevKey, DeciderFailure] = {}

    def decision(self, key: JevKey) -> ParticipationDecision | None:
        return self._decisions.get(key)

    def failure(self, key: JevKey) -> DeciderFailure | None:
        return self._failures.get(key)

    async def refresh(self, decider: Decider, request: JevRefreshInput) -> JevRefreshOutcome:
        key = jev_cache_key(request)
        if key in self._decisions:
            return "skipped"
        window = decider_window(request.turns, request.window_turns)
        result, error = await capture_error(partial(decider.decide, window))
        if error is not None:
            self._remember_failure(key, classify_decider_failure(error))
            return "failed"
        if result is None:
            return "skipped"
        self._remember_decision(key, result)
        return "decided"

    def _remember_decision(self, key: JevKey, decision: ParticipationDecision) -> None:
        if len(self._decisions) > _CACHE_LIMIT:
            self._decisions.clear()
        self._decisions[key] = decision

    def _remember_failure(self, key: JevKey, failure: DeciderFailure) -> None:
        if len(self._failures) > _CACHE_LIMIT:
            self._failures.clear()
        self._failures[key] = failure


@dataclass(frozen=True, slots=True)
class JevKick:
    text: str
    events: tuple[TranscriptEvent, ...]
    twin_names: tuple[str, ...]
    window_turns: int


class JevState:
    def __init__(self, *, speak_threshold: float = 0.70, clarify_threshold: float = 0.50) -> None:
        self.cache = JevCache()
        self.task: asyncio.Task[None] | None = None
        self.text = ""
        self._speak_threshold = speak_threshold
        self._clarify_threshold = clarify_threshold

    @property
    def speak_threshold(self) -> float:
        return self._speak_threshold

    @property
    def clarify_threshold(self) -> float:
        return self._clarify_threshold

    def decision(self, key: JevKey) -> ParticipationDecision | None:
        return self.cache.decision(key)

    def failure(self, key: JevKey) -> DeciderFailure | None:
        return self.cache.failure(key)

    def cancel(self) -> None:
        task = self.task
        self.task = None
        self.text = ""
        if task is not None and not task.done():
            task.cancel()

    async def wait_idle(self) -> None:
        task = self.task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


def _swallow_refresh_error(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


async def _refresh(state: JevState, decider: Decider, kick: JevKick) -> None:
    cue, _ = detect_turn_cue(kick.text, kick.twin_names)
    aliases = [name.strip().casefold() for name in kick.twin_names if name.strip()]
    spoken = {event.participant_id for event in kick.events} - {"unknown"}
    mine = cue == "explicit" or not any(alias in spoken for alias in aliases)
    turns = tuple(
        DeciderTurn(
            speaker=event.participant_id,
            text=event.text,
            mine=detect_turn_cue(event.text, kick.twin_names)[0] != "none",
        )
        for event in kick.events
    )
    await state.cache.refresh(
        decider,
        JevRefreshInput(
            event_ids=tuple(event.id for event in kick.events),
            turns=turns,
            cue=cue,
            mine=mine,
            window_turns=kick.window_turns,
        ),
    )


def kick_jev_refresh(
    state: JevState,
    decider: Decider,
    kick: JevKick,
    *,
    busy: bool,
    pending: bool,
) -> bool:
    if busy or pending or not kick.events:
        return False
    cue, _ = detect_turn_cue(kick.text, kick.twin_names)
    if cue == "none":
        return False
    running = state.task
    if running is not None and not running.done():
        if state.text == kick.text:
            return False
        running.cancel()
    state.text = kick.text
    task = asyncio.create_task(_refresh(state, decider, kick))
    task.add_done_callback(_swallow_refresh_error)
    state.task = task
    return True
