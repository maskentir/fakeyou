import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.participation_decision import ParticipationDecision
from fakeyou.conversation.participation_graph import JudgeThresholds, decision_of
from fakeyou.conversation.turn_events import TurnEvent
from fakeyou.conversation.turn_graph import (
    TurnGraphState,
    TurnInput,
    TurnRuntime,
    cancel_speech,
    record_barge_in,
)
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.participation import (
    ParticipationContext,
    is_vocative,
    mentions_whole_phrase,
)
from fakeyou.foundation.resources import read_package_text
from fakeyou.foundation.transcript import TranscriptEvent, transcript_stamp
from fakeyou.foundation.triggers import ReplyChannel
from fakeyou.observability.faults import capture_error

_ADMITTED = frozenset({"speak", "clarify"})
_STOP_DATA: dict[str, list[str]] = json.loads(read_package_text("stop_phrases.json"))
_STOP_STRONG = frozenset(_STOP_DATA["strong"])
_STOP_FILLER = frozenset(_STOP_DATA["filler"])
_STOP_PHRASES = frozenset(_STOP_DATA["phrases"])
_STOP_MAX_TOKENS = 6
_OVERLAP_WINDOW_SECONDS = 20.0


def _tokens(text: str) -> list[str]:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text.casefold())
    return cleaned.split()


def is_stop_command(text: str, names: Sequence[str]) -> bool:
    tokens = _tokens(text)
    if not tokens or len(tokens) > _STOP_MAX_TOKENS:
        return False
    name_tokens = {token for name in names for token in _tokens(name)}
    tokens = [token for token in tokens if token not in name_tokens]
    if not tokens:
        return False
    if " ".join(tokens) in _STOP_PHRASES:
        return True
    return all(token in _STOP_STRONG or token in _STOP_FILLER for token in tokens) and any(
        token in _STOP_STRONG for token in tokens
    )


@dataclass(frozen=True, slots=True)
class IngressTiming:
    barge_in_min_words: int = DEFAULTS.conversation.barge_in_min_words
    clock: Callable[[], float] = monotonic


@dataclass(frozen=True, slots=True)
class PolicyOptions:
    enabled: bool = False
    others_speaking: Callable[[], bool] | None = None


def addressing_phrases(phrases: Sequence[str]) -> tuple[str, ...]:
    return tuple(phrase.strip().casefold() for phrase in phrases if phrase.strip())


def judge_yields_the_floor(judgement: TurnJudgement, thresholds: JudgeThresholds) -> bool:
    if judgement.backchannel or judgement.addressee != "twin":
        return False
    return judgement.confidence >= thresholds.barge_in


class TurnIngress:
    def __init__(
        self,
        lifecycle: TurnLifecycle,
        *,
        addressing_names: Sequence[str],
        wake_phrases: Sequence[str] = (),
        timing: IngressTiming | None = None,
        barge_in_judge_enabled: bool = True,
    ) -> None:
        resolved = IngressTiming() if timing is None else timing
        self._lifecycle = lifecycle
        self._addressing_names = addressing_phrases(addressing_names)
        self._wake_phrases = addressing_phrases(wake_phrases)
        self._participant_policy = False
        self._others_speaking_signal: Callable[[], bool] | None = None
        self._barge_in_min_words = resolved.barge_in_min_words
        self._clock = resolved.clock
        self._seen: set[str] = set()
        self._in_flight = False
        self._barge_in_judge_enabled = barge_in_judge_enabled
        self._judge_tasks: set[asyncio.Task[None]] = set()
        self._last_attention: str | None = None
        self._last_final: tuple[str, str, float] | None = None

    def configure_policy(self, options: PolicyOptions) -> None:
        if options.enabled and not self._wake_phrases:
            raise ValueError("participant policy requires configured twin aliases")
        self._participant_policy = options.enabled
        self._others_speaking_signal = options.others_speaking

    @property
    def lifecycle(self) -> TurnLifecycle:
        return self._lifecycle

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return self._lifecycle.events

    async def receive_final(self, event: TranscriptEvent) -> ParticipationDecision | None:
        runtime = self._open_final(event)
        if runtime is None:
            return None
        output = await self._invoke_final(event, runtime)
        self._lifecycle.start_requested()
        decision: ParticipationDecision | None = output["participation"]
        if decision is not None and decision.decision in _ADMITTED:
            self._last_final = (
                event.participant_id,
                " ".join(_tokens(event.text)),
                self._clock(),
            )
        if decision is not None and decision.decision not in _ADMITTED:
            await self._yield_floor(event.text, decision)
        return decision

    def receive_chat(self, event: TranscriptEvent) -> bool:
        if not event.text.strip() or not self._mentioned(event.text):
            return False
        return self._lifecycle.defer(event, channel="chat", needs_quiet_floor=False)

    def receive_attention(self, event: TranscriptEvent, *, channel: ReplyChannel) -> bool:
        if not event.text.strip():
            return False
        if event.text == self._last_attention:
            return False
        self._last_attention = event.text
        return self._lifecycle.defer(event, channel=channel, needs_quiet_floor=channel == "voice")

    async def handle_final(self, event: TranscriptEvent) -> ParticipationDecision | None:
        decision = await self.receive_final(event)
        await self._lifecycle.drained()
        return decision

    async def ready_to_answer(self, text: str) -> bool:
        lifecycle = self._lifecycle
        judging = lifecycle.judging
        session = lifecycle.session
        if judging is None or lifecycle.is_closed or lifecycle.is_failed or not text.strip():
            return False
        window = judging.windows.for_interim(
            text, twin_speaking=session is not None and session.is_speaking
        )
        judgement, _ = await capture_error(partial(judging.cache.judgement, window))
        if judgement is None or not judgement.finished:
            return False
        verdict = decision_of(judgement, judging.thresholds, named=self._addressed(text))
        return verdict.decision in _ADMITTED

    async def withdraw(self, *, reason: str) -> None:
        lifecycle = self._lifecycle
        await cancel_speech(lifecycle.floor, lifecycle.journal, reason=reason)

    async def settled(self) -> None:
        tasks = list(self._judge_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_interim(self, text: str) -> None:
        lifecycle = self._lifecycle
        if text.strip():
            lifecycle.note_activity()
        window: JudgeWindow | None = None
        if lifecycle.runtime is not None and not lifecycle.is_closed and not lifecycle.is_failed:
            window = await self._prefetch(text)
        session = lifecycle.session
        if lifecycle.is_closed or lifecycle.is_failed or not lifecycle.barge_in or session is None:
            return
        words = len(text.split())
        if is_stop_command(text, (*self._addressing_names, *self._wake_phrases)):
            if self._twin_holds_floor(session) and text != lifecycle.barge.last_interim:
                lifecycle.barge.last_interim = text
                await self._yield_the_floor(words, decided_by="stop_lexicon")
            return
        if words < self._barge_in_min_words:
            return
        active = lifecycle.floor.active
        if not session.is_speaking and (active is None or active.task.done()):
            return
        latch = lifecycle.barge
        if text == latch.last_interim:
            return
        latch.last_interim = text
        judging = lifecycle.judging
        if judging is None or not self._barge_in_judge_enabled:
            await self._yield_the_floor(words, decided_by=None)
            return
        self._ask_the_judge(text, words, window)

    def _twin_holds_floor(self, session: Any) -> bool:
        active = self._lifecycle.floor.active
        return bool(session.is_speaking or (active is not None and not active.task.done()))

    def _ask_the_judge(self, text: str, words: int, window: JudgeWindow | None) -> None:
        generation = self._lifecycle.floor.generation
        task = asyncio.create_task(self._judge_the_barge_in(text, words, window, generation))
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)

    async def _judge_the_barge_in(
        self, text: str, words: int, window: JudgeWindow | None, generation: int
    ) -> None:
        lifecycle = self._lifecycle
        judging = lifecycle.judging
        if judging is None:
            return
        session = lifecycle.session
        if window is None:
            window = judging.windows.for_interim(
                text, twin_speaking=session is not None and session.is_speaking
            )
        judgement, _ = await capture_error(partial(judging.cache.judgement, window))
        if generation != lifecycle.floor.generation:
            lifecycle.journal.record(
                "barge_in_rejected", source="agent", reason="stale_generation", words=words
            )
            return
        if judgement is None:
            await self._yield_the_floor(words, decided_by="judge_unavailable")
            return
        if not judge_yields_the_floor(judgement, judging.thresholds):
            lifecycle.journal.record(
                "barge_in_rejected",
                source="agent",
                reason="judge",
                words=words,
                addressee=judgement.addressee,
                confidence=judgement.confidence,
            )
            return
        await self._yield_the_floor(words, decided_by="judge")

    async def _yield_the_floor(self, words: int, *, decided_by: str | None) -> None:
        lifecycle = self._lifecycle
        session = lifecycle.session
        latch = lifecycle.barge
        if session is None:
            return
        active = lifecycle.floor.active
        if not session.is_speaking and (active is None or active.task.done()):
            return
        if not latch.interrupted:
            latch.interrupted = True
            record_barge_in(
                lifecycle.journal, "interim", words, decided_by=decided_by, interrupted=active
            )
            if session.is_speaking:
                await session.stop_playback()
                latch.stream_stops += 1
        await cancel_speech(lifecycle.floor, lifecycle.journal, reason="interim")

    def _is_overlap_duplicate(self, event: TranscriptEvent) -> bool:
        if self._last_final is None:
            return False
        participant_id, previous, admitted_at = self._last_final
        if participant_id != event.participant_id:
            return False
        if self._clock() - admitted_at > _OVERLAP_WINDOW_SECONDS:
            return False
        current = " ".join(_tokens(event.text))
        if min(len(previous), len(current)) < 8:
            return False
        return current.endswith(previous) or previous.endswith(current)

    def _mentioned(self, text: str) -> bool:
        names = self._addressing_names or self._wake_phrases
        return any(mentions_whole_phrase(text, name) for name in names)

    @property
    def _rules_admission(self) -> bool:
        return bool(self._addressing_names) or self._participant_policy

    def _vocative(self, text: str) -> bool:
        names = self._wake_phrases or self._addressing_names
        return is_vocative(text, names)

    def _addressed(self, text: str) -> bool:
        if self._participant_policy:
            return is_vocative(text, self._wake_phrases)
        return any(mentions_whole_phrase(text, name) for name in self._addressing_names)

    def _others_speaking(self) -> bool:
        signal = self._others_speaking_signal
        return self._participant_policy and signal is not None and signal()

    def _open_final(self, event: TranscriptEvent) -> TurnRuntime | None:
        lifecycle = self._lifecycle
        if lifecycle.is_closed or lifecycle.is_failed:
            return None
        if not event.finalized or not event.text.strip():
            return None
        lifecycle.note_activity()
        if lifecycle.is_paused:
            lifecycle.journal.record(
                "transcript_suppressed", source="agent", transcript_id=event.id, reason="paused"
            )
            return None
        runtime = lifecycle.runtime
        if runtime is None or event.id in self._seen:
            return None
        if self._is_overlap_duplicate(event):
            lifecycle.journal.record(
                "transcript_suppressed",
                source="agent",
                transcript_id=event.id,
                reason="overlap_duplicate",
            )
            return None
        self._seen.add(event.id)
        lifecycle.note_final(event)
        return runtime

    async def _invoke_final(self, event: TranscriptEvent, runtime: TurnRuntime) -> TurnGraphState:
        self._in_flight = True
        try:
            return await self._lifecycle.turn.run(
                TurnInput(
                    event=TurnEvent(
                        kind="final",
                        event_id=event.id,
                        at=transcript_stamp(event).timestamp(),
                        payload=event.text,
                    ),
                    context=ParticipationContext(
                        addressed=self._addressed(event.text),
                        others_speaking=self._others_speaking(),
                        vocative=self._vocative(event.text),
                    ),
                    speaking_generation=self._lifecycle.floor.generation,
                    transcript_event=event,
                    history=list(self._lifecycle.events),
                    addressing_names=self._addressing_names,
                    admission="rules" if self._rules_admission else "admit",
                ),
                runtime,
            )
        finally:
            self._in_flight = False

    async def _yield_floor(self, text: str, decision: ParticipationDecision) -> None:
        lifecycle = self._lifecycle
        session = lifecycle.session
        if decision.backchannel or not lifecycle.barge_in or session is None:
            return
        if not session.is_speaking and decision.addressee != "twin":
            return
        record_barge_in(
            lifecycle.journal, "final", len(text.split()), interrupted=lifecycle.floor.active
        )
        if session.is_speaking:
            await session.stop_playback()
        await cancel_speech(lifecycle.floor, lifecycle.journal, reason="final")

    async def _prefetch(self, text: str) -> JudgeWindow | None:
        runtime = self._lifecycle.runtime
        if runtime is None:
            return None
        judging = self._lifecycle.judging
        window = (
            judging.windows.for_interim(text, twin_speaking=runtime.speaker.is_speaking)
            if judging is not None
            else None
        )
        events = list(self._lifecycle.events)
        await self._lifecycle.turn.run(
            TurnInput(
                event=TurnEvent(
                    kind="interim",
                    event_id=events[-1].id if events else "",
                    at=self._clock(),
                    payload=text,
                ),
                context=ParticipationContext(addressed=False),
                speaking_generation=self._lifecycle.floor.generation,
                history=events,
                addressing_names=self._addressing_names,
                pending=self._in_flight or self._lifecycle.queue.pending,
                judge_window=window,
            ),
            runtime,
        )
        return window
