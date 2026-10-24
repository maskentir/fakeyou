import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.participation_decider import DeciderTurn, ParticipationDecision
from fakeyou.conversation.turn_events import TurnEvent
from fakeyou.conversation.turn_graph import (
    TurnGraphState,
    TurnInput,
    TurnRuntime,
    cancel_speech,
    record_barge_in,
)
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from fakeyou.foundation.acks import is_bare_ack, is_interjection
from fakeyou.foundation.participation import (
    ParticipationContext,
    is_other_observed_speaker,
    is_vocative,
    mentions_whole_phrase,
    same_observed_speaker,
    same_voice,
)
from fakeyou.foundation.transcript import TranscriptEvent, transcript_stamp
from fakeyou.foundation.triggers import ReplyChannel
from fakeyou.foundation.turn_tracker import TurnState, detect_turn_cue, empty_turn_state, note_event


@dataclass(frozen=True, slots=True)
class IngressTiming:
    activation_seconds: float = DEFAULTS.conversation.activation_seconds
    barge_in_min_words: int = DEFAULTS.conversation.barge_in_min_words
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        seconds = self.activation_seconds
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("activation window must be positive and finite")


@dataclass(frozen=True, slots=True)
class PolicyOptions:
    enabled: bool = False
    others_speaking: Callable[[], bool] | None = None


def addressing_phrases(phrases: Sequence[str]) -> tuple[str, ...]:
    return tuple(phrase.strip().casefold() for phrase in phrases if phrase.strip())


class TurnIngress:
    def __init__(
        self,
        lifecycle: TurnLifecycle,
        *,
        addressing_names: Sequence[str],
        wake_phrases: Sequence[str] = (),
        timing: IngressTiming | None = None,
    ) -> None:
        resolved = IngressTiming() if timing is None else timing
        self._lifecycle = lifecycle
        self._addressing_names = addressing_phrases(addressing_names)
        self._wake_phrases = addressing_phrases(wake_phrases)
        self._participant_policy = False
        self._others_speaking_signal: Callable[[], bool] | None = None
        self._barge_in_min_words = resolved.barge_in_min_words
        self._activation_seconds = resolved.activation_seconds
        self._clock = resolved.clock
        self._seen: set[str] = set()
        self._turns: list[DeciderTurn] = []
        self._turn: TurnState = empty_turn_state()
        self._engaged_speaker: str | None = None
        self._engaged_voice: str | None = None
        self._active_until = 0.0
        self._in_flight = False
        self._awaiting_reply = False
        self._previous_inbound_was_bare_ack = False
        lifecycle.observe_spoken(self._note_spoken)

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
        bare_ack = is_bare_ack(event.text)
        if not self._rules_admission and bare_ack and self._previous_inbound_was_bare_ack:
            return self._reject_bare_run(event)
        now = self._clock()
        output = await self._invoke_final(event, runtime, now)
        self._lifecycle.start_requested()
        decision: ParticipationDecision | None = output["participation"]
        if decision is not None and decision.decision in ("speak", "clarify"):
            self._settle_admitted(event, decision, output, now)
            return decision
        if decision is not None:
            await self._yield_floor(event.text, bare_ack=bare_ack)
        return decision

    def receive_chat(self, event: TranscriptEvent) -> bool:
        if not event.text.strip() or not self._mentioned(event.text):
            return False
        return self._lifecycle.defer(event, channel="chat", needs_quiet_floor=False)

    def receive_attention(self, event: TranscriptEvent, *, channel: ReplyChannel) -> bool:
        return self._lifecycle.defer(event, channel=channel, needs_quiet_floor=channel == "voice")

    def _mentioned(self, text: str) -> bool:
        names = self._addressing_names or self._wake_phrases
        return any(mentions_whole_phrase(text, name) for name in names)

    async def handle_final(self, event: TranscriptEvent) -> ParticipationDecision | None:
        decision = await self.receive_final(event)
        await self._lifecycle.drained()
        return decision

    @property
    def _rules_admission(self) -> bool:
        return bool(self._addressing_names) or self._participant_policy

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
        self._seen.add(event.id)
        self._lifecycle.note_final(event)
        self._turn = note_event(self._turn, event, self._addressing_names)
        self._turns.append(
            DeciderTurn(
                speaker=event.participant_id,
                text=event.text,
                mine=detect_turn_cue(event.text, self._addressing_names)[0] != "none",
            )
        )
        return runtime

    def _context(self, event: TranscriptEvent, now: float) -> ParticipationContext:
        activation_valid = now < self._active_until
        addressed = self._addressed(event.text)
        return ParticipationContext(
            addressed=addressed,
            followup=not addressed and activation_valid,
            same_participant=self._same_speaker(event),
            ambiguous=False,
            others_speaking=self._others_speaking(),
            activation_valid=activation_valid,
            bare_ack=is_bare_ack(event.text),
            interjection=is_interjection(event.text),
            awaiting_reply=self._awaiting_reply,
            turn_cue=self._turn.cue,
            turn_mine=self._turn.my_turn_likely,
        )

    async def _invoke_final(
        self, event: TranscriptEvent, runtime: TurnRuntime, now: float
    ) -> TurnGraphState:
        self._in_flight = True
        try:
            output = cast(
                TurnGraphState,
                await self._lifecycle.graph.ainvoke(
                    TurnInput(
                        event=TurnEvent(
                            kind="final",
                            event_id=event.id,
                            at=transcript_stamp(event).timestamp(),
                            payload=event.text,
                        ),
                        turns=list(self._turns),
                        context=self._context(event, now),
                        speaking_generation=self._lifecycle.floor.generation,
                        transcript_event=event,
                        history=list(self._lifecycle.events),
                        addressing_names=self._addressing_names,
                        admission="rules" if self._rules_admission else "admit",
                    ),
                    context=runtime,
                ),
            )
        finally:
            self._in_flight = False
        return output

    def _settle_admitted(
        self,
        event: TranscriptEvent,
        decision: ParticipationDecision,
        output: TurnGraphState,
        now: float,
    ) -> None:
        if not self._participant_policy:
            self._previous_inbound_was_bare_ack = is_bare_ack(event.text)
        if self._rules_admission:
            self._admit(event, decision, output.get("decision_source"), now)

    def _reject_bare_run(self, event: TranscriptEvent) -> ParticipationDecision:
        self._lifecycle.journal.record(
            "ack_decided",
            source="agent",
            transcript_id=event.id,
            action="silent",
            reason="bare_ack_run",
        )
        self._previous_inbound_was_bare_ack = True
        return ParticipationDecision(decision="silent", confidence=1.0, reason_code="bare_ack_run")

    def _note_spoken(self, sentence: str) -> None:
        self._awaiting_reply = sentence.rstrip().endswith("?")

    def _admit(
        self,
        event: TranscriptEvent,
        decision: ParticipationDecision,
        source: str | None,
        now: float,
    ) -> None:
        self._active_until = now + self._activation_seconds
        if source == "rule" and decision.reason_code == "addressed":
            self._engaged_voice = (
                event.voice_clusters[0] if len(event.voice_clusters) == 1 else None
            )
        if event.participant_id != "unknown":
            self._engaged_speaker = event.participant_id

    def _same_speaker(self, event: TranscriptEvent) -> bool:
        voice = same_voice(self._engaged_voice, event.voice_clusters)
        if voice is not None:
            return voice
        if self._participant_policy:
            return same_observed_speaker(self._engaged_speaker, event.participant_id)
        return not is_other_observed_speaker(self._engaged_speaker, event.participant_id)

    async def _yield_floor(self, text: str, *, bare_ack: bool) -> None:
        lifecycle = self._lifecycle
        session = lifecycle.session
        if bare_ack or self._wake_phrases or not lifecycle.barge_in or session is None:
            return
        if not session.is_speaking:
            return
        record_barge_in(lifecycle.journal, "final", len(text.split()))
        await session.stop_playback()

    async def handle_interim(self, text: str) -> None:
        lifecycle = self._lifecycle
        if text.strip():
            lifecycle.note_activity()
        if lifecycle.runtime is not None and not lifecycle.is_closed and not lifecycle.is_failed:
            await self._kick_jev(text)
        session = lifecycle.session
        if (
            lifecycle.is_closed
            or lifecycle.is_failed
            or not lifecycle.barge_in
            or session is None
            or (self._wake_phrases and self._clock() >= self._active_until)
        ):
            return
        words = len(text.split())
        if words < self._barge_in_min_words:
            return
        active = lifecycle.floor.active
        if not session.is_speaking and (active is None or active.task.done()):
            return
        latch = lifecycle.barge
        if text == latch.last_interim:
            return
        latch.last_interim = text
        if not latch.interrupted:
            latch.interrupted = True
            record_barge_in(lifecycle.journal, "interim", words)
            if session.is_speaking:
                await session.stop_playback()
                latch.stream_stops += 1
        await cancel_speech(lifecycle.floor, lifecycle.journal, reason="interim")

    async def _kick_jev(self, text: str) -> None:
        runtime = self._lifecycle.runtime
        if runtime is None:
            return
        events = list(self._lifecycle.events)
        await self._lifecycle.graph.ainvoke(
            TurnInput(
                event=TurnEvent(
                    kind="interim",
                    event_id=events[-1].id if events else "",
                    at=self._clock(),
                    payload=text,
                ),
                turns=list(self._turns),
                context=ParticipationContext(
                    addressed=False,
                    followup=False,
                    same_participant=False,
                    ambiguous=False,
                    others_speaking=False,
                    activation_valid=False,
                    turn_cue=self._turn.cue,
                    turn_mine=self._turn.my_turn_likely,
                ),
                speaking_generation=self._lifecycle.floor.generation,
                history=events,
                addressing_names=self._addressing_names,
                pending=self._in_flight or self._lifecycle.queue.pending,
            ),
            context=runtime,
        )
