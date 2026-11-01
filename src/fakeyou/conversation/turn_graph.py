import asyncio
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Hashable,
    Mapping,
)
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.conversation import cap_spoken_reply
from fakeyou.conversation.floor import ActiveSpeech, SpeechFloor, interrupt_speech
from fakeyou.conversation.participation_decision import ParticipationDecision
from fakeyou.conversation.participation_graph import (
    DecisionSource,
    JudgeThresholds,
    ParticipationRuntime,
    ParticipationState,
    TurnJudging,
    build_participation_subgraph,
)
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.sentence_stream import shape_sentences
from fakeyou.conversation.thinking_cue import (
    CueSpeaker,
    CueState,
    CueTurn,
    ThinkingCue,
    cue_when_slow,
)
from fakeyou.conversation.turn_events import JournalSink, TurnEvent
from fakeyou.conversation.turn_judge import JudgeWindow
from fakeyou.conversation.turn_queue import AdmittedFinals, DeferredTurn, PendingFinals
from fakeyou.conversation.turn_recovery import (
    TurnRecovery,
    TurnScope,
    record_agent_failed,
    record_agent_finished,
    record_agent_started,
    record_drop,
    record_sentence_dropped,
    turn_request_id,
    within_sentence_budget,
)
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.protocols import SupportsAclose
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.cleanup import close_safely
from fakeyou.observability.faults import FaultReporter, capture_error
from fakeyou.observability.provider_faults import classify_exception
from fakeyou.perception.scrub import scrub_internal_refs
from fakeyou.session.commands import CommandOutcome, CommandSpec
from fakeyou.session.facts import StateQuestion
from fakeyou.session.intent import EvidenceNeed, Reading, intent_of
from fakeyou.session.planner import TurnPlan, plan_turn, with_language
from fakeyou.session.policy import UNATTRIBUTED, Channel, Grant, authorize
from fakeyou.speech.contract import PreparedSpeech


class Speaker(Protocol):
    @property
    def is_speaking(self) -> bool: ...

    async def speak(
        self,
        utterance: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None: ...

    async def prepare(
        self,
        utterance: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech | None: ...

    async def speak_prepared(self, prepared: PreparedSpeech) -> None: ...

    async def stop_playback(self) -> None: ...


BargeTrigger = Literal["final", "interim"]
ConversationPhase = Literal[
    "listening", "weighing", "queued", "interrupted", "waiting", "acting", "failed", "closed"
]


def record_barge_in(
    journal: JournalSink,
    trigger: BargeTrigger,
    words: int,
    *,
    decided_by: str | None = None,
    interrupted: ActiveSpeech | None = None,
) -> None:
    fields: dict[str, Any] = {"trigger": trigger, "words": words}
    if decided_by is not None:
        fields["decided_by"] = decided_by
    if interrupted is not None and interrupted.transcript_id is not None:
        fields["interrupted_transcript_id"] = interrupted.transcript_id
    journal.record("barge_in_detected", source="agent", **fields)


def _cancel_request_id(active: ActiveSpeech) -> str | None:
    if active.request_id is not None:
        return active.request_id
    if active.transcript_id is None:
        return None
    return turn_request_id(active.transcript_id, active.generation)


def _cancel_fields(floor: SpeechFloor, active: ActiveSpeech, reason: str) -> dict[str, Any]:
    transcript_id = active.transcript_id
    return {
        "request_id": _cancel_request_id(active),
        "transcript_id": transcript_id,
        "generation": active.generation,
        "reason": reason,
        "duration_ms": (floor.now() - active.started) * 1000,
    }


async def cancel_speech(floor: SpeechFloor, journal: JournalSink, *, reason: str) -> None:
    active = floor.active
    if active is None or active.task.done():
        return
    journal.record(
        "agent_cancel_requested", source="agent", **_cancel_fields(floor, active, reason)
    )
    await floor.interrupt(reason)
    journal.record("agent_cancelled", source="agent", **_cancel_fields(floor, active, reason))


class PhaseSink(Protocol):
    def move(self, phase: ConversationPhase, detail: str) -> None: ...


class Responder(Protocol):
    def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]: ...


class WholeResponder(Protocol):
    async def respond(self, transcript: str) -> SpokenChunk: ...


ToolStartHook = Callable[[], None]


WholeTurnFactory = Callable[[tuple[TranscriptEvent, ...], ToolStartHook], WholeResponder]
StreamTurnFactory = Callable[[tuple[TranscriptEvent, ...], ToolStartHook, str], Responder]


@dataclass(frozen=True, slots=True)
class PlannedTurn:
    plan: TurnPlan
    actor: str = "unattributed"


class TurnPlanning(Protocol):
    def __call__(
        self, event: TranscriptEvent, reading: Reading, channel: Channel
    ) -> PlannedTurn: ...


StateSpeech = Callable[[StateQuestion, str | None], str | None]
EvidencePort = Callable[[str, tuple[str, ...]], Awaitable[Mapping[str, object]]]


class CommandPort(Protocol):
    async def run(
        self, spec: CommandSpec, *, plan_id: str, ordinal: int, actor: str
    ) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class SentenceLimits:
    max_sentences: int
    language: str


@dataclass(slots=True)
class BargeLatch:
    interrupted: bool = False
    last_interim: str | None = None
    stream_stops: int = 0

    def rearm(self) -> None:
        self.interrupted = False
        self.last_interim = None


class TurnScheduler(Protocol):
    @property
    def is_busy(self) -> bool: ...

    def request_pending(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TurnRuntime:
    speaker: Speaker
    journal: JournalSink
    responder: Responder | None = None
    floor: SpeechFloor | None = None
    judging: TurnJudging | None = None
    phase: PhaseSink | None = None
    queue: PendingFinals | None = None
    recovery: TurnRecovery | None = None
    on_fail: Callable[[Exception], None] | None = None
    response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds
    sentences: SentenceLimits | None = None
    whole: WholeResponder | None = None
    whole_turn: WholeTurnFactory | None = None
    stream_turn: StreamTurnFactory | None = None
    admitted: AdmittedFinals | None = None
    closed: Callable[[], bool] | None = None
    paused: Callable[[], bool] | None = None
    barge_in: bool = True
    raise_if_failed: Callable[[], None] | None = None
    scheduler: TurnScheduler | None = None
    bound: Callable[[], bool] | None = None
    barge: BargeLatch | None = None
    note_spoken: Callable[[str], None] | None = None
    shadow: bool = False
    faults: FaultReporter | None = None
    chat_turn: WholeTurnFactory | None = None
    planning: TurnPlanning | None = None
    commands: CommandPort | None = None
    state_speech: StateSpeech | None = None
    evidence: EvidencePort | None = None
    plans: dict[str, PlannedTurn] = field(default_factory=dict)
    cue: ThinkingCue | None = None
    others_speaking: Callable[[], bool] | None = None


def _closed(runtime: TurnRuntime) -> bool:
    return runtime.closed is not None and runtime.closed()


def _paused(runtime: TurnRuntime) -> bool:
    return runtime.paused is not None and runtime.paused()


def _bound(runtime: TurnRuntime) -> bool:
    return runtime.bound is None or runtime.bound()


def _raise_if_failed(runtime: TurnRuntime) -> None:
    if runtime.raise_if_failed is not None:
        runtime.raise_if_failed()


def _busy(runtime: TurnRuntime) -> bool:
    if runtime.scheduler is not None:
        return runtime.scheduler.is_busy
    return runtime.floor is not None and runtime.floor.busy


def _admitted_count(runtime: TurnRuntime) -> int:
    if runtime.admitted is not None:
        return runtime.admitted.count
    return 0 if runtime.floor is None else runtime.floor.generation


def _advance_admitted(runtime: TurnRuntime) -> int:
    if runtime.admitted is not None:
        return runtime.admitted.advance()
    return _admitted_count(runtime)


def _floor_generation(runtime: TurnRuntime) -> int:
    return 0 if runtime.floor is None else runtime.floor.generation


def _fail(runtime: TurnRuntime, cause: Exception) -> None:
    if runtime.on_fail is not None:
        runtime.on_fail(cause)


def _move_phase(runtime: TurnRuntime, phase: ConversationPhase, detail: str) -> None:
    if runtime.phase is not None:
        runtime.phase.move(phase, detail)


def _turn_current(runtime: TurnRuntime, generation: int, floor_generation: int) -> bool:
    if _closed(runtime):
        return False
    if runtime.admitted is not None and runtime.admitted.count != generation:
        return False
    return runtime.floor is None or runtime.floor.admits(floor_generation)


def _admission_current(runtime: TurnRuntime, generation: int) -> bool:
    if _closed(runtime):
        return False
    return runtime.admitted is None or runtime.admitted.count == generation


def _drop_outcome(floor: SpeechFloor) -> tuple[str, str]:
    if floor.last_reason == "close":
        return "closed", "close"
    if floor.last_reason == "interim":
        return "stopped", "interim"
    return "stale_generation", "new_final"


def turn_drop_outcome(runtime: TurnRuntime, generation: int) -> tuple[str, str]:
    if _closed(runtime):
        return "closed", "close"
    if runtime.admitted is not None:
        if runtime.admitted.count != generation:
            return "stale_generation", "new_final"
        return "stopped", "interim"
    if runtime.floor is not None:
        return _drop_outcome(runtime.floor)
    return "stale_generation", "new_final"


def tool_start_hook(
    runtime: TurnRuntime, generation: int, floor_generation: int, detail: str
) -> ToolStartHook:
    def on_tool_start() -> None:
        if _turn_current(runtime, generation, floor_generation):
            _move_phase(runtime, "acting", detail)
            _raise_if_failed(runtime)

    return on_tool_start


async def take_floor_for_final(runtime: TurnRuntime, transcript_text: str) -> None:
    floor = runtime.floor
    interrupted = floor.active if floor is not None else None
    if floor is not None:
        await cancel_speech(floor, runtime.journal, reason="new_final")
    if not _bound(runtime):
        return
    speaker = runtime.speaker
    if runtime.barge_in and speaker.is_speaking:
        record_barge_in(
            runtime.journal, "final", len(transcript_text.split()), interrupted=interrupted
        )
    if (runtime.barge_in and speaker.is_speaking) or (_busy(runtime) and not speaker.is_speaking):
        await speaker.stop_playback()


Admission = Literal["rules", "admit"]


class TurnInput(TypedDict):
    event: TurnEvent
    context: ParticipationContext
    speaking_generation: int
    transcript_event: NotRequired[TranscriptEvent | None]
    history: NotRequired[list[TranscriptEvent]]
    addressing_names: NotRequired[tuple[str, ...]]
    pending: NotRequired[bool]
    admission: NotRequired[Admission]
    judge_window: NotRequired[JudgeWindow | None]


class TurnGraphState(TypedDict):
    event: TurnEvent
    context: ParticipationContext
    speaking_generation: int
    transcript_text: str
    participation: ParticipationDecision | None
    failure: str | None
    transcript_event: NotRequired[TranscriptEvent | None]
    history: NotRequired[list[TranscriptEvent]]
    addressing_names: NotRequired[tuple[str, ...]]
    pending: NotRequired[bool]
    decision_source: NotRequired[DecisionSource | None]
    admission: NotRequired[Admission]
    delivered: NotRequired[tuple[str, ...]]
    judge_window: NotRequired[JudgeWindow | None]


TurnRoute = Literal["speak", "end", "failed"]
IngestRoute = Literal["speak", "participation", "refresh"]
ParticipationGraph = CompiledStateGraph[
    ParticipationState, ParticipationRuntime, ParticipationState, ParticipationState
]
_ADMITTED: frozenset[str] = frozenset({"speak", "clarify"})


def route_after_ingest(state: TurnGraphState) -> IngestRoute:
    kind = state["event"].kind
    if kind == "stop":
        return "speak"
    if kind == "interim":
        return "refresh"
    if state.get("admission") == "admit":
        return "speak"
    return "participation"


def route_after_participation(state: TurnGraphState) -> TurnRoute:
    if state["failure"] is not None:
        return "failed"
    decision = state["participation"]
    if decision is not None and decision.decision in _ADMITTED:
        return "speak"
    return "end"


def _prefetch_judgement(state: TurnGraphState, runtime: TurnRuntime) -> None:
    judging = runtime.judging
    text = state["transcript_text"]
    if judging is None or state.get("pending", False) or not text.strip():
        return
    window = state.get("judge_window")
    if window is None:
        window = judging.windows.for_interim(text, twin_speaking=runtime.speaker.is_speaking)
    judging.cache.prefetch(window)


_NO_THRESHOLDS = JudgeThresholds(reply=1.0, clarify=1.0)


async def _participate(
    state: TurnGraphState,
    runtime: TurnRuntime,
    participation_graph: ParticipationGraph,
) -> dict[str, ParticipationDecision | DecisionSource | str | None]:
    if state.get("admission") == "admit":
        return {"failure": None}
    event = state.get("transcript_event")
    if event is None:
        raise ValueError("a final turn carries its transcript event")
    judging = runtime.judging
    window = (
        judging.windows.for_final(event, twin_speaking=runtime.speaker.is_speaking)
        if judging is not None
        else None
    )
    result = await participation_graph.ainvoke(
        ParticipationState(
            window=window,
            context=state["context"],
            transcript_id=state["event"].event_id,
            last_participation=None,
            decision_source=None,
        ),
        context=ParticipationRuntime(
            journal=runtime.journal,
            thresholds=judging.thresholds if judging is not None else _NO_THRESHOLDS,
            cache=judging.cache if judging is not None else None,
        ),
    )
    return {
        "participation": result["last_participation"],
        "decision_source": result["decision_source"],
        "failure": None,
    }


LOOKAHEAD_DEPTH = 2


class SentenceSink(Protocol):
    async def prepare(self, sentence: str, language: str) -> PreparedSpeech | None: ...

    async def say(self, prepared: PreparedSpeech) -> None: ...


@dataclass(slots=True)
class ReplyStreams:
    reply: AsyncIterator[SpokenChunk] | None = None
    source: AsyncIterator[SpokenChunk] | None = None


@dataclass(frozen=True, slots=True)
class SpeechRuntime:
    responder: Responder | None
    sink: SentenceSink
    journal: JournalSink
    floor: SpeechFloor | None = None
    sentences: SentenceLimits | None = None
    transcript_id: str | None = None
    scope: TurnScope | None = None
    response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds
    whole: WholeResponder | None = None
    paused: Callable[[], bool] | None = None
    held: bool = False
    current: Callable[[], bool] | None = None
    raise_if_failed: Callable[[], None] | None = None
    bound: Callable[[], bool] | None = None
    faults: FaultReporter | None = None
    drop_detail: Callable[[], str] | None = None
    streams: ReplyStreams = field(default_factory=ReplyStreams)
    cue_state: CueState | None = None


class SpeechInput(TypedDict):
    transcript: str
    speaking_generation: int
    transcript_event: NotRequired[TranscriptEvent | None]


class SpeechState(TypedDict):
    transcript: str
    speaking_generation: int
    reply: bool
    failure: str | None
    transcript_event: NotRequired[TranscriptEvent | None]
    delivered: NotRequired[tuple[str, ...]]
    completed: NotRequired[bool]
    error: NotRequired[Exception | None]
    settled: NotRequired[bool]
    silent: NotRequired[bool]
    agent_journaled: NotRequired[bool]


type SpeechGraph = CompiledStateGraph[SpeechState, SpeechRuntime, SpeechInput, SpeechState]


class TurnDropped(Exception):
    pass


class TurnSink:
    def __init__(
        self,
        runtime: TurnRuntime,
        scope: TurnScope | None,
        transcript_id: str,
        generations: tuple[int, int],
        *,
        streamed: bool,
    ) -> None:
        self._runtime = runtime
        self._scope = scope
        self._transcript_id = transcript_id
        self._generation, self._floor_generation = generations
        self._streamed = streamed
        self._stop_mark = 0 if runtime.barge is None else runtime.barge.stream_stops
        self._spoken = 0
        self._offered = 0
        self._started = False
        self._queued = False
        self._indices: dict[PreparedSpeech, int] = {}

    @property
    def started(self) -> bool:
        return self._started

    async def prepare(self, sentence: str, language: str) -> PreparedSpeech | None:
        sentence_index = self._offered
        self._offered += 1
        scrubbed = scrub_internal_refs(sentence)
        if not scrubbed:
            return None
        prepared = await self._runtime.speaker.prepare(
            scrubbed,
            sentence_index=sentence_index,
            transcript_id=self._transcript_id,
            language=language,
        )
        if prepared is None:
            return None
        self._indices[prepared] = sentence_index
        if not self._queued and self._drop_outcome() is None:
            _move_phase(self._runtime, "queued", self._transcript_id)
            self._queued = True
        return prepared

    async def say(self, prepared: PreparedSpeech) -> None:
        runtime = self._runtime
        sentence = prepared.text
        index = self._indices.get(prepared, prepared.sentence_index)
        self._started = True
        if self._streamed and index is not None:
            self._check_live(index)
        _raise_if_failed(runtime)
        if runtime.barge is not None:
            runtime.barge.rearm()
        if runtime.note_spoken is not None:
            runtime.note_spoken(sentence)
        await runtime.speaker.speak_prepared(prepared)
        self._spoken += 1

    def _check_live(self, index: int) -> None:
        outcome = self._drop_outcome()
        if outcome is None:
            return
        detail, reason = outcome
        record_drop(
            self._runtime.journal, self._scope, sentence_index=index, detail=detail, reason=reason
        )
        raise TurnDropped(detail)

    def _drop_outcome(self) -> tuple[str, str] | None:
        runtime = self._runtime
        if _paused(runtime):
            return "paused", "paused"
        stopped = runtime.barge is not None and runtime.barge.stream_stops != self._stop_mark
        if stopped or not _turn_current(runtime, self._generation, self._floor_generation):
            return turn_drop_outcome(runtime, self._generation)
        if not _bound(runtime):
            return "closed", "close"
        return None


def _turn_scope(runtime: TurnRuntime, transcript_id: str, generation: int) -> TurnScope | None:
    floor = runtime.floor
    if floor is None:
        return None
    return TurnScope(
        request_id=turn_request_id(transcript_id, generation),
        transcript_id=transcript_id,
        generation=generation,
        started=floor.now(),
        clock=floor.now,
        response_timeout_seconds=runtime.response_timeout_seconds,
    )


def _turn_whole(
    runtime: TurnRuntime, batch: tuple[TranscriptEvent, ...], hook: ToolStartHook
) -> WholeResponder | None:
    factory = runtime.whole_turn
    if factory is None:
        return runtime.whole
    return factory(batch, hook)


def _turn_responder(
    runtime: TurnRuntime, batch: tuple[TranscriptEvent, ...], hook: ToolStartHook, request_id: str
) -> Responder | None:
    factory = runtime.stream_turn
    if factory is None:
        return runtime.responder
    return factory(batch, hook, request_id)


def _speech_context(
    runtime: TurnRuntime,
    sink: SentenceSink,
    scope: TurnScope | None,
    generations: tuple[int, int],
    transcript_id: str,
) -> "SpeechRuntime":
    generation, floor_generation = generations
    return SpeechRuntime(
        responder=runtime.responder,
        sink=sink,
        journal=runtime.journal,
        floor=runtime.floor,
        transcript_id=transcript_id,
        scope=scope,
        response_timeout_seconds=runtime.response_timeout_seconds,
        paused=runtime.paused,
        held=True,
        current=partial(_turn_current, runtime, generation, floor_generation),
        raise_if_failed=runtime.raise_if_failed,
        bound=runtime.bound,
        faults=runtime.faults,
        drop_detail=partial(_drop_detail, runtime, generation),
    )


def _drop_detail(runtime: TurnRuntime, generation: int) -> str:
    return turn_drop_outcome(runtime, generation)[0]


def _raise_reply_failure(output: Mapping[str, object]) -> None:
    error = output.get("error")
    if output.get("failure") == "reply_failed" and isinstance(error, Exception):
        raise error


def _finish_turn(
    runtime: TurnRuntime,
    output: Mapping[str, object],
    transcript_id: str,
    generations: tuple[int, int],
    *,
    streamed: bool,
) -> None:
    if streamed:
        _raise_if_failed(runtime)
    generation, _floor_generation = generations
    if _closed(runtime) or (runtime.admitted is not None and runtime.admitted.count != generation):
        return
    if output.get("completed"):
        _move_phase(runtime, "listening" if output.get("delivered") else "waiting", transcript_id)
    elif output.get("silent"):
        _move_phase(runtime, "waiting", transcript_id)


_NO_GRANT = Grant(frozenset())
MAX_PENDING_PLANS = 32
UNJUDGED_EVIDENCE: tuple[EvidenceNeed, ...] = ("chat_history", "memory")


def reading_of(verdict: ParticipationDecision) -> Reading:
    return Reading(
        decision=verdict.decision,
        reason=verdict.reason_code,
        meet_action=verdict.meet_action,
        reaction_emoji=verdict.reaction_emoji,
        action_only=verdict.action_only,
        requests_chat=verdict.requests_chat,
        evidence=verdict.evidence_needs,
        state_question=verdict.state_question,
        language=verdict.language,
    )


def plan_for(
    runtime: TurnRuntime, event: TranscriptEvent, reading: Reading, channel: Channel
) -> PlannedTurn:
    if runtime.planning is not None:
        planned = runtime.planning(event, reading, channel)
        return replace(planned, plan=with_language(planned.plan, reading.language))
    authorization = authorize(
        intent_of(reading), actor=UNATTRIBUTED, channel=channel, grant=_NO_GRANT
    )
    plan = plan_turn(authorization, channel=channel, plan_id=f"plan-{event.id}")
    return PlannedTurn(with_language(plan, reading.language))


_OUTCOME_NOTES: dict[str, str] = {
    "effect_observed": "{kind} is done; say so plainly, the way a participant would.",
    "effect_unobserved": (
        "{kind} was done but you could not see it take effect; do not say anyone saw it."
    ),
    "unknown": "It is not known whether {kind} happened; say you are not sure it went through.",
}
_REFUSAL_NOTES: dict[str, str] = {
    "unattributed_actor": (
        "You did not {what} because the request could not be tied to a participant in this "
        "call; say so briefly."
    ),
    "not_permitted": "You are not permitted to {what} in this meeting; decline briefly.",
    "chat_not_permitted": "You may not write in the meeting chat; answer by voice instead.",
    "browser_unavailable": "You cannot {what} right now because the meeting controls are "
    "unavailable; say so briefly.",
}


def plan_notes(plan: TurnPlan, outcome: CommandOutcome | None) -> tuple[str, ...]:
    notes = [
        _REFUSAL_NOTES[refusal.reason].format(what=refusal.what.replace("_", " "))
        for refusal in plan.refusals
    ]
    if outcome is not None:
        kind = outcome.kind.replace("_", " ")
        template = _OUTCOME_NOTES.get(outcome.state, "{kind} did not happen.")
        notes.append(template.format(kind=kind[0].upper() + kind[1:]))
    if plan.output == "chat":
        notes.append(
            "Your reply is posted in the meeting chat exactly as you write it: write only the "
            "message, and if the participant dictated the words, write exactly those words."
        )
    if plan.leave:
        notes.append("You will leave the call right after this reply.")
    if plan.clarify and not plan.refusals:
        notes.append("Ask one short question about which of the requests to do first.")
    return tuple(notes)


def for_reply(
    event: TranscriptEvent, plan: TurnPlan, outcome: CommandOutcome | None
) -> TranscriptEvent:
    notes = plan_notes(plan, outcome)
    text = f"{event.text}\n[System: {' '.join(notes)}]" if notes else event.text
    return replace(event, text=text, evidence_needs=plan.evidence)


async def with_evidence(
    runtime: TurnRuntime, question: str, event: TranscriptEvent
) -> TranscriptEvent:
    if runtime.evidence is None or not event.evidence_needs:
        return event
    return replace(event, evidence=await runtime.evidence(question, event.evidence_needs))


async def _run_action(runtime: TurnRuntime, planned: PlannedTurn) -> CommandOutcome | None:
    action, commands = planned.plan.action, runtime.commands
    if action is None or commands is None:
        return None
    return await commands.run(
        CommandSpec(action.kind, action.emoji or ""),
        plan_id=planned.plan.plan_id,
        ordinal=0,
        actor=planned.actor,
    )


async def _leave(runtime: TurnRuntime, planned: PlannedTurn) -> CommandOutcome | None:
    if not planned.plan.leave or runtime.commands is None:
        return None
    return await runtime.commands.run(
        CommandSpec("leave"), plan_id=planned.plan.plan_id, ordinal=1, actor=planned.actor
    )


def _record_plan(runtime: TurnRuntime, event_id: str, planned: PlannedTurn) -> None:
    plan = planned.plan
    runtime.journal.record(
        "plan_selected",
        source="agent",
        transcript_id=event_id,
        plan_id=plan.plan_id,
        template=plan.template,
        output=plan.output,
        evidence=list(plan.evidence),
        action=plan.action.kind if plan.action is not None else None,
        leave=plan.leave,
        refusals=[f"{refusal.what}:{refusal.reason}" for refusal in plan.refusals],
        actor=planned.actor,
    )


async def act_first(
    runtime: TurnRuntime, final: TranscriptEvent, planned: PlannedTurn
) -> tuple[bool, CommandOutcome | None]:
    plan = planned.plan
    if plan.action is not None and runtime.commands is not None:
        _move_phase(runtime, "acting", final.id)
    outcome = await _run_action(runtime, planned)
    if plan.template == "leave_only":
        await _leave(runtime, planned)
        return True, outcome
    if plan.action_only and (outcome is None or not outcome.not_done) and not plan.refusals:
        return True, outcome
    return plan.template == "silent", outcome


async def answer_state(
    runtime: TurnRuntime, final: TranscriptEvent, planned: PlannedTurn, generation: int
) -> bool:
    question = planned.plan.state_question
    if question is None or runtime.state_speech is None:
        return False
    text = runtime.state_speech(question, planned.plan.language)
    if text is None:
        return False
    runtime.journal.record(
        "state_answer_selected",
        source="agent",
        transcript_id=final.id,
        question=question,
        output=planned.plan.output,
    )
    if planned.plan.output == "chat":
        if runtime.commands is not None:
            _move_phase(runtime, "acting", final.id)
            await runtime.commands.run(
                CommandSpec("send_chat", text),
                plan_id=planned.plan.plan_id,
                ordinal=2,
                actor=planned.actor,
            )
        _move_phase(runtime, "listening", final.id)
        return True
    if not _admission_current(runtime, generation):
        return True
    _move_phase(runtime, "queued", final.id)
    language = runtime.sentences.language if runtime.sentences is not None else None
    await runtime.speaker.speak(text, transcript_id=final.id, language=language)
    if runtime.note_spoken is not None:
        runtime.note_spoken(text)
    _move_phase(runtime, "listening", final.id)
    return True


async def _run_turn(
    runtime: TurnRuntime,
    speech: SpeechGraph,
    batch: tuple[TranscriptEvent, ...],
    scope: TurnScope | None,
    generation: int,
) -> None:
    final = batch[-1]
    judged = runtime.plans.pop(final.id, None)
    planned = judged or plan_for(runtime, final, Reading("speak", "unjudged"), "speech")
    done, outcome = await act_first(runtime, final, planned)
    if done:
        if _admission_current(runtime, generation):
            _move_phase(runtime, "waiting", final.id)
        return
    if await answer_state(runtime, final, planned, generation):
        return
    if planned.plan.output == "chat":
        await _deliver_chat(runtime, final, planned, generation, outcome)
        return
    reply_event = for_reply(final, planned.plan, outcome)
    if judged is None:
        reply_event = replace(reply_event, evidence_needs=UNJUDGED_EVIDENCE)
    farewell = Farewell()
    try:
        await _speak_reply(runtime, speech, batch, reply_event, (scope, generation, farewell))
    finally:
        if farewell.started:
            await _leave(runtime, planned)


@dataclass(slots=True)
class Farewell:
    started: bool = False


async def _speak_reply(
    runtime: TurnRuntime,
    speech: SpeechGraph,
    batch: tuple[TranscriptEvent, ...],
    reply_event: TranscriptEvent,
    turn: tuple[TurnScope | None, int, Farewell],
) -> None:
    final = batch[-1]
    scope, generation, farewell = turn
    floor_generation = _floor_generation(runtime)
    generations = (generation, floor_generation)
    hook = tool_start_hook(runtime, generation, floor_generation, final.id)
    request_id = turn_request_id(final.id, generation)
    streamed = runtime.whole_turn is None and runtime.whole is None
    sink = TurnSink(runtime, scope, final.id, generations, streamed=streamed)
    cue_state = CueState()
    cue = _start_cue(
        runtime,
        CueTurn(
            final.id,
            partial(_cue_blocker, runtime, sink, generations),
            state=cue_state,
            source=final.source,
        ),
    )
    try:
        reply_batch = (*batch[:-1], await with_evidence(runtime, final.text, reply_event))
        context = replace(
            _speech_context(runtime, sink, scope, generations, final.id),
            responder=_turn_responder(runtime, reply_batch, hook, request_id) if streamed else None,
            whole=_turn_whole(runtime, reply_batch, hook),
            sentences=runtime.sentences,
            cue_state=cue_state,
        )
        output = await speech.ainvoke(
            SpeechInput(
                transcript=final.text,
                speaking_generation=floor_generation,
                transcript_event=final,
            ),
            context=context,
        )
    finally:
        farewell.started = sink.started
        await _settle_cue(cue)
    if cue_state.played and not output.get("completed"):
        runtime.journal.record(
            "thinking_cue_orphaned",
            source="agent",
            transcript_id=final.id,
            detail="no_answer",
        )
    _raise_reply_failure(output)
    _finish_turn(runtime, output, final.id, generations, streamed=streamed)


def _start_cue(runtime: TurnRuntime, turn: CueTurn) -> asyncio.Task[None] | None:
    cue, speaker = runtime.cue, runtime.speaker
    if cue is None or runtime.shadow or not isinstance(speaker, CueSpeaker):
        return None
    return asyncio.create_task(cue_when_slow(cue, speaker, runtime.journal, turn))


async def _settle_cue(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _cue_blocker(runtime: TurnRuntime, sink: TurnSink, generations: tuple[int, int]) -> str | None:
    generation, floor_generation = generations
    if sink.started:
        return "answered"
    if _paused(runtime) or not _bound(runtime):
        return "paused"
    if not _turn_current(runtime, generation, floor_generation):
        return "stale"
    others = runtime.others_speaking
    if others is not None and others():
        return "floor_busy"
    return None


async def _held(
    runtime: TurnRuntime, work: Coroutine[Any, Any, None], scope: TurnScope | None
) -> bool:
    floor = runtime.floor
    if floor is None or scope is None:
        await work
        return True
    return await floor.hold(
        work,
        transcript_id=scope.transcript_id,
        request_id=scope.request_id,
        generation=scope.generation,
    )


def _turn_dropped(runtime: TurnRuntime) -> None:
    if _closed(runtime):
        raise asyncio.CancelledError
    _raise_if_failed(runtime)


async def recover_turn(
    runtime: TurnRuntime, error: Exception, generation: int, transcript_id: str
) -> None:
    recovery = runtime.recovery
    if recovery is None:
        return
    if recovery.record_failure(error, transcript_id):
        _fail(runtime, error)
        return
    _move_phase(runtime, "waiting", transcript_id)
    if not _bound(runtime) or _admitted_count(runtime) != generation:
        return
    if not recovery.should_apologize:
        return
    speaker = runtime.speaker
    _, speech_error = await capture_error(
        lambda: speaker.speak(recovery.failed_line, transcript_id=transcript_id)
    )
    if speech_error is not None:
        _fail(runtime, speech_error)


async def _settle_outcome(
    runtime: TurnRuntime,
    outcome: tuple[bool | None, Exception | None],
    generation: int,
    transcript_id: str,
) -> None:
    completed, error = outcome
    if error is not None:
        await recover_turn(runtime, error, generation, transcript_id)
    elif completed:
        if runtime.recovery is not None:
            runtime.recovery.record_success()
    else:
        _turn_dropped(runtime)


async def _drain_pending(runtime: TurnRuntime, speech: SpeechGraph, queue: PendingFinals) -> None:
    while queue.pending and not _closed(runtime):
        batch = queue.take()
        generation = _admitted_count(runtime)
        transcript_id = batch[-1].id
        _move_phase(runtime, "weighing", transcript_id)
        _raise_if_failed(runtime)
        scope = _turn_scope(runtime, transcript_id, generation)
        if scope is not None:
            record_agent_started(runtime.journal, scope)
        if runtime.barge is not None:
            runtime.barge.interrupted = False
        work = _run_turn(runtime, speech, batch, scope, generation)
        outcome = await capture_error(partial(_held, runtime, work, scope))
        await _settle_outcome(runtime, outcome, generation, transcript_id)


async def drain_turns(runtime: TurnRuntime, speech: SpeechGraph, queue: PendingFinals) -> None:
    _, error = await capture_error(partial(_drain_pending, runtime, speech, queue))
    if error is not None:
        _fail(runtime, error)


async def _chat_reading(runtime: TurnRuntime, event: TranscriptEvent) -> Reading:
    judging = runtime.judging
    if judging is None:
        return Reading("speak", "chat_named")
    window = judging.windows.for_final(event, twin_speaking=runtime.speaker.is_speaking)
    judgement, _error = await capture_error(partial(judging.cache.judgement, window))
    if judgement is None:
        return Reading("speak", "chat_named")
    return Reading(
        decision="speak",
        reason="chat_named",
        meet_action=judgement.meet_action,
        reaction_emoji=judgement.reaction_emoji,
        action_only=judgement.action_only,
        evidence=judgement.evidence,
        state_question=judgement.state_question,
        language=judgement.language,
    )


async def _deliver_chat(
    runtime: TurnRuntime,
    event: TranscriptEvent,
    planned: PlannedTurn,
    generation: int,
    outcome: CommandOutcome | None,
) -> None:
    factory, commands = runtime.chat_turn, runtime.commands
    if factory is None or commands is None:
        runtime.journal.record(
            "chat_reply_dropped", source="agent", transcript_id=event.id, reason="no_chat_channel"
        )
        return
    floor_generation = _floor_generation(runtime)
    hook = tool_start_hook(runtime, generation, floor_generation, event.id)
    reply_event = await with_evidence(runtime, event.text, for_reply(event, planned.plan, outcome))
    reply = await _respond_within(
        factory((reply_event,), hook), event.text, runtime.response_timeout_seconds
    )
    if not _turn_current(runtime, generation, floor_generation):
        raise TurnDropped(turn_drop_outcome(runtime, generation)[0])
    text = scrub_internal_refs(reply.text)
    sent: CommandOutcome | None = None
    if text:
        _move_phase(runtime, "acting", event.id)
        sent = await commands.run(
            CommandSpec("send_chat", text),
            plan_id=planned.plan.plan_id,
            ordinal=2,
            actor=planned.actor,
        )
        runtime.journal.record(
            "chat_reply_finished",
            source="agent",
            transcript_id=event.id,
            state=sent.state,
            chars=len(text),
        )
    if sent is not None and not sent.not_done:
        await _leave(runtime, planned)
    _move_phase(runtime, "listening" if text else "waiting", event.id)


async def _run_chat_turn(runtime: TurnRuntime, event: TranscriptEvent, generation: int) -> None:
    planned = plan_for(runtime, event, await _chat_reading(runtime, event), "chat")
    _record_plan(runtime, event.id, planned)
    done, outcome = await act_first(runtime, event, planned)
    if done:
        _move_phase(runtime, "waiting", event.id)
        return
    if await answer_state(runtime, event, planned, generation):
        return
    await _deliver_chat(runtime, event, planned, generation, outcome)


async def run_deferred_turn(runtime: TurnRuntime, speech: SpeechGraph, turn: DeferredTurn) -> bool:
    event = turn.event
    generation = _admitted_count(runtime)
    _move_phase(runtime, "weighing", event.id)
    scope = _turn_scope(runtime, event.id, generation)
    if scope is not None:
        record_agent_started(runtime.journal, scope)
    if turn.channel == "chat":
        work = _run_chat_turn(runtime, event, generation)
    else:
        work = _run_turn(runtime, speech, (event,), scope, generation)
    completed, error = await capture_error(partial(_held, runtime, work, scope))
    if isinstance(error, TurnDropped) or (error is None and not completed):
        return False
    if error is not None:
        if runtime.faults is not None:
            runtime.faults.record(classify_exception("agent", error, operation="deferred_turn"))
        runtime.journal.record(
            "deferred_turn_failed",
            source="agent",
            transcript_id=event.id,
            channel=turn.channel,
            error_type=type(error).__name__,
        )
        _move_phase(runtime, "waiting", event.id)
    return True


def _final_of(state: TurnGraphState) -> TranscriptEvent:
    event = state.get("transcript_event")
    if event is not None:
        return event
    turn = state["event"]
    return TranscriptEvent(
        id=turn.event_id,
        timestamp=datetime.fromtimestamp(turn.at, UTC).isoformat(),
        source="meeting_audio",
        text=turn.payload,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
    )


async def _start_pending(runtime: TurnRuntime, speech: SpeechGraph, queue: PendingFinals) -> None:
    scheduler = runtime.scheduler
    if scheduler is not None:
        scheduler.request_pending()
        return
    await drain_turns(runtime, speech, queue)


def _speaking_generation(runtime: TurnRuntime, state: TurnGraphState) -> int:
    floor = runtime.floor
    return state["speaking_generation"] if floor is None else floor.generation


async def _admit_final(
    state: TurnGraphState, runtime: TurnRuntime, speech: SpeechGraph
) -> dict[str, int | str | None | tuple[str, ...]]:
    final = _final_of(state)
    if runtime.shadow:
        runtime.journal.record(
            "transcript_suppressed", source="agent", transcript_id=final.id, reason="shadow"
        )
        return {"speaking_generation": state["speaking_generation"]}
    verdict = state.get("participation")
    if verdict is not None:
        planned = plan_for(runtime, final, reading_of(verdict), "speech")
        _record_plan(runtime, final.id, planned)
        if planned.plan.template == "silent":
            return {"speaking_generation": state["speaking_generation"]}
        runtime.plans[final.id] = planned
        for stale in list(runtime.plans)[:-MAX_PENDING_PLANS]:
            del runtime.plans[stale]
    queue = runtime.queue if runtime.queue is not None else PendingFinals()
    queue.admit(final)
    _advance_admitted(runtime)
    if _busy(runtime):
        _move_phase(runtime, "interrupted", final.id)
    await take_floor_for_final(runtime, final.text)
    await _start_pending(runtime, speech, queue)
    return {"speaking_generation": _speaking_generation(runtime, state)}


async def ingest_turn(state: TurnGraphState) -> dict[str, str | ParticipationDecision]:
    event = state["event"]
    if event.kind not in ("final", "stop", "interim"):
        raise ValueError(f"turn graph takes final, interim or stop events, got {event.kind}")
    update: dict[str, str | ParticipationDecision] = {"transcript_text": event.payload}
    if event.kind == "final" and state.get("admission") == "admit":
        update["participation"] = ParticipationDecision(
            decision="speak", confidence=1.0, reason_code="unaddressed_admitted"
        )
    return update


async def gather_branches(state: TurnGraphState) -> dict[str, Any]:
    del state
    return {}


async def record_turn_failure(state: TurnGraphState, runtime: Runtime[TurnRuntime]) -> None:
    runtime.context.journal.record("turn_failed", source="agent", code=state["failure"])


@dataclass(frozen=True, slots=True)
class _TurnNodes:
    participation_graph: ParticipationGraph
    speech_graph: SpeechGraph

    async def refresh(self, state: TurnGraphState, runtime: Runtime[TurnRuntime]) -> None:
        _prefetch_judgement(state, runtime.context)

    async def participate(
        self, state: TurnGraphState, runtime: Runtime[TurnRuntime]
    ) -> dict[str, ParticipationDecision | DecisionSource | str | None]:
        return await _participate(state, runtime.context, self.participation_graph)

    async def speak(
        self, state: TurnGraphState, runtime: Runtime[TurnRuntime]
    ) -> dict[str, int | str | None | tuple[str, ...]]:
        if state["event"].kind == "stop":
            return {
                "speaking_generation": await interrupt_speech(
                    runtime.context.floor, state["speaking_generation"]
                )
            }
        return await _admit_final(state, runtime.context, self.speech_graph)


def build_turn_graph(
    *,
    participation: ParticipationGraph | None = None,
) -> CompiledStateGraph[TurnGraphState, TurnRuntime, TurnInput, TurnGraphState]:
    nodes = _TurnNodes(
        participation_graph=(
            build_participation_subgraph() if participation is None else participation
        ),
        speech_graph=build_speech_graph(),
    )
    builder = StateGraph(TurnGraphState, input_schema=TurnInput, context_schema=TurnRuntime)
    builder.add_node("ingest", ingest_turn)
    builder.add_node("refresh", nodes.refresh)
    builder.add_node("participation", nodes.participate)
    builder.add_node("failed", record_turn_failure)
    builder.add_node("speak", nodes.speak)
    builder.add_edge(START, "ingest")
    builder.add_edge("refresh", END)
    builder.add_edge("failed", END)
    builder.add_edge("speak", END)
    _route_participation_directly(builder)
    return builder.compile()


type TurnGraphBuilder = StateGraph[TurnGraphState, TurnRuntime, TurnInput, TurnGraphState]
_DECISION_ROUTES: dict[Hashable, str] = {"speak": "speak", "failed": "failed", "end": END}


def _route_participation_directly(builder: TurnGraphBuilder) -> None:
    builder.add_conditional_edges(
        "ingest",
        route_after_ingest,
        {"speak": "speak", "participation": "participation", "refresh": "refresh"},
    )
    builder.add_conditional_edges("participation", route_after_participation, _DECISION_ROUTES)


class _RecordingSink:
    def __init__(self, inner: SentenceSink, delivered: list[str]) -> None:
        self._inner = inner
        self._delivered = delivered

    async def prepare(self, sentence: str, language: str) -> PreparedSpeech | None:
        return await self._inner.prepare(sentence, language)

    async def say(self, prepared: PreparedSpeech) -> None:
        await self._inner.say(prepared)
        self._delivered.append(prepared.text)


class _SupersedableSink:
    def __init__(
        self,
        inner: SentenceSink,
        floor: SpeechFloor,
        generation: int,
        context: "SpeechRuntime",
        delivered: list[str],
    ) -> None:
        self._inner = inner
        self._floor = floor
        self._generation = generation
        self._context = context
        self._delivered = delivered

    async def prepare(self, sentence: str, language: str) -> PreparedSpeech | None:
        return await self._inner.prepare(sentence, language)

    async def say(self, prepared: PreparedSpeech) -> None:
        if self._floor.admits(self._generation):
            await self._inner.say(prepared)
            return
        detail, reason = _drop_outcome(self._floor)
        record_drop(
            self._context.journal,
            self._context.scope,
            sentence_index=len(self._delivered),
            detail=detail,
            reason=reason,
        )
        await prepared.aclose()


async def _close_stream(stream: object, faults: FaultReporter | None) -> None:
    if isinstance(stream, SupportsAclose):
        await close_safely(stream.aclose, source="agent", operation="close_stream", faults=faults)


async def _replay(sentences: tuple[SpokenChunk, ...]) -> AsyncIterator[SpokenChunk]:
    for sentence in sentences:
        yield sentence


async def _deliver(replies: AsyncIterator[SpokenChunk], sink: SentenceSink) -> None:
    queue: asyncio.Queue[PreparedSpeech | None] = asyncio.Queue()
    slots = asyncio.Semaphore(LOOKAHEAD_DEPTH)
    producer = asyncio.create_task(_produce(replies, sink, queue, slots))
    taken: list[PreparedSpeech] = []
    try:
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            slots.release()
            taken.append(sentence)
            await sink.say(sentence)
        await producer
    finally:
        await _close_producer(producer)
        for sentence in taken:
            await sentence.aclose()
        await _close_queued(queue)


async def _produce(
    replies: AsyncIterator[SpokenChunk],
    sink: SentenceSink,
    queue: asyncio.Queue[PreparedSpeech | None],
    slots: asyncio.Semaphore,
) -> None:
    held: PreparedSpeech | None = None
    try:
        async for chunk in replies:
            await slots.acquire()
            sentence = await sink.prepare(chunk.text, chunk.language)
            if sentence is None:
                slots.release()
                continue
            held = sentence
            queue.put_nowait(sentence)
            held = None
    finally:
        if held is not None:
            await held.aclose()
        queue.put_nowait(None)


async def _close_producer(producer: asyncio.Task[None]) -> None:
    producer.cancel()
    await asyncio.gather(producer, return_exceptions=True)


async def _close_queued(queue: asyncio.Queue[PreparedSpeech | None]) -> None:
    while not queue.empty():
        sentence = queue.get_nowait()
        if sentence is not None:
            await sentence.aclose()


def _reply_stream(runtime: SpeechRuntime, transcript: str) -> AsyncIterator[SpokenChunk]:
    responder = runtime.responder
    if responder is None:
        raise RuntimeError("a streamed reply needs a responder")
    replies = responder.stream_reply(transcript)
    limits = runtime.sentences
    if limits is None:
        return replies
    return shape_sentences(replies, max_sentences=limits.max_sentences, language=limits.language)


def _whole_update(
    *,
    reply: bool = False,
    failure: str | None = None,
    error: Exception | None = None,
    settled: bool = False,
    silent: bool = False,
) -> dict[str, Any]:
    return {
        "reply": reply,
        "failure": failure,
        "error": error,
        "completed": False,
        "delivered": (),
        "settled": settled,
        "silent": silent,
        "agent_journaled": reply,
    }


def _advanced(floor: SpeechFloor | None, generation: int) -> int:
    if floor is None:
        return generation + 1
    return floor.advance() if floor.admits(generation) else floor.generation


def _suppressed(context: SpeechRuntime) -> bool:
    return context.paused is not None and context.paused()


def _still_current(context: SpeechRuntime, generation: int) -> bool:
    if context.current is not None:
        return context.current()
    floor = context.floor
    return floor is None or floor.admits(generation)


def _speech_bound(context: SpeechRuntime) -> bool:
    return context.bound is None or context.bound()


async def _respond_within(whole: WholeResponder, transcript: str, seconds: float) -> SpokenChunk:
    async with asyncio.timeout(seconds):
        return await whole.respond(transcript)


def _spoken_reply(
    context: SpeechRuntime, reply: SpokenChunk, request_id: str | None
) -> SpokenChunk:
    text = reply.text.strip()
    spoken = (
        text
        if context.sentences is None
        else cap_spoken_reply(text, context.sentences.max_sentences, reply.language)
    )
    if spoken != text:
        context.journal.record(
            "reply_capped",
            source="agent",
            request_id=request_id,
            chars_in=len(text),
            chars_out=len(spoken),
        )
    return SpokenChunk(text=spoken, language=reply.language)


async def _respond_whole(
    context: SpeechRuntime, whole: WholeResponder, transcript: str, generation: int
) -> dict[str, Any]:
    scope = context.scope
    if scope is not None and not context.held:
        record_agent_started(context.journal, scope)
    reply, error = await capture_error(
        lambda: _respond_within(whole, transcript, context.response_timeout_seconds)
    )
    if error is not None:
        if scope is not None:
            record_agent_failed(context.journal, scope)
        return {
            **_whole_update(failure="reply_failed", error=error),
            "speaking_generation": _advanced(context.floor, generation),
        }
    if reply is None:
        return _whole_update(settled=True, silent=True)
    if scope is not None:
        record_agent_finished(context.journal, scope)
    if context.raise_if_failed is not None:
        context.raise_if_failed()
    if not _still_current(context, generation):
        return _whole_update(settled=True)
    request_id = None if scope is None else scope.request_id
    if _suppressed(context):
        context.journal.record(
            "turn_suppressed", source="agent", request_id=request_id, reason="paused"
        )
        return _whole_update(settled=True)
    if not reply.text.strip() or not _speech_bound(context):
        return _whole_update(settled=True, silent=True)
    context.streams.reply = _replay((_spoken_reply(context, reply, request_id),))
    return _whole_update(reply=True)


def _open_reply(context: SpeechRuntime, transcript: str) -> AsyncIterator[SpokenChunk]:
    try:
        return _reply_stream(context, transcript)
    except Exception:
        if context.held and context.scope is not None:
            record_agent_failed(context.journal, context.scope)
        raise


async def respond(state: SpeechState, runtime: Runtime[SpeechRuntime]) -> dict[str, Any]:
    context = runtime.context
    if context.whole is not None:
        return await _respond_whole(
            context, context.whole, state["transcript"], state["speaking_generation"]
        )
    replies = _open_reply(context, state["transcript"])
    scope = context.scope
    if scope is None:
        context.streams.reply = replies
        context.streams.source = replies
        return {"reply": True}
    if not context.held:
        record_agent_started(context.journal, scope)
    context.streams.reply = within_sentence_budget(replies, scope.response_timeout_seconds)
    context.streams.source = replies
    return {"reply": True}


SpeakUpdate = dict[str, int | str | bool | tuple[str, ...] | Exception | None]


def _speak_update(
    generation: int, error: Exception | None, delivered: list[str], *, completed: bool
) -> SpeakUpdate:
    return {
        "speaking_generation": generation,
        "failure": "reply_failed" if error is not None else None,
        "error": error,
        "delivered": tuple(delivered),
        "completed": completed,
    }


@dataclass(frozen=True, slots=True)
class _SpeechEnd:
    error: Exception | None
    completed: bool
    cancelled: bool
    delivered: int


def _journal_turn_end(context: SpeechRuntime, floor: SpeechFloor, end: _SpeechEnd) -> None:
    scope = context.scope
    if scope is not None and end.error is not None:
        record_agent_failed(context.journal, scope)
    elif scope is not None and end.completed:
        record_agent_finished(context.journal, scope)
    if end.cancelled and end.delivered:
        detail, _reason = _drop_outcome(floor)
        record_sentence_dropped(context.journal, scope, sentence_index=end.delivered, detail=detail)


async def _deliver_held(
    replies: AsyncIterator[SpokenChunk], sink: SentenceSink
) -> tuple[bool, Exception | None]:
    _, error = await capture_error(lambda: _deliver(replies, sink))
    if isinstance(error, TurnDropped):
        return True, None
    return False, error


def _journal_held_end(context: SpeechRuntime, error: Exception | None, *, completed: bool) -> None:
    scope = context.scope
    if scope is None:
        return
    if error is not None:
        record_agent_failed(context.journal, scope)
    elif completed:
        record_agent_finished(context.journal, scope)


def _held_generation(floor: SpeechFloor | None, generation: int, *, completed: bool) -> int:
    if floor is None:
        return generation + 1
    if completed and floor.admits(generation):
        return floor.advance()
    return floor.generation


async def _speak_held(
    state: SpeechState, context: SpeechRuntime, reply: AsyncIterator[SpokenChunk]
) -> SpeakUpdate:
    delivered: list[str] = []
    recorder = _RecordingSink(context.sink, delivered)
    try:
        dropped, error = await _deliver_held(reply, recorder)
    except asyncio.CancelledError:
        if delivered and context.drop_detail is not None:
            record_sentence_dropped(
                context.journal,
                context.scope,
                sentence_index=len(delivered),
                detail=context.drop_detail(),
            )
        raise
    finally:
        await _close_stream(context.streams.source, context.faults)
    completed = error is None and not dropped
    if not state.get("agent_journaled"):
        _journal_held_end(context, error, completed=completed)
    generation = _held_generation(context.floor, state["speaking_generation"], completed=completed)
    return _speak_update(generation, error, delivered, completed=completed)


async def speak_reply(state: SpeechState, runtime: Runtime[SpeechRuntime]) -> SpeakUpdate:
    context = runtime.context
    reply = context.streams.reply
    if reply is None:
        raise RuntimeError("speak runs only after respond set a reply")
    if context.held:
        return await _speak_held(state, context, reply)
    floor = context.floor
    generation = state["speaking_generation"]
    agent_journaled = bool(state.get("agent_journaled"))
    delivered: list[str] = []
    recorder = _RecordingSink(context.sink, delivered)
    if floor is None:
        _, error = await capture_error(lambda: _deliver(reply, recorder))
        return _speak_update(generation + 1, error, delivered, completed=error is None)
    if not floor.admits(generation):
        return {
            "speaking_generation": floor.generation,
            "failure": None,
            "delivered": (),
            "completed": False,
        }
    sink = _SupersedableSink(recorder, floor, generation, context, delivered)
    request_id = None if context.scope is None else context.scope.request_id
    held, error = await capture_error(
        lambda: floor.hold(
            _deliver(reply, sink), transcript_id=context.transcript_id, request_id=request_id
        )
    )
    admitted = floor.admits(generation)
    completed = error is None and held is True and admitted
    if not agent_journaled:
        _journal_turn_end(
            context,
            floor,
            _SpeechEnd(
                error=error,
                completed=completed,
                cancelled=held is False,
                delivered=len(delivered),
            ),
        )
    return _speak_update(
        floor.advance() if admitted else floor.generation,
        error,
        delivered,
        completed=completed,
    )


def route_after_respond(state: SpeechState) -> Literal["speak", "failed", "end"]:
    if state.get("failure") is not None:
        return "failed"
    if not state.get("reply"):
        return "end"
    return "speak"


async def speech_failed(state: SpeechState, runtime: Runtime[SpeechRuntime]) -> None:
    context = runtime.context
    if context.scope is not None and state["failure"] == "reply_failed":
        return
    context.journal.record("turn_failed", source="agent", code=state["failure"])


def route_after_speak(state: SpeechState) -> Literal["failed", "end"]:
    return "failed" if state["failure"] is not None else "end"


def build_speech_graph() -> CompiledStateGraph[
    SpeechState, SpeechRuntime, SpeechInput, SpeechState
]:
    builder = StateGraph(SpeechState, input_schema=SpeechInput, context_schema=SpeechRuntime)
    builder.add_node("respond", respond)
    builder.add_node("speak", speak_reply)
    builder.add_node("failed", speech_failed)
    builder.add_edge(START, "respond")
    builder.add_conditional_edges(
        "respond", route_after_respond, {"speak": "speak", "failed": "failed", "end": END}
    )
    builder.add_conditional_edges("speak", route_after_speak, {"failed": "failed", "end": END})
    builder.add_edge("failed", END)
    return builder.compile()
