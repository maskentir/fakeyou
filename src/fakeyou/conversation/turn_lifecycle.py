import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Literal

from langgraph.graph.state import CompiledStateGraph

from fakeyou.conversation.jev_refresh import JevState
from fakeyou.conversation.session import ConversationError, MeetingAudio
from fakeyou.conversation.turn_events import JournalSink
from fakeyou.conversation.turn_graph import (
    BargeLatch,
    ConversationPhase,
    Responder,
    SentenceLimits,
    SpeechFloor,
    StreamTurnFactory,
    TurnGraphState,
    TurnInput,
    TurnRuntime,
    WholeResponder,
    WholeTurnFactory,
    build_speech_graph,
    cancel_speech,
    drain_turns,
)
from fakeyou.conversation.turn_queue import AdmittedFinals, PendingFinals
from fakeyou.conversation.turn_recovery import RecoveryOptions, TurnRecovery
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import ConsumerSink, guard_observer
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_exception

TransitionObserver = Callable[[ConversationPhase, ConversationPhase, str], None]
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


def public_phase(phase: ConversationPhase) -> PublicPhase:
    return _PUBLIC_OF[phase]


@dataclass(frozen=True, slots=True)
class ReplyOptions:
    whole: WholeResponder | None = None
    whole_turn: WholeTurnFactory | None = None
    stream_turn: StreamTurnFactory | None = None
    sentences: SentenceLimits | None = None


def reply_mode(*, sentence_streaming: bool) -> ReplyMode:
    return "streamed" if sentence_streaming else "whole_turn"


class _SessionSpeaker:
    def __init__(self, lifecycle: "TurnLifecycle") -> None:
        self._lifecycle = lifecycle

    @property
    def is_speaking(self) -> bool:
        session = self._lifecycle.session
        return session is not None and session.is_speaking

    async def speak(
        self,
        utterance: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        session = self._session()
        if sentence_index is None and transcript_id is None:
            await session.speak(utterance)
            return
        await session.speak(utterance, sentence_index=sentence_index, transcript_id=transcript_id)

    async def stop_playback(self) -> None:
        await self._session().stop_playback()

    def _session(self) -> MeetingAudio:
        session = self._lifecycle.session
        if session is None:
            raise ConversationError("meeting conversation has no audio session")
        return session


class TurnLifecycle:
    def __init__(
        self,
        graph: CompiledStateGraph[TurnGraphState, TurnRuntime, TurnInput, TurnGraphState],
        *,
        journal: JournalSink,
        responder: Responder | None = None,
        jev: JevState | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._graph = graph
        self._speech = build_speech_graph()
        self._barge_in = True
        self._shadow = False
        self._journal = journal
        self._responder = responder
        self._jev = jev
        self._options = RecoveryOptions()
        self._recovery = self._build_recovery()
        self._reply = ReplyOptions()
        self._observer: TransitionObserver | None = None
        self._spoken: Callable[[str], None] | None = None
        self._phase: ConversationPhase = "listening"
        self._floor = SpeechFloor(clock=clock)
        self._queue = PendingFinals()
        self._admitted = AdmittedFinals()
        self._barge = BargeLatch()
        self._events: list[TranscriptEvent] = []
        self._session: MeetingAudio | None = None
        self._drain: asyncio.Task[None] | None = None
        self._pending_requested = False
        self._closed = False
        self._paused = False
        self._failure: ConversationError | None = None
        self._runtime: TurnRuntime | None = self._build_runtime()

    @property
    def graph(self) -> CompiledStateGraph[TurnGraphState, TurnRuntime, TurnInput, TurnGraphState]:
        return self._graph

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
    def barge(self) -> BargeLatch:
        return self._barge

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return tuple(self._events)

    def note_final(self, event: TranscriptEvent) -> None:
        self._events.append(event)

    @property
    def recovery(self) -> TurnRecovery:
        return self._recovery

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

    def observe_spoken(self, observer: Callable[[str], None]) -> None:
        self._spoken = observer

    def _build_recovery(self) -> TurnRecovery:
        return TurnRecovery(
            journal=self._journal,
            faults=self._options.faults,
            language=self._options.language,
            is_closed=lambda: self._closed,
        )

    def _refresh_runtime(self) -> None:
        if not self._closed:
            self._runtime = self._build_runtime()

    def _build_runtime(self) -> TurnRuntime:
        return TurnRuntime(
            speaker=_SessionSpeaker(self),
            journal=self._journal,
            responder=self._responder,
            floor=self._floor,
            jev=self._jev,
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
        )

    def _note_spoken(self, sentence: str) -> None:
        if self._spoken is not None:
            self._spoken(sentence)

    @property
    def jev(self) -> JevState | None:
        return self._jev

    @property
    def barge_in(self) -> bool:
        return self._barge_in

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
        drain = self._drain
        return (drain is not None and not drain.done()) or self._floor.busy

    @property
    def accepts_ingress(self) -> bool:
        return not self._closed and not self._paused and self._failure is None

    def bind(self, session: MeetingAudio) -> None:
        if self._closed:
            raise ConversationError("meeting conversation is closed")
        if self._session is not None and self._session is not session:
            raise ConversationError("meeting conversation already owns an audio session")
        self._session = session
        self.start_pending()

    def request_pending(self) -> None:
        self._pending_requested = True

    def start_requested(self) -> None:
        pending, self._pending_requested = self._pending_requested, False
        if pending:
            self.start_pending()

    def start_pending(self) -> None:
        runtime = self._runtime
        if runtime is None or self._failure is not None or self._session is None:
            return
        if not self._queue.pending:
            return
        drain = self._drain
        if drain is None or drain.done():
            self._drain = asyncio.create_task(drain_turns(runtime, self._speech, self._queue))

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
        drain = self._drain
        if drain is not None:
            await asyncio.shield(drain)
        active = self._floor.active
        if active is not None:
            await asyncio.shield(active.task)
        self.raise_if_failed()

    async def drained(self) -> None:
        drain = self._drain
        if drain is not None:
            await asyncio.gather(drain, return_exceptions=True)

    async def pause(self) -> None:
        self._paused = True
        session = self._session
        if session is not None and session.is_speaking:
            await session.stop_playback()

    def resume(self) -> None:
        self._paused = False

    async def close(self) -> None:
        self._closed = True
        self._admitted.advance()
        self._queue.clear()
        if self._jev is not None:
            self._jev.cancel()
        self._session = None
        self._runtime = None
        await self._release_floor()
        drain = self._drain
        if drain is not None and not drain.done():
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        self._phase = "closed"

    async def _release_floor(self) -> None:
        active = self._floor.active
        if active is not None and not active.task.done():
            await cancel_speech(self._floor, self._journal, reason="close")
            return
        await self._floor.interrupt("close")
