import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.control import ReplyControl
from fakeyou.conversation.floor import SpeechFloor
from fakeyou.conversation.participation_decision import ParticipationDecision
from fakeyou.conversation.participation_graph import JudgeThresholds, TurnJudging, decision_of
from fakeyou.conversation.session import (
    ConversationError,
    MeetingAudio,
    SupportsCues,
    SupportsLouder,
)
from fakeyou.conversation.thinking_cue import ThinkingCue
from fakeyou.conversation.turn_events import JournalSink, TurnEvent
from fakeyou.conversation.turn_graph import (
    CommandPort,
    ConversationPhase,
    EvidencePort,
    PlannedTurn,
    Responder,
    SentenceLimits,
    StateSpeech,
    StreamTurnFactory,
    TurnGraphState,
    TurnInput,
    TurnPlanning,
    TurnRunner,
    TurnRuntime,
    WholeResponder,
    WholeTurnFactory,
    build_speech_graph,
    cancel_speech,
    drain_turns,
    record_barge_in,
    run_deferred_turn,
)
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
from fakeyou.conversation.turn_queue import (
    AdmittedFinals,
    BargeLatch,
    DeferredTurn,
    DeferredTurns,
    PendingFinals,
    QuietFloor,
)
from fakeyou.conversation.turn_recovery import RecoveryOptions, TurnRecovery
from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.participation import (
    ParticipationContext,
    is_vocative,
    mentions_whole_phrase,
)
from fakeyou.foundation.resources import read_package_text
from fakeyou.foundation.transcript import TranscriptEvent, transcript_stamp
from fakeyou.foundation.triggers import ReplyChannel
from fakeyou.observability.faults import ConsumerSink, capture_error, guard_observer
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_exception
from fakeyou.speech.contract import PreparedSpeech

_ADMITTED = frozenset({"speak", "clarify"})
_STOP_DATA: dict[str, list[str]] = json.loads(read_package_text("stop_phrases.json"))
_STOP_STRONG = frozenset(_STOP_DATA["strong"])
_STOP_FILLER = frozenset(_STOP_DATA["filler"])
_STOP_PHRASES = frozenset(_STOP_DATA["phrases"])
_STOP_MAX_TOKENS = 6
_OVERLAP_WINDOW_SECONDS = 20.0
WAKEUP_FLOOR_SECONDS = 0.25
PublicPhase = Literal["active", "failed", "leaving"]
ReplyMode = Literal["whole_turn", "streamed"]

_PUBLIC_OF: dict[ConversationPhase, PublicPhase] = {
    "listening": "active",
    "weighing": "active",
    "queued": "active",
    "interrupted": "active",
    "waiting": "active",
    "acting": "active",
    "failed": "failed",
    "closed": "leaving",
}

TransitionObserver = Callable[[ConversationPhase, ConversationPhase, str], None]


def public_phase(phase: ConversationPhase) -> PublicPhase:
    return _PUBLIC_OF[phase]


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
class ReplyOptions:
    whole: WholeResponder | None = None
    whole_turn: WholeTurnFactory | None = None
    stream_turn: StreamTurnFactory | None = None
    sentences: SentenceLimits | None = None


@dataclass(frozen=True, slots=True)
class DeferredOptions:
    quiet_gap_seconds: float = DEFAULTS.perception.attention.floor_gap_seconds
    ttl_seconds: float = DEFAULTS.perception.attention.ttl_seconds
    chat_turn: WholeTurnFactory | None = None
    planning: TurnPlanning | None = None
    commands: CommandPort | None = None
    state_speech: StateSpeech | None = None
    evidence: EvidencePort | None = None
    others_speaking: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True)
class IngressTiming:
    barge_in_min_words: int = DEFAULTS.conversation.barge_in_min_words
    clock: Callable[[], float] = monotonic


@dataclass(frozen=True, slots=True)
class IngressOptions:
    addressing_names: tuple[str, ...] = ()
    wake_phrases: tuple[str, ...] = ()
    timing: IngressTiming = IngressTiming()
    barge_in_judge_enabled: bool = True


DEFAULT_INGRESS_OPTIONS = IngressOptions()


@dataclass(frozen=True, slots=True)
class ActorWiring:
    journal: JournalSink
    ingress: IngressOptions = DEFAULT_INGRESS_OPTIONS
    responder: Responder | None = None
    judging: TurnJudging | None = None
    clock: Callable[[], float] = monotonic


@dataclass(frozen=True, slots=True)
class PolicyOptions:
    enabled: bool = False
    others_speaking: Callable[[], bool] | None = None


def reply_mode(*, sentence_streaming: bool) -> ReplyMode:
    return "streamed" if sentence_streaming else "whole_turn"


def addressing_phrases(phrases: Sequence[str]) -> tuple[str, ...]:
    return tuple(phrase.strip().casefold() for phrase in phrases if phrase.strip())


def judge_yields_the_floor(judgement: TurnJudgement, thresholds: JudgeThresholds) -> bool:
    if judgement.backchannel or judgement.addressee != "twin":
        return False
    return judgement.confidence >= thresholds.barge_in


def _running(task: asyncio.Task[None] | None) -> bool:
    return task is not None and not task.done()


class FloorActor:
    def __init__(self, turn: TurnRunner, wiring: ActorWiring) -> None:
        journal = wiring.journal
        ingress = wiring.ingress
        responder = wiring.responder
        judging = wiring.judging
        clock = wiring.clock
        resolved = ingress.timing
        self._turn = turn
        self._speech = build_speech_graph()
        self._barge_in = True
        self._shadow = False
        self._cue: ThinkingCue | None = None
        self._plans: dict[str, PlannedTurn] = {}
        self._journal = journal
        self._responder = responder
        self._judging = judging
        self._control: ReplyControl | None = None
        self._clock = resolved.clock
        self._options = RecoveryOptions()
        self._recovery = self._build_recovery()
        self._reply = ReplyOptions()
        self._observer: TransitionObserver | None = None
        self._spoken: Callable[[str], None] | None = None
        self._phase: ConversationPhase = "listening"
        self._floor = SpeechFloor(clock=clock)
        self._queue = PendingFinals()
        self._deferred = DeferredTurns()
        self._deferred_options = DeferredOptions()
        self._quiet = QuietFloor(clock=clock)
        self._wakeup: asyncio.TimerHandle | None = None
        self._admitted = AdmittedFinals()
        self._barge = BargeLatch()
        self._events: list[TranscriptEvent] = []
        self._session: MeetingAudio | None = None
        self._voice_drain: asyncio.Task[None] | None = None
        self._chat_drain: asyncio.Task[None] | None = None
        self._attention_drain: asyncio.Task[None] | None = None
        self._pending_requested = False
        self._closed = False
        self._paused = False
        self._failure: ConversationError | None = None
        self._addressing_names = addressing_phrases(ingress.addressing_names)
        self._wake_phrases = addressing_phrases(ingress.wake_phrases)
        self._participant_policy = False
        self._others_speaking_signal: Callable[[], bool] | None = None
        self._barge_in_min_words = resolved.barge_in_min_words
        self._barge_in_judge_enabled = ingress.barge_in_judge_enabled
        self._seen: set[str] = set()
        self._in_flight = False
        self._judge_tasks: set[asyncio.Task[None]] = set()
        self._last_attention: str | None = None
        self._last_final: tuple[str, str, float] | None = None
        self._runtime: TurnRuntime | None = self._build_runtime()

    @property
    def turn(self) -> TurnRunner:
        return self._turn

    @property
    def journal(self) -> JournalSink:
        return self._journal

    @property
    def floor(self) -> SpeechFloor:
        return self._floor

    @property
    def phase(self) -> ConversationPhase:
        return self._phase

    @property
    def queue(self) -> PendingFinals:
        return self._queue

    @property
    def admitted(self) -> AdmittedFinals:
        return self._admitted

    @property
    def deferred(self) -> DeferredTurns:
        return self._deferred

    @property
    def quiet_floor(self) -> QuietFloor:
        return self._quiet

    @property
    def barge(self) -> BargeLatch:
        return self._barge

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return tuple(self._events)

    @property
    def recovery(self) -> TurnRecovery:
        return self._recovery

    @property
    def judging(self) -> TurnJudging | None:
        return self._judging

    @property
    def barge_in(self) -> bool:
        return self._barge_in

    @property
    def session(self) -> MeetingAudio | None:
        return self._session

    @property
    def runtime(self) -> TurnRuntime | None:
        return self._runtime

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_failed(self) -> bool:
        return self._failure is not None

    @property
    def is_busy(self) -> bool:
        return any(_running(task) for task in self._lane_tasks()) or self._floor.busy

    @property
    def accepts_ingress(self) -> bool:
        return not self._closed and not self._paused and self._failure is None

    def note_activity(self) -> None:
        self._quiet.note_activity()

    def note_final(self, event: TranscriptEvent) -> None:
        self._events.append(event)

    def _observer_sink(self) -> ConsumerSink:
        journal = self._journal
        return ConsumerSink(self._options.faults, journal if isinstance(journal, Journal) else None)

    def configure_recovery(self, options: RecoveryOptions) -> None:
        self._options = options
        self._recovery = self._build_recovery()
        self._refresh_runtime()

    def configure_reply(self, options: ReplyOptions) -> None:
        self._reply = options
        self._refresh_runtime()

    def configure_barge_in(self, *, enabled: bool) -> None:
        self._barge_in = enabled
        self._refresh_runtime()

    def configure_shadow(self, *, enabled: bool) -> None:
        self._shadow = enabled
        self._refresh_runtime()

    def configure_deferred(self, options: DeferredOptions) -> None:
        self._deferred_options = options
        self._refresh_runtime()

    def configure_control(self, control: ReplyControl | None) -> None:
        self._control = control
        self._bind_control()

    def configure_cue(self, cue: ThinkingCue | None) -> None:
        self._cue = cue
        self._prepare_cues()
        self._refresh_runtime()

    def set_addressing_names(self, names: Sequence[str]) -> None:
        self._addressing_names = addressing_phrases(names)

    def configure_policy(self, options: PolicyOptions) -> None:
        if options.enabled and not self._wake_phrases:
            raise ValueError("participant policy requires configured twin aliases")
        self._participant_policy = options.enabled
        self._others_speaking_signal = options.others_speaking

    def _prepare_cues(self) -> None:
        session = self._session
        if self._cue is not None and isinstance(session, SupportsCues):
            session.prepare_cues(self._cue.lines)

    def _bind_control(self) -> None:
        control = self._control
        session = self._session
        if control is None or session is None:
            return
        if isinstance(session, SupportsLouder):
            control.on_louder = session.louder

    def observe_spoken(self, observer: Callable[[str], None]) -> None:
        self._spoken = observer

    def observe(self, observer: TransitionObserver) -> None:
        self._observer = observer

    def move(self, phase: ConversationPhase, detail: str) -> None:
        previous, self._phase = self._phase, phase
        observer = self._observer
        if observer is not None:
            guard_observer(
                partial(observer, previous, phase, detail),
                sink=self._observer_sink(),
                source="agent",
                detail="transition_observer",
                operation="transition_observer",
            )

    def _build_recovery(self) -> TurnRecovery:
        return TurnRecovery(
            journal=self._journal,
            faults=self._options.faults,
            language=self._options.language,
            is_closed=lambda: self._closed,
            clock=self._clock,
        )

    def _refresh_runtime(self) -> None:
        if not self._closed:
            self._runtime = self._build_runtime()

    def _build_runtime(self) -> TurnRuntime:
        return TurnRuntime(
            speaker=_ActorSpeaker(self),
            journal=self._journal,
            responder=self._responder,
            floor=self._floor,
            judging=self._judging,
            phase=self,
            queue=self._queue,
            recovery=self._recovery,
            on_fail=self.fail,
            response_timeout_seconds=self._options.response_timeout_seconds,
            sentences=self._reply.sentences,
            whole=self._reply.whole,
            whole_turn=self._reply.whole_turn,
            stream_turn=self._reply.stream_turn,
            admitted=self._admitted,
            closed=lambda: self._closed,
            paused=lambda: self._paused,
            barge_in=self._barge_in,
            raise_if_failed=self.raise_if_failed,
            scheduler=self,
            bound=lambda: self._session is not None,
            barge=self._barge,
            note_spoken=self._note_spoken,
            shadow=self._shadow,
            faults=self._options.faults,
            chat_turn=self._deferred_options.chat_turn,
            planning=self._deferred_options.planning,
            commands=self._deferred_options.commands,
            state_speech=self._deferred_options.state_speech,
            evidence=self._deferred_options.evidence,
            plans=self._plans,
            cue=self._cue,
            others_speaking=self._deferred_options.others_speaking,
        )

    def _note_spoken(self, sentence: str) -> None:
        self._quiet.note_activity()
        if self._spoken is not None:
            self._spoken(sentence)

    def bind(self, session: MeetingAudio) -> None:
        if self._closed:
            raise ConversationError("meeting conversation is closed")
        if self._session is not None and self._session is not session:
            raise ConversationError("meeting conversation already owns an audio session")
        self._session = session
        self._bind_control()
        self._prepare_cues()
        self.start_pending()

    def request_pending(self) -> None:
        self._pending_requested = True

    def start_requested(self) -> None:
        pending, self._pending_requested = self._pending_requested, False
        if pending:
            self.start_pending()

    def defer(
        self, event: TranscriptEvent, *, channel: ReplyChannel, needs_quiet_floor: bool
    ) -> bool:
        if not self.accepts_ingress or self._shadow:
            self._journal.record(
                "deferred_turn_suppressed",
                source="agent",
                transcript_id=event.id,
                reason="shadow" if self._shadow else "not_accepting",
            )
            return False
        turn = DeferredTurn(
            event=event,
            channel=channel,
            expires_at=self._clock() + self._deferred_options.ttl_seconds,
            needs_quiet_floor=needs_quiet_floor,
        )
        if not self._deferred.add(turn):
            return False
        self._journal.record(
            "deferred_turn_queued", source="agent", transcript_id=event.id, channel=channel
        )
        self.start_pending()
        return True

    def start_pending(self) -> None:
        runtime = self._runtime
        if runtime is None or self._failure is not None or self._session is None:
            return
        if self._queue.pending and not _running(self._voice_drain):
            self._voice_drain = asyncio.create_task(self._drain_pending_finals(runtime))
        if self._deferred.pending_for("chat") and not _running(self._chat_drain):
            self._chat_drain = asyncio.create_task(self._drain_deferred(runtime, "chat"))
        if self._deferred.pending_for("voice") and not _running(self._attention_drain):
            self._attention_drain = asyncio.create_task(self._drain_deferred(runtime, "voice"))

    async def _drain_pending_finals(self, runtime: TurnRuntime) -> None:
        while not self._closed and self._failure is None:
            if not self._queue.pending:
                break
            await drain_turns(runtime, self._speech, self._queue)
        if not self._closed and self._failure is None:
            self.start_pending()
        self._arm_wakeup()

    async def _drain_deferred(self, runtime: TurnRuntime, channel: ReplyChannel) -> None:
        while not self._closed and self._failure is None:
            turn = self._next_deferred(channel)
            if turn is None:
                break
            delivered = await run_deferred_turn(runtime, self._speech, turn)
            if not delivered and not self._closed and not self._deferred.retry(turn):
                self._journal.record(
                    "deferred_turn_dropped",
                    source="agent",
                    transcript_id=turn.event.id,
                    channel=turn.channel,
                    reason="preempted",
                )
        self._arm_wakeup()

    def _lane_tasks(self) -> list[asyncio.Task[None] | None]:
        return [self._voice_drain, self._chat_drain, self._attention_drain]

    def _next_deferred(self, channel: ReplyChannel) -> DeferredTurn | None:
        for expired in self._deferred.expire(self._clock()):
            self._journal.record(
                "deferred_turn_expired",
                source="agent",
                transcript_id=expired.event.id,
                channel=expired.channel,
            )
        ready = self._chat_ready if channel == "chat" else self._attention_ready
        return self._deferred.next_ready_for(channel, ready)

    def _chat_ready(self, turn: DeferredTurn) -> bool:
        del turn
        return not self._paused and not self._shadow

    def _attention_ready(self, turn: DeferredTurn) -> bool:
        del turn
        if self._paused or self._shadow:
            return False
        if self._queue.pending or _running(self._voice_drain):
            return False
        session = self._session
        if session is not None and session.is_speaking:
            return False
        others = self._deferred_options.others_speaking
        if others is not None and others():
            return False
        return self._quiet.quiet_for() >= self._deferred_options.quiet_gap_seconds

    def _arm_wakeup(self) -> None:
        if self._wakeup is not None:
            self._wakeup.cancel()
            self._wakeup = None
        if self._closed or not self._deferred.pending:
            return
        remaining = self._deferred_options.quiet_gap_seconds - self._quiet.quiet_for()
        expiry = self._deferred.earliest_expiry()
        delay = max(WAKEUP_FLOOR_SECONDS, remaining)
        if expiry is not None:
            delay = min(delay, max(WAKEUP_FLOOR_SECONDS, expiry - self._clock()))
        self._wakeup = asyncio.get_running_loop().call_later(delay, self.start_pending)

    def fail(self, cause: Exception) -> None:
        if self._failure is None:
            if self._options.faults is not None:
                self._options.faults.record(classify_exception("agent", cause))
            self._failure = ConversationError("meeting conversation failed")
            self._failure.__cause__ = cause
            self._phase = "failed"

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def wait_idle(self) -> None:
        for task in self._lane_tasks():
            if task is not None:
                await asyncio.shield(task)
        active = self._floor.active
        if active is not None:
            await asyncio.shield(active.task)
        self.raise_if_failed()

    async def drained(self) -> None:
        lanes = [task for task in self._lane_tasks() if task is not None]
        if lanes:
            await asyncio.gather(*lanes, return_exceptions=True)

    async def settled(self) -> None:
        tasks = list(self._judge_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def pause(self) -> None:
        self._paused = True
        session = self._session
        if session is not None and session.is_speaking:
            await session.stop_playback()

    def resume(self) -> None:
        self._paused = False
        self.start_pending()

    async def close(self) -> None:
        self._closed = True
        self._admitted.advance()
        self._queue.clear()
        self._deferred.clear()
        if self._wakeup is not None:
            self._wakeup.cancel()
            self._wakeup = None
        if self._judging is not None:
            self._judging.cache.cancel()
        self._session = None
        self._runtime = None
        await self._release_floor()
        lanes: list[asyncio.Task[None]] = []
        for task in (self._voice_drain, self._chat_drain, self._attention_drain):
            if task is not None and not task.done():
                lanes.append(task)
        for task in lanes:
            task.cancel()
        if lanes:
            await asyncio.gather(*lanes, return_exceptions=True)
        self._phase = "closed"

    async def _release_floor(self) -> None:
        active = self._floor.active
        if active is not None and not active.task.done():
            await cancel_speech(self._floor, self._journal, reason="close")
            return
        await self._floor.interrupt("close")

    async def receive_final(self, event: TranscriptEvent) -> ParticipationDecision | None:
        runtime = self._open_final(event)
        if runtime is None:
            return None
        output = await self._invoke_final(event, runtime)
        self.start_requested()
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
        return self.defer(event, channel="chat", needs_quiet_floor=False)

    def receive_attention(self, event: TranscriptEvent, *, channel: ReplyChannel) -> bool:
        if not event.text.strip():
            return False
        if event.text == self._last_attention:
            return False
        self._last_attention = event.text
        return self.defer(event, channel=channel, needs_quiet_floor=channel == "voice")

    async def handle_final(self, event: TranscriptEvent) -> ParticipationDecision | None:
        decision = await self.receive_final(event)
        await self.drained()
        return decision

    async def ready_to_answer(self, text: str) -> bool:
        judging = self._judging
        session = self._session
        if judging is None or self._closed or self._failure is not None or not text.strip():
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
        await cancel_speech(self._floor, self._journal, reason=reason)

    async def handle_interim(self, text: str) -> None:
        if text.strip():
            self.note_activity()
        window: JudgeWindow | None = None
        if self._runtime is not None and not self._closed and self._failure is None:
            window = await self._prefetch(text)
        session = self._session
        if self._closed or self._failure is not None or not self._barge_in or session is None:
            return
        words = len(text.split())
        if is_stop_command(text, (*self._addressing_names, *self._wake_phrases)):
            if self._twin_holds_floor(session) and text != self._barge.last_interim:
                self._barge.last_interim = text
                await self._yield_the_floor(words, decided_by="stop_lexicon")
            return
        if words < self._barge_in_min_words:
            return
        active = self._floor.active
        if not session.is_speaking and (active is None or active.task.done()):
            return
        latch = self._barge
        if text == latch.last_interim:
            return
        latch.last_interim = text
        judging = self._judging
        if judging is None or not self._barge_in_judge_enabled:
            await self._yield_the_floor(words, decided_by=None)
            return
        self._ask_the_judge(text, words, window)

    def _twin_holds_floor(self, session: Any) -> bool:
        active = self._floor.active
        return bool(session.is_speaking or (active is not None and not active.task.done()))

    def _ask_the_judge(self, text: str, words: int, window: JudgeWindow | None) -> None:
        generation = self._floor.generation
        task = asyncio.create_task(self._judge_the_barge_in(text, words, window, generation))
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)

    async def _judge_the_barge_in(
        self, text: str, words: int, window: JudgeWindow | None, generation: int
    ) -> None:
        judging = self._judging
        if judging is None:
            return
        session = self._session
        if window is None:
            window = judging.windows.for_interim(
                text, twin_speaking=session is not None and session.is_speaking
            )
        judgement, _ = await capture_error(partial(judging.cache.judgement, window))
        if generation != self._floor.generation:
            self._journal.record(
                "barge_in_rejected", source="agent", reason="stale_generation", words=words
            )
            return
        if judgement is None:
            await self._yield_the_floor(words, decided_by="judge_unavailable")
            return
        if not judge_yields_the_floor(judgement, judging.thresholds):
            self._journal.record(
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
        session = self._session
        latch = self._barge
        if session is None:
            return
        active = self._floor.active
        if not session.is_speaking and (active is None or active.task.done()):
            return
        if not latch.interrupted:
            latch.interrupted = True
            record_barge_in(
                self._journal, "interim", words, decided_by=decided_by, interrupted=active
            )
            if session.is_speaking:
                await session.stop_playback()
                latch.stream_stops += 1
        await cancel_speech(self._floor, self._journal, reason="interim")

    def _is_overlap_duplicate(self, event: TranscriptEvent) -> bool:
        if self._last_final is None:
            return False
        participant_id, previous, admitted_at = self._last_final
        if participant_id != event.participant_id:
            return False
        if self._clock() - admitted_at > _OVERLAP_WINDOW_SECONDS:
            return False
        current = " ".join(_tokens(event.text))
        if current == previous:
            return False
        if min(len(previous), len(current)) < 8:
            return False
        shorter, longer = sorted((previous, current), key=len)
        return longer.startswith(shorter) or longer.endswith(shorter)

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
        if self._closed or self._failure is not None:
            return None
        if not event.finalized or not event.text.strip():
            return None
        self.note_activity()
        if self._paused:
            self._journal.record(
                "transcript_suppressed", source="agent", transcript_id=event.id, reason="paused"
            )
            return None
        runtime = self._runtime
        if runtime is None or event.id in self._seen:
            return None
        if self._is_overlap_duplicate(event):
            self._journal.record(
                "transcript_suppressed",
                source="agent",
                transcript_id=event.id,
                reason="overlap_duplicate",
            )
            return None
        self._seen.add(event.id)
        self.note_final(event)
        return runtime

    async def _invoke_final(self, event: TranscriptEvent, runtime: TurnRuntime) -> TurnGraphState:
        self._in_flight = True
        try:
            return await self._turn.run(
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
                    speaking_generation=self._floor.generation,
                    transcript_event=event,
                    history=list(self._events),
                    addressing_names=self._addressing_names,
                    admission="rules" if self._rules_admission else "admit",
                ),
                runtime,
            )
        finally:
            self._in_flight = False

    async def _yield_floor(self, text: str, decision: ParticipationDecision) -> None:
        session = self._session
        if decision.backchannel or not self._barge_in or session is None:
            return
        if not session.is_speaking and decision.addressee != "twin":
            return
        record_barge_in(self._journal, "final", len(text.split()), interrupted=self._floor.active)
        if session.is_speaking:
            await session.stop_playback()
        await cancel_speech(self._floor, self._journal, reason="final")

    async def _prefetch(self, text: str) -> JudgeWindow | None:
        runtime = self._runtime
        if runtime is None:
            return None
        judging = self._judging
        window = (
            judging.windows.for_interim(text, twin_speaking=runtime.speaker.is_speaking)
            if judging is not None
            else None
        )
        events = list(self._events)
        await self._turn.run(
            TurnInput(
                event=TurnEvent(
                    kind="interim",
                    event_id=events[-1].id if events else "",
                    at=self._clock(),
                    payload=text,
                ),
                context=ParticipationContext(addressed=False),
                speaking_generation=self._floor.generation,
                history=events,
                addressing_names=self._addressing_names,
                pending=self._in_flight or self._queue.pending,
                judge_window=window,
            ),
            runtime,
        )
        return window


class _ActorSpeaker:
    def __init__(self, actor: FloorActor) -> None:
        self._actor = actor

    @property
    def is_speaking(self) -> bool:
        session = self._actor.session
        return session is not None and session.is_speaking

    async def speak(
        self,
        utterance: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        session = self._session()
        if sentence_index is None and transcript_id is None:
            await session.speak(utterance, language=language)
            return
        await session.speak(
            utterance,
            sentence_index=sentence_index,
            transcript_id=transcript_id,
            language=language,
        )

    async def stop_playback(self) -> None:
        await self._session().stop_playback()

    async def prepare(
        self,
        utterance: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech | None:
        return await self._session().prepare(
            utterance,
            sentence_index=sentence_index,
            transcript_id=transcript_id,
            language=language,
        )

    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        await self._session().speak_prepared(prepared)

    async def play_cue(self, text: str) -> bool:
        session = self._actor.session
        if not isinstance(session, SupportsCues):
            return False
        return await session.play_cue(text)

    def _session(self) -> MeetingAudio:
        session = self._actor.session
        if session is None:
            raise ConversationError("meeting conversation has no audio session")
        return session
