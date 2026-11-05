import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import partial
from typing import Any, Literal, Self

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.control import ReplyControl
from fakeyou.conversation.floor import SpeechFloor
from fakeyou.conversation.participation_decision import ParticipationDecision
from fakeyou.conversation.participation_graph import JudgeThresholds, TurnJudging, decision_of
from fakeyou.conversation.session import (
    ConversationError,
    MeetingAudio,
    SupportsCues,
    SupportsDucking,
    SupportsFreezing,
    SupportsLouder,
)
from fakeyou.conversation.thinking_cue import ThinkingCue
from fakeyou.conversation.turn_graph import (
    CommandPort,
    ConversationPhase,
    EvidencePort,
    PlannedTurn,
    Responder,
    SentenceLimits,
    Speaker,
    StateSpeech,
    StreamTurnFactory,
    TurnGraphState,
    TurnInput,
    TurnPlanning,
    TurnRunner,
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
    TurnEvent,
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
from fakeyou.observability.faults import ConsumerSink, FaultReporter, capture_error, guard_observer
from fakeyou.observability.journal import Journal, JournalSink
from fakeyou.observability.provider_faults import classify_exception
from fakeyou.speech.contract import PreparedSpeech

_ADMITTED = frozenset({"speak", "clarify"})
_STOP_DATA: dict[str, list[str]] = json.loads(read_package_text("stop_phrases.json"))
_STOP_STRONG = frozenset(_STOP_DATA["strong"])
_STOP_FILLER = frozenset(_STOP_DATA["filler"])
_STOP_PHRASES = frozenset(_STOP_DATA["phrases"])
_STOP_MAX_TOKENS = 6
_OVERLAP_WINDOW_SECONDS = 20.0
_RESUMED_MATCH = 0.7
_EXTRA_WORDS_FOR_MORE = 3
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


def _early_words(text: str) -> list[str]:
    return "".join(ch for ch in text.casefold() if ch.isalnum() or ch.isspace()).split()


def says_more(text: str, committed: str) -> bool:
    before, after = _early_words(committed), _early_words(text)
    if len(after) <= len(before):
        return False
    return SequenceMatcher(None, before, after[: len(before)]).ratio() >= _RESUMED_MATCH


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
    duck_grace_seconds: float = 1.5
    freeze_sustain_seconds: float = 0.6
    freeze_gap_seconds: float = 0.25
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
class EarlyOptions:
    hold_seconds: float
    build_event: Callable[[str], TranscriptEvent]
    prosody_threshold: float = 0.5
    prosody_expected: bool = False
    prosody_wait_seconds: float = 0.25


@dataclass(slots=True)
class _EarlyCommit:
    event: TranscriptEvent
    text: str
    resumed: bool = False


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
        self._duck_grace_seconds = resolved.duck_grace_seconds
        self._freeze_sustain_seconds = resolved.freeze_sustain_seconds
        self._freeze_gap_seconds = resolved.freeze_gap_seconds
        self._ducked = False
        self._frozen = False
        self._duck_timer: asyncio.Task[None] | None = None
        self._freeze_timer: asyncio.Task[None] | None = None
        self._voice_active = False
        self._voice_last_at = 0.0
        self._turn_end_probability: float | None = None
        self._turn_end_scored = asyncio.Event()
        self._barge_seq = 0
        self._barge_in_judge_enabled = ingress.barge_in_judge_enabled
        self._seen: set[str] = set()
        self._in_flight = False
        self._judge_tasks: set[asyncio.Task[None]] = set()
        self._last_attention: str | None = None
        self._last_final: tuple[str, str, float] | None = None
        self._early_options: EarlyOptions | None = None
        self._early_answer: Callable[[TranscriptEvent], Awaitable[None]] | None = None
        self._early_text = ""
        self._early_version = 0
        self._early_timer: asyncio.Task[None] | None = None
        self._early_starting: asyncio.Task[None] | None = None
        self._early_commit: _EarlyCommit | None = None
        self._early_withdrawn: set[str] = set()
        self._early_declined: tuple[int, tuple[str, ...]] | None = None
        self._interim_window: JudgeWindow | None = None
        self._early_window: JudgeWindow | None = None
        self._speaker = _ActorSpeaker(self)

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
    def speaker(self) -> Speaker:
        return self._speaker

    @property
    def plans(self) -> dict[str, PlannedTurn]:
        return self._plans

    @property
    def responder(self) -> Responder | None:
        return self._responder

    @property
    def sentences(self) -> SentenceLimits | None:
        return self._reply.sentences

    @property
    def whole(self) -> WholeResponder | None:
        return self._reply.whole

    @property
    def whole_turn(self) -> WholeTurnFactory | None:
        return self._reply.whole_turn

    @property
    def stream_turn(self) -> StreamTurnFactory | None:
        return self._reply.stream_turn

    @property
    def chat_turn(self) -> WholeTurnFactory | None:
        return self._deferred_options.chat_turn

    @property
    def planning(self) -> TurnPlanning | None:
        return self._deferred_options.planning

    @property
    def commands(self) -> CommandPort | None:
        return self._deferred_options.commands

    @property
    def state_speech(self) -> StateSpeech | None:
        return self._deferred_options.state_speech

    @property
    def evidence(self) -> EvidencePort | None:
        return self._deferred_options.evidence

    @property
    def cue(self) -> ThinkingCue | None:
        return self._cue

    @property
    def faults(self) -> FaultReporter | None:
        return self._options.faults

    @property
    def others_speaking(self) -> Callable[[], bool] | None:
        return self._deferred_options.others_speaking

    @property
    def note_spoken(self) -> Callable[[str], None]:
        return self._note_spoken

    @property
    def shadow(self) -> bool:
        return self._shadow

    @property
    def response_timeout_seconds(self) -> float:
        return self._options.response_timeout_seconds

    @property
    def is_bound(self) -> bool:
        return self._session is not None

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

    def configure_reply(self, options: ReplyOptions) -> None:
        self._reply = options

    def configure_barge_in(self, *, enabled: bool) -> None:
        self._barge_in = enabled

    def configure_shadow(self, *, enabled: bool) -> None:
        self._shadow = enabled

    def configure_deferred(self, options: DeferredOptions) -> None:
        self._deferred_options = options

    def configure_early(
        self,
        options: EarlyOptions,
        *,
        answer: Callable[[TranscriptEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._early_options = options
        self._early_answer = answer

    def configure_control(self, control: ReplyControl | None) -> None:
        self._control = control
        self._bind_control()

    def configure_cue(self, cue: ThinkingCue | None) -> None:
        self._cue = cue
        self._prepare_cues()

    def set_addressing_names(self, names: Sequence[str]) -> None:
        self._addressing_names = addressing_phrases(names)

    def configure_policy(self, options: PolicyOptions) -> None:
        if options.enabled and not (self._wake_phrases or self._addressing_names):
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
        if self._failure is not None or self._session is None:
            return
        if self._queue.pending and not _running(self._voice_drain):
            self._voice_drain = asyncio.create_task(self._drain_pending_finals())
        if self._deferred.pending_for("chat") and not _running(self._chat_drain):
            self._chat_drain = asyncio.create_task(self._drain_deferred("chat"))
        if self._deferred.pending_for("voice") and not _running(self._attention_drain):
            self._attention_drain = asyncio.create_task(self._drain_deferred("voice"))

    async def _drain_pending_finals(self) -> None:
        while not self._closed and self._failure is None:
            if not self._queue.pending:
                break
            await drain_turns(self, self._speech, self._queue)
        if not self._closed and self._failure is None:
            self.start_pending()
        self._arm_wakeup()

    async def _drain_deferred(self, channel: ReplyChannel) -> None:
        while not self._closed and self._failure is None:
            turn = self._next_deferred(channel)
            if turn is None:
                break
            delivered = await run_deferred_turn(self, self._speech, turn)
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
        while True:
            candidates = (self._early_timer, self._early_starting)
            pending: list[asyncio.Task[None]] = [
                task for task in candidates if task is not None and not task.done()
            ]
            if not pending:
                break
            for task in pending:
                await asyncio.shield(task)
        for lane in self._lane_tasks():
            if lane is not None:
                await asyncio.shield(lane)
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
        self._unduck(None)
        session = self._session
        if session is not None and session.is_speaking:
            await session.stop_playback()

    def reset_duck(self) -> None:
        self._unduck(None)

    def resume(self) -> None:
        self._paused = False
        self.start_pending()

    async def close(self) -> None:
        self._closed = True
        self._admitted.advance()
        self._queue.clear()
        self._deferred.clear()
        early_tasks = [
            task
            for task in (
                self._early_timer,
                self._early_starting,
                self._duck_timer,
                self._freeze_timer,
            )
            if task is not None
        ]
        self._early_timer = self._early_starting = None
        self._duck_timer = None
        self._freeze_timer = None
        for early_task in early_tasks:
            early_task.cancel()
        if early_tasks:
            await asyncio.gather(*early_tasks, return_exceptions=True)
        self._unduck(None)
        if self._wakeup is not None:
            self._wakeup.cancel()
            self._wakeup = None
        if self._judging is not None:
            self._judging.cache.cancel()
        self._session = None
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
        if event.id in self._early_withdrawn:
            self._early_withdrawn.discard(event.id)
            return None
        if self._open_final(event) is None:
            return None
        output = await self._invoke_final(event)
        if event.id in self._early_withdrawn:
            self._early_withdrawn.discard(event.id)
            self._queue.discard(event.id)
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
        return await self._early_readiness(text) is None

    def _interim_judgement(self, text: str) -> TurnJudgement | None:
        judging = self._judging
        window = self._interim_window
        if judging is None or window is None:
            return None
        if _early_words(window.latest.text) != _early_words(text):
            return None
        return judging.cache.completed(window)

    async def _early_judgement(self, text: str) -> TurnJudgement | None:
        judging = self._judging
        if judging is None:
            return None
        judgement = self._interim_judgement(text)
        if judgement is not None:
            return judgement
        window = self._early_window
        if window is None or _early_words(window.latest.text) != _early_words(text):
            session = self._session
            window = judging.windows.for_interim(
                text, twin_speaking=session is not None and session.is_speaking
            )
        judgement, _ = await capture_error(partial(judging.cache.judgement, window))
        return judgement

    async def _early_readiness(self, text: str) -> str | None:
        judging = self._judging
        if judging is None:
            return "no_judge"
        if self._closed or self._failure is not None or not text.strip():
            return "unavailable"
        judgement = await self._early_judgement(text)
        if judgement is None:
            return "judge_failed"
        if not judgement.finished:
            return "judge_unfinished"
        verdict = decision_of(judgement, judging.thresholds, named=self._addressed(text))
        if verdict.decision not in _ADMITTED:
            return "judge_not_admitted"
        options = self._early_options
        if options is None:
            return None
        probability = self._turn_end_probability
        if probability is None and options.prosody_expected and not self._voice_active:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._turn_end_scored.wait(), options.prosody_wait_seconds)
            probability = self._turn_end_probability
        self._turn_end_probability = None
        if probability is not None and probability < options.prosody_threshold:
            return "prosody_incomplete"
        return None

    def _duck(self) -> None:
        if self._ducked or self._closed or self._failure is not None or not self._barge_in:
            return
        session = self._session
        if session is None or not isinstance(session, SupportsDucking):
            return
        if not self._twin_holds_floor(session):
            return
        self._ducked = True
        session.duck()
        self._journal.record("barge_in_ducked", source="agent")
        self._duck_timer = asyncio.create_task(self._duck_grace())
        self._arm_freeze()

    def _arm_freeze(self) -> None:
        session = self._session
        if not isinstance(session, SupportsFreezing):
            return
        if self._freeze_timer is not None and not self._freeze_timer.done():
            return
        self._freeze_timer = asyncio.create_task(
            self._freeze_after_sustain(self._freeze_sustain_seconds)
        )

    async def _freeze_after_sustain(self, delay: float) -> None:
        await asyncio.sleep(delay)
        session = self._session
        if (
            not self._ducked
            or self._frozen
            or self._closed
            or self._failure is not None
            or session is None
            or not isinstance(session, SupportsFreezing)
            or not self._twin_holds_floor(session)
        ):
            return
        if not self._voice_active:
            if self._clock() - self._voice_last_at <= self._freeze_gap_seconds:
                self._freeze_timer = asyncio.create_task(self._freeze_after_sustain(0.02))
            return
        self._frozen = True
        session.freeze()
        self._journal.record("barge_in_frozen", source="agent")

    def _unduck(self, reason: str | None) -> None:
        if not self._ducked:
            return
        self._ducked = False
        timer, self._duck_timer = self._duck_timer, None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
        freeze_timer, self._freeze_timer = self._freeze_timer, None
        if (
            freeze_timer is not None
            and freeze_timer is not asyncio.current_task()
            and not freeze_timer.done()
        ):
            freeze_timer.cancel()
        session = self._session
        if self._frozen:
            self._frozen = False
            if session is not None and isinstance(session, SupportsFreezing):
                session.thaw()
        if session is not None and isinstance(session, SupportsDucking):
            session.unduck()
        if reason is not None:
            self._journal.record("barge_in_resumed", source="agent", reason=reason)

    async def _duck_grace(self) -> None:
        await asyncio.sleep(self._duck_grace_seconds)
        if self._ducked and self._voice_active:
            self._duck_timer = asyncio.create_task(self._duck_grace())
            return
        self._unduck("grace")

    def note_turn_end(self, *, probability: float) -> None:
        if self._voice_active:
            return
        self._turn_end_probability = probability
        self._turn_end_scored.set()

    def note_speech(self, *, active: bool) -> None:
        if active or self._voice_active:
            self._voice_last_at = self._clock()
        self._voice_active = active
        self._turn_end_probability = None
        self._turn_end_scored.clear()
        if active:
            self._duck()
            if self._ducked and not self._frozen:
                self._arm_freeze()
        if self._early_options is None:
            return
        if active:
            self._cancel_early_hold(declined="superseded")
            return
        self._arm_early_hold()

    def _arm_early_hold(self) -> None:
        if (
            self._voice_active
            or self._closed
            or self._failure is not None
            or not self._early_text.strip()
            or self._early_commit is not None
            or (self._early_timer is not None and not self._early_timer.done())
            or self._early_declined == (self._early_version, tuple(_early_words(self._early_text)))
        ):
            return
        self._early_window = None
        judging = self._judging
        if judging is not None:
            session = self._session
            window = judging.windows.for_interim(
                self._early_text,
                twin_speaking=session is not None and session.is_speaking,
            )
            judging.cache.prefetch(window)
            self._early_window = window
        self._early_timer = asyncio.create_task(
            self._after_early_hold(self._early_version, self._early_text)
        )

    def _extends_armed_text(self, words: list[str]) -> bool:
        window = self._early_window
        timer = self._early_timer
        if window is None or timer is None or timer.done():
            return False
        armed = _early_words(window.latest.text)
        return len(words) == len(armed) + 1 and words[: len(armed)] == armed

    async def note_interim(self, text: str) -> None:
        if self._early_options is None:
            return
        words = _early_words(text)
        changed = words != _early_words(self._early_text)
        extended = changed and self._extends_armed_text(words)
        self._early_text = text
        if changed and not extended:
            self._cancel_early_hold()
        commit = self._early_commit
        if commit is not None and not commit.resumed and says_more(text, commit.text):
            await self._cancel_early(commit, reason="speech_resumed")
        if changed and not extended:
            self._arm_early_hold()

    async def settle_early(self, final: TranscriptEvent) -> bool:
        if self._early_options is None:
            return False
        commit, self._early_commit = self._early_commit, None
        self._interim_window = None
        self._early_window = None
        words = len(_early_words(self._early_text))
        self._early_text = ""
        self._cancel_early_hold(declined="final_first", words=words)
        if commit is None:
            return False
        if (
            not commit.resumed
            and says_more(final.text, commit.text)
            and (len(_early_words(final.text)) - len(_early_words(commit.text)))
            >= _EXTRA_WORDS_FOR_MORE
        ):
            await self._cancel_early(commit, reason="final_says_more")
        if commit.resumed:
            return False
        self._journal.record(
            "early_turn_verified",
            source="agent",
            transcript_id=final.id,
            early_transcript_id=commit.event.id,
            early_words=len(_early_words(commit.text)),
            final_words=len(_early_words(final.text)),
        )
        return True

    def _cancel_early_hold(self, *, declined: str | None = None, words: int | None = None) -> None:
        self._early_version += 1
        self._early_window = None
        timer, self._early_timer = self._early_timer, None
        if timer is not None and not timer.done():
            timer.cancel()
            if declined is not None:
                self._journal.record(
                    "early_turn_declined",
                    source="agent",
                    reason=declined,
                    words=words if words is not None else len(_early_words(self._early_text)),
                )

    async def _after_early_hold(self, version: int, text: str) -> None:
        options = self._early_options
        if options is None:
            return
        await asyncio.sleep(options.hold_seconds)
        if version != self._early_version:
            return
        reason = await self._early_readiness(text)
        if reason is not None:
            if reason != "unavailable":
                self._early_declined = (version, tuple(_early_words(text)))
                self._journal.record(
                    "early_turn_declined",
                    source="agent",
                    reason=reason,
                    words=len(_early_words(text)),
                )
                if version == self._early_version and _early_words(
                    self._early_text
                ) != _early_words(text):
                    if self._early_timer is asyncio.current_task():
                        self._early_timer = None
                    self._arm_early_hold()
            return
        if version != self._early_version or self._early_commit is not None:
            return
        event = options.build_event(text)
        self._early_commit = _EarlyCommit(event, text)
        self._early_starting, self._early_timer = self._early_timer, None
        self._journal.record(
            "early_turn_started",
            source="agent",
            transcript_id=event.id,
            words=len(_early_words(text)),
            hold_ms=round(options.hold_seconds * 1000),
        )
        if self._early_answer is not None:
            await self._early_answer(event)
        else:
            await self.receive_final(event)

    async def _cancel_early(self, commit: _EarlyCommit, *, reason: str) -> None:
        commit.resumed = True
        self._journal.record(
            "early_turn_cancelled",
            source="agent",
            transcript_id=commit.event.id,
            reason=reason,
        )
        self._early_withdrawn.add(commit.event.id)
        self._queue.discard(commit.event.id)
        active = self._floor.active
        if active is not None and active.transcript_id == commit.event.id:
            await cancel_speech(self._floor, self._journal, reason=reason)

    async def handle_interim(self, text: str) -> None:
        if text.strip():
            self.note_activity()
        window: JudgeWindow | None = None
        if not self._closed and self._failure is None:
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
        if not words or not self._floor_contested(session, text):
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

    def _floor_contested(self, session: Any, text: str) -> bool:
        active = self._floor.active
        if not session.is_speaking and (active is None or active.task.done()):
            return False
        return not self._revises_active_turn(text)

    def _revises_active_turn(self, text: str) -> bool:
        last = self._last_final
        if last is None:
            return False
        _participant, previous, _admitted_at = last
        before = previous.split()
        after = _tokens(text)
        if not before or not after:
            return False
        if after == before:
            return True
        shorter, longer = sorted((before, after), key=len)
        return SequenceMatcher(None, shorter, longer[: len(shorter)]).ratio() >= _RESUMED_MATCH

    def _twin_holds_floor(self, session: Any) -> bool:
        active = self._floor.active
        return bool(session.is_speaking or (active is not None and not active.task.done()))

    def _ask_the_judge(self, text: str, words: int, window: JudgeWindow | None) -> None:
        generation = self._floor.generation
        self._barge_seq += 1
        task = asyncio.create_task(
            self._judge_the_barge_in(text, words, window, generation, self._barge_seq)
        )
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)

    async def _judge_the_barge_in(
        self, text: str, words: int, window: JudgeWindow | None, generation: int, sequence: int
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
            self._unduck(None)
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
            if sequence == self._barge_seq:
                self._unduck("judge")
            return
        await self._yield_the_floor(words, decided_by="judge")

    async def _yield_the_floor(self, words: int, *, decided_by: str | None) -> None:
        self._unduck(None)
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
            return is_vocative(text, self._wake_phrases or self._addressing_names)
        return any(mentions_whole_phrase(text, name) for name in self._addressing_names)

    def _others_speaking(self) -> bool:
        signal = self._others_speaking_signal
        return self._participant_policy and signal is not None and signal()

    def _open_final(self, event: TranscriptEvent) -> Self | None:
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
        if event.id in self._seen:
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
        return self

    async def _invoke_final(self, event: TranscriptEvent) -> TurnGraphState:
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
                self,
            )
        finally:
            self._in_flight = False

    async def _yield_floor(self, text: str, decision: ParticipationDecision) -> None:
        self._unduck(None)
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
        judging = self._judging
        window = (
            judging.windows.for_interim(text, twin_speaking=self.speaker.is_speaking)
            if judging is not None
            else None
        )
        self._interim_window = window
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
                voice_active=self._voice_active,
            ),
            self,
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
        self._actor.reset_duck()
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
