import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from fakeyou.audio.audio import MeetingAudioError, SupportsKeywords
from fakeyou.audio.stream_capture import (
    CaptureDrainLoop,
    DrainPolling,
    StreamRegistry,
    SupportsCaptureRegistry,
    load_capture_script,
)
from fakeyou.audio.voice_activity import (
    VoiceActivityLoop,
    VoiceActivityTracker,
    load_voice_activity_script,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.conversation.moments import MomentDetector
from fakeyou.conversation.session import (
    ConversationError,
    MeetingAudio,
    MeetingConversation,
    SessionConfig,
    SessionResult,
    SupportsAdmission,
    SupportsMaintenance,
)
from fakeyou.conversation.state import (
    TERMINAL_PHASES,
    LifecycleState,
    PublicPhase,
    RuntimeContext,
)
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import require_url
from fakeyou.foundation.invariants import (
    AGENT_MIC_SOURCE_NAME,
    LEAVE_BUDGET_SECONDS,
    MEETING_SINK_NAME,
)
from fakeyou.foundation.taxonomy import TerminalReason
from fakeyou.foundation.transcript import AttentionContext, TranscriptEvent, attention_line
from fakeyou.meet.browser import BrowserLaunch, open_direct_page, stable_fingerprint_seed
from fakeyou.meet.meet import (
    DEFAULT_MEET_SIGNALS,
    GuestJoinRequest,
    JoinPolling,
    JoinResult,
    JoinStatus,
    MeetSignals,
    disable_meeting_camera,
    enable_meeting_microphone,
    leave_meeting,
    observe_call_state,
    request_guest_join,
)
from fakeyou.meet.participant_observer import (
    DEFAULT_PARTICIPANT_SCHEDULE,
    QUERY_METHOD_MINING,
    ParticipantEvidenceWriter,
    ParticipantObserver,
    observer_epoch,
)
from fakeyou.meet.watchdog import UIWatchdog, WatchdogCheck, dismiss_known_errors
from fakeyou.observability.cleanup import TeardownStack, close_safely
from fakeyou.observability.faults import (
    ConsumerSink,
    FaultReporter,
    bounded_traceback,
    capture_error,
    guard_consumer,
    until_stopped,
)
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import (
    ProviderFault,
    classify_exception,
    trim_detail,
)
from fakeyou.orchestration.commands import CommandRunner
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.orchestration.lifecycle import (
    CheckpointDegradation,
    DegradingCheckpointer,
    JoinAttempt,
    JoinStep,
    LifecycleAttribution,
    LifecycleEventKind,
    LifecycleThread,
    LifecycleUpdate,
    apply_transition,
    build_lifecycle_graph,
    initial_lifecycle_state,
    initialize_lifecycle,
    join_retry_policy,
    meeting_thread_id,
    resume_lifecycle,
)
from fakeyou.orchestration.perception import PerceptionStack
from fakeyou.orchestration.preflight import ProbeResult, run_preflight
from fakeyou.orchestration.presence import PROTOCOL_STALE_SECONDS, AloneClock
from fakeyou.perception.attention import AttentionPrefilter
from fakeyou.perception.percepts import ChatLine
from fakeyou.perception.roster import MeetRoster
from fakeyou.perception.sensors import (
    DEFAULT_PERCEPTION_SCHEDULE,
    PerceptionDrain,
    load_perception_script,
)
from fakeyou.perception.timeline import ChatReceived, TimelineEvent


@asynccontextmanager
async def session_resources(faults: FaultReporter | None) -> AsyncIterator[TeardownStack]:
    stack = TeardownStack(faults=faults)
    try:
        yield stack
    finally:
        await stack.aclose()


def _fault_report(faults: FaultReporter | None) -> dict[str, Any] | None:
    return faults.report() if faults is not None else None


def _failure_detail(error: BaseException) -> str:
    return trim_detail(str(error)) or type(error).__name__


def _fault_suffix(faults: FaultReporter | None) -> str:
    reported = faults.report() if faults is not None else None
    if not reported:
        return ""
    top = reported["faults"]["kinds"][0]
    return f" [{top['source']}_degraded: {top['kind']}x{top['count']}]"


_JOIN_EVENTS: dict[JoinStatus, tuple[LifecycleEventKind, LifecycleAttribution]] = {
    "admitted": ("admitted", "host_action"),
    "denied": ("denied", "host_action"),
    "blocked": ("blocked", "system_fault"),
    "timeout": ("lobby_timed_out", "system_fault"),
    "aborted": ("owner_stopped", "owner_action"),
    "failed": ("browser_failed", "system_fault"),
}


def _terminal_result(
    phase: PublicPhase,
    terminal_reason: TerminalReason | None,
    detail: str,
    faults: FaultReporter | None,
) -> SessionResult:
    return SessionResult(
        phase, terminal_reason, detail + _fault_suffix(faults), _fault_report(faults)
    )


_STOPPED_BEFORE_JOIN = "owner stopped before guest join"
_STOPPED_BEFORE_RETRY = "owner stopped before join retry"
OBSERVATION_GRACE_SECONDS = 60.0
DOM_PRESENCE_FRESH_SECONDS = 30.0
KNOWN_ERROR_DISMISS_SECONDS = 5.0


@dataclass(slots=True)
class MonitorScan:
    next_scan: float
    remote_participants: int | None
    presence_at: float
    failing_since: float | None = None
    hard_failures: int = 0


_LIVE_PHASES: tuple[PublicPhase, ...] = ("admitted", "active")


@dataclass
class MeetingWorker:
    config: SessionConfig
    page: Page
    stop: asyncio.Event
    signals: MeetSignals
    sleep: Callable[[float], Awaitable[None]]
    monotonic: Callable[[], float]
    open_audio: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None = None
    initial_speech: str | None = None
    conversation: MeetingConversation | None = None
    audio: MeetingAudio | None = None
    faults: FaultReporter | None = None
    journal: Journal | None = None
    observer: ParticipantObserver | None = None
    voice_activity: VoiceActivityTracker | None = None
    voice_loop: VoiceActivityLoop | None = None
    capture_drain: CaptureDrainLoop | None = None
    capture_registry: StreamRegistry | None = None
    perception: PerceptionStack | None = None
    perception_drain: PerceptionDrain | None = None
    perception_sync_watched: bool = False
    on_phase: Callable[[str], None] | None = None
    moments: MomentDetector | None = None
    commands: CommandRunner | None = None
    kernel: KernelPort | None = None
    detail: str = ""
    base: LifecycleState | None = None
    working: LifecycleState | None = None
    admitted_at: float | None = None
    disconnected_from: PublicPhase | None = None
    origin_kind: LifecycleEventKind | None = None
    origin_attribution: LifecycleAttribution | None = None
    admission: JoinResult | None = None
    join_started: float | None = None
    presence: AloneClock = field(init=False)
    attention: AttentionPrefilter = field(init=False)
    attention_screen: str | None = field(init=False, default=None)
    attention_chat: str | None = field(init=False, default=None)
    _events_watched: bool = False

    def __post_init__(self) -> None:
        self.presence = AloneClock(leave_after_seconds=self.config.alone_leave_seconds)
        self.attention = AttentionPrefilter(max_per_window=self.config.attention_max_interjections)
        self.begin(
            initial_lifecycle_state(
                self.config.owner_id, self.config.twin_id, self.config.meeting_id
            )
        )

    def begin(self, state: LifecycleState) -> None:
        self.base = state
        self.working = cast(
            LifecycleState,
            {**state, "external_effects": list(state["external_effects"])},
        )

    def _state(self) -> LifecycleState:
        if self.working is None:
            raise RuntimeError("lifecycle step ran without a begin")
        return self.working

    def phase_now(self) -> PublicPhase:
        return self._state()["phase"]

    def _commit(self) -> LifecycleUpdate:
        if self.base is None:
            raise RuntimeError("lifecycle step committed without a begin")
        before = self.base
        after = self._state()
        update: LifecycleUpdate = {}
        if after["phase"] != before["phase"]:
            update["phase"] = after["phase"]
        if after["terminal_reason"] != before["terminal_reason"]:
            update["terminal_reason"] = after["terminal_reason"]
        if after["external_effects"] != before["external_effects"]:
            update["external_effects"] = after["external_effects"]
        return update

    def _send(
        self, kind: LifecycleEventKind, attribution: LifecycleAttribution, detail: str
    ) -> None:
        state = self._state()
        update = apply_transition(state, kind, attribution, previous=self.disconnected_from)
        if update:
            self.working = cast(LifecycleState, {**state, **update})
            if self.on_phase is not None and self.working["phase"] != state["phase"]:
                self.on_phase(self.working["phase"])
        self.detail = detail

    def _join_request(self) -> GuestJoinRequest:
        return GuestJoinRequest(
            link=self.config.link,
            twin_name=self.config.twin_name,
            lobby_timeout_seconds=self.config.lobby_timeout_seconds,
            poll_interval_seconds=self.config.join_poll_interval_seconds,
        )

    def _probe_scripts(self) -> tuple[str, ...]:
        return (
            ((load_voice_activity_script(),) if self.voice_activity is not None else ())
            + ((load_capture_script(),) if self.config.per_stream_enabled else ())
            + ((load_perception_script(),) if self.perception is not None else ())
        )

    async def prepare(self, state: LifecycleState) -> LifecycleUpdate:
        self.begin(state)
        self._send("prepared", "owner_action", "meeting session preparing")
        return self._commit()

    async def join(self, state: LifecycleState, attempt: JoinAttempt) -> JoinStep:
        self.begin(state)
        if attempt.number == 1:
            self.join_started = self.monotonic()
            if self.stop.is_set():
                self._send("owner_stopped", "owner_action", _STOPPED_BEFORE_JOIN)
                stopped = JoinResult(status="aborted", detail=_STOPPED_BEFORE_JOIN)
                return JoinStep(stopped, self._commit())
        self._send("join_requested", "owner_action", "worker requested guest join")
        if self.stop.is_set():
            stopped = JoinResult(status="aborted", detail=_STOPPED_BEFORE_RETRY)
            return self._settle_join(stopped, attempts=attempt.number - 1)
        if attempt.number == 1:
            self.record("join_requested", source="runtime")
        self.watch_perception_sync()
        result = await self._attempt_guest_join()
        if attempt.retries(result.status):
            if not self.stop.is_set():
                return JoinStep(result, {})
            result = JoinResult(status="aborted", detail=_STOPPED_BEFORE_RETRY)
        return self._settle_join(result, attempts=attempt.number)

    async def _attempt_guest_join(self) -> JoinResult:
        outcome = await until_stopped(
            request_guest_join(
                self.page,
                self._join_request(),
                self.signals,
                JoinPolling(self.sleep, self.monotonic, self.stop.is_set),
                self._probe_scripts(),
            ),
            self.stop,
        )
        if outcome is None:
            return JoinResult(status="aborted", detail="owner stopped guest join")
        return outcome

    def _settle_join(self, result: JoinResult, *, attempts: int) -> JoinStep:
        started = self.join_started if self.join_started is not None else self.monotonic()
        self.record(
            "join_finished",
            source="runtime",
            status=result.status,
            detail=result.detail,
            duration_ms=(self.monotonic() - started) * 1000,
            audio_ready=self.audio is not None,
            attempts=attempts,
        )
        if result.status == "admitted":
            self.admission = result
        else:
            kind, attribution = _JOIN_EVENTS[result.status]
            self._send(kind, attribution, result.detail)
        return JoinStep(result, self._commit())

    async def await_admission(self, state: LifecycleState) -> LifecycleUpdate:
        self.begin(state)
        if self.admission is not None:
            self._send("admitted", "host_action", self.admission.detail)
        return self._commit()

    async def activate(self, state: LifecycleState) -> LifecycleUpdate:
        self.begin(state)
        self.admitted_at = self.monotonic()
        if self.perception is not None:
            self.perception.sensors.mark_admitted(datetime.now(UTC))
        if self.audio is None:
            await self.disable_camera_best_effort()
            self._send("activated", "policy", "guest admitted to the call")
            return self._commit()
        try:
            return await self._activate_audio(self.audio)
        except ConversationError as error:
            return await self._finish_audio_failure("conversation_failed", _failure_detail(error))
        except MeetingAudioError as error:
            return await self._finish_audio_failure("audio_failed", _failure_detail(error))

    async def _finish_audio_failure(self, kind: LifecycleEventKind, detail: str) -> LifecycleUpdate:
        audio = self.audio
        self.audio = None
        if audio is not None:
            await close_safely(audio.stop, source="audio", faults=self.faults)
        return await self._finish_commit(kind, "system_fault", detail)

    async def _activate_audio(self, audio: MeetingAudio) -> LifecycleUpdate:
        audio.raise_if_failed()
        if isinstance(audio, SupportsAdmission):
            audio.mark_admitted()
        await self.dismiss_known_errors_best_effort()
        await self.disable_camera_best_effort()
        try:
            enabled = await until_stopped(
                enable_meeting_microphone(self.page, self.sleep), self.stop
            )
        except RuntimeError as error:
            raise MeetingAudioError("meeting microphone could not be enabled") from error
        if enabled is None or self.stop.is_set():
            return await self._finish_commit(
                "owner_stopped", "owner_action", "owner stopped microphone startup"
            )
        self.record("microphone_enabled", source="runtime")
        audio.raise_if_failed()
        await self._start_capture_guarded(enabled=self.config.per_stream_enabled)
        if self.initial_speech:
            await until_stopped(audio.speak(self.initial_speech), self.stop)
        if self.stop.is_set():
            await self.close_capture_drain()
            return await self._finish_commit(
                "owner_stopped", "owner_action", "owner stopped conversation startup"
            )
        if self.conversation is not None:
            try:
                self.conversation.bind(audio)
            except ConversationError as error:
                await self.disable_conversation(error)
            else:
                self.record("conversation_bound", source="runtime")
        self._send("activated", "policy", "guest admitted and audio live")
        return self._commit()

    async def _finish_commit(
        self, kind: LifecycleEventKind, attribution: LifecycleAttribution, detail: str
    ) -> LifecycleUpdate:
        await self.finish(kind, attribution, detail)
        return self._commit()

    async def monitor(self, state: LifecycleState) -> LifecycleUpdate:
        self.begin(state)
        admitted_at = self.admitted_at if self.admitted_at is not None else self.monotonic()
        try:
            await self.run_monitor(admitted_at)
        except ConversationError as error:
            return await self._finish_audio_failure("conversation_failed", _failure_detail(error))
        except MeetingAudioError as error:
            return await self._finish_audio_failure("audio_failed", _failure_detail(error))
        return self._commit()

    async def leave(self, state: LifecycleState) -> LifecycleUpdate:
        self.begin(state)
        if self.phase_now() != "leaving":
            return self._commit()
        origin_detail = self.detail
        if self.commands is not None and self.commands.left:
            detail = "twin already left the call on request"
            self._send("leave_sent", self.origin_attribution or "policy", detail)
            keeps_origin = self.origin_kind in ("audio_failed", "conversation_failed")
            self.detail = origin_detail if keeps_origin else detail
            return self._commit()
        try:
            async with asyncio.timeout(
                min(LEAVE_BUDGET_SECONDS, self.config.leave_timeout_seconds)
            ):
                left = await leave_meeting(
                    self.page,
                    self.signals,
                    self.sleep,
                    self.monotonic,
                    self.config.leave_timeout_seconds,
                )
        except (PlaywrightError, TimeoutError) as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation="leave"))
            self._send("browser_failed", "system_fault", "Meeting leave cleanup failed")
            self.detail = origin_detail
            return self._commit()
        if left.status == "left":
            self._send("leave_sent", self.origin_attribution or "policy", left.detail)
        else:
            self._send("browser_failed", "system_fault", left.detail)
        keeps_origin = self.origin_kind in ("audio_failed", "conversation_failed")
        self.detail = origin_detail if keeps_origin else left.detail
        return self._commit()

    async def dismiss_known_errors_best_effort(self) -> None:
        try:
            async with asyncio.timeout(KNOWN_ERROR_DISMISS_SECONDS):
                dismissed = await dismiss_known_errors(self.page)
        except (PlaywrightError, TimeoutError, TypeError, RuntimeError) as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation="dismiss_modal"))
            return
        for text in dismissed:
            self.record("modal_dismissed", source="browser", detail=text)

    async def disable_camera_best_effort(self) -> None:
        if self.stop.is_set():
            return
        try:
            await until_stopped(disable_meeting_camera(self.page, self.sleep), self.stop)
        except RuntimeError as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation="camera"))

    async def _start_capture_guarded(self, *, enabled: bool) -> None:
        try:
            await self.start_capture_drain(enabled=enabled)
        except RuntimeError as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation="stream_capture"))

    async def start_participant_observer(self) -> None:
        if self.observer is not None:
            return
        if not self.config.participant_policy_enabled:
            return
        evidence_path = self.config.participant_evidence_path
        if evidence_path is None:
            return
        epoch = observer_epoch(self.config.meeting_id)
        faults = self.faults
        writer = ParticipantEvidenceWriter(
            evidence_path,
            epoch=epoch,
            method=QUERY_METHOD_MINING,
            max_bytes=self.config.participant_evidence_max_bytes,
            faults=faults,
        )

        def persist(raw: Any, mono_ms: float) -> None:
            try:
                writer.write(raw, mono_ms)
            except OSError as error:
                if faults is not None:
                    faults.record(
                        classify_exception("browser", error, operation="participant_evidence")
                    )

        self.observer = ParticipantObserver(
            self.page,
            on_poll=persist,
            schedule=replace(DEFAULT_PARTICIPANT_SCHEDULE, monotonic=self.monotonic),
            sinks=ConsumerSink(faults=self.faults, journal=self.journal),
        )
        await self.observer.start()
        self.record(
            "participant_observer_started",
            source="runtime",
            method=QUERY_METHOD_MINING,
            epoch=epoch,
        )

    async def close_participant_observer(self) -> None:
        if self.observer is not None:
            await close_safely(self.observer.close, source="browser", faults=self.faults)
            self.record(
                "participant_observer_stopped",
                source="runtime",
                polls=self.observer.polls,
                poll_failures=self.observer.poll_failures,
                last_poll_error=self.observer.last_poll_error,
            )
            self.observer = None

    async def start_voice_activity(self) -> None:
        if self.voice_loop is not None or self.voice_activity is None:
            return
        self.voice_loop = VoiceActivityLoop(
            self.page,
            self.voice_activity,
            sinks=ConsumerSink(faults=self.faults, journal=self.journal),
        )
        await self.voice_loop.start()
        self.record("voice_activity_started", source="runtime")

    async def close_voice_activity(self) -> None:
        if self.voice_loop is not None:
            await close_safely(self.voice_loop.close, source="browser", faults=self.faults)
            self.record(
                "voice_activity_stopped",
                source="runtime",
                polls=self.voice_loop.polls,
                poll_failures=self.voice_loop.poll_failures,
                last_poll_error=self.voice_loop.last_poll_error,
            )
            self.voice_loop = None

    async def start_capture_drain(self, *, enabled: bool = False) -> None:
        if not enabled or self.capture_drain is not None:
            return
        if isinstance(self.audio, SupportsCaptureRegistry):
            self.capture_registry = self.audio.capture_registry
        else:
            self.capture_registry = StreamRegistry()
        self.capture_drain = CaptureDrainLoop(
            self.page,
            self.capture_registry,
            polling=DrainPolling(
                max_consecutive_failures=self.config.drain_max_consecutive_failures,
                failure_journal_every=self.config.drain_failure_journal_every,
            ),
            sinks=ConsumerSink(faults=self.faults, journal=self.journal),
            twin_name=self.config.twin_name,
        )
        await self.capture_drain.start()
        self.record("stream_capture_started", source="runtime")

    async def close_capture_drain(self) -> None:
        if self.capture_drain is not None:
            await close_safely(self.capture_drain.close, source="browser", faults=self.faults)
            self.capture_drain = None
        self.capture_registry = None

    def watch_perception_sync(self) -> None:
        if self.perception is None or self.perception_sync_watched:
            return
        self.page.on("response", self.perception.sensors.on_response)
        self.perception_sync_watched = True

    def attention_trigger(self) -> AttentionContext | None:
        perception = self.perception
        if perception is None:
            return None
        snapshot = perception.timeline.snapshot()
        screen = snapshot.current_screen
        if screen is None or screen.reading is None:
            return None
        reading = screen.reading
        key = f"{screen.ref}:{reading.title}"
        if key == self.attention_screen:
            return None
        self.attention_screen = key
        title = reading.title or "a slide"
        points = reading.key_points if reading.is_meeting_content else []
        detail = "; ".join(point for point in points[:2])
        seen = f"{title}: {detail}" if detail else title
        return AttentionContext(
            kind="screen",
            trigger_text=f"The shared screen is now {seen}.",
            trigger_event_id=screen.ref,
        )

    def _deliver_attention(self, context: AttentionContext, *, source: str) -> bool:
        conversation = self.conversation
        if conversation is None or not self.config.attention_enabled:
            return False
        if conversation.is_busy:
            return False
        now = self.monotonic()
        if not self.attention.allow(now):
            return False
        self.attention.take(now)
        self.record(
            "attention_observed",
            source=source,
            chars=len(context.trigger_text),
            kind=context.kind,
        )
        conversation.receive_attention(
            TranscriptEvent(
                id=f"attention-{now:.3f}",
                timestamp=datetime.now(UTC).isoformat(),
                source="meeting_attention",
                text=attention_line(context),
                finalized=True,
                participant_id="unknown",
                speaker_evidence_id=None,
                attention=context,
            ),
            channel="voice",
        )
        return True

    async def offer_attention(self) -> None:
        conversation = self.conversation
        if conversation is None or not self.config.attention_enabled:
            return
        if conversation.is_busy:
            return
        now = self.monotonic()
        if not self.attention.allow(now):
            return
        if self.moments is not None:
            due = self.moments.take_ready(now=now)
            if due is not None:
                self._deliver_attention(due, source="agent")
                return
        trigger = self.attention_trigger()
        if trigger is None:
            return
        self._deliver_attention(trigger, source="perception")

    def watch_meeting_events(self) -> None:
        perception = self.perception
        if perception is None or self._events_watched:
            return
        perception.timeline.subscribe(self._on_timeline_event)
        self._events_watched = True

    def _on_timeline_event(self, event: TimelineEvent) -> None:
        if isinstance(event, ChatReceived):
            self._on_chat_line(event.line)

    def _on_chat_line(self, line: ChatLine) -> None:
        if self.kernel is not None:
            self.kernel.chat(
                message_id=line.ref,
                device_id=line.sender.participant_id,
                text=line.text,
                own=line.own,
                backlog=line.backlog,
            )
        conversation = self.conversation
        if conversation is None or line.own or line.backlog:
            return
        self.record(
            "chat_line_observed",
            source="perception",
            ref=line.ref,
            chars=len(line.text),
        )
        conversation.receive_chat(
            TranscriptEvent(
                id=line.ref,
                timestamp=line.sent_at.isoformat(),
                source="meeting_chat",
                text=line.text,
                finalized=True,
                participant_id=line.sender.participant_id,
                speaker_evidence_id=None,
                display_claim=line.sender.name or None,
            )
        )

    def watch_roster_keywords(self) -> None:
        perception = self.perception
        if perception is None:
            return
        sensors = perception.sensors
        shared = sensors.roster_listener

        def listener(roster: MeetRoster) -> None:
            if shared is not None:
                shared(roster)
            self.bias_transcriber(roster)

        sensors.roster_listener = listener

    def bias_transcriber(self, roster: MeetRoster) -> None:
        audio = self.audio
        if audio is None or not isinstance(audio, SupportsKeywords):
            return
        names = [name for name in roster.people().values() if name]
        if audio.update_keywords(names):
            self.record("stt_keywords_recorded", source="audio", keywords=len(names))

    async def start_perception(self) -> None:
        if self.perception is None or self.perception_drain is not None:
            return
        self.watch_perception_sync()
        self.watch_roster_keywords()
        self.watch_meeting_events()
        drain = PerceptionDrain(
            self.page,
            self.perception.sensors,
            schedule=replace(DEFAULT_PERCEPTION_SCHEDULE, monotonic=self.monotonic),
            sinks=ConsumerSink(faults=self.faults, journal=self.journal),
        )
        self.perception_drain = drain
        await drain.start()
        self.record("perception_drain_started", source="runtime")

    async def close_perception(self) -> None:
        drain = self.perception_drain
        if drain is not None:
            self.perception_drain = None
            await close_safely(drain.close, source="browser", faults=self.faults)
            self.record(
                "perception_drain_stopped",
                source="runtime",
                polls=drain.polls,
                poll_failures=drain.poll_failures,
                last_poll_error=drain.last_poll_error,
                frames=drain.sensors.frames_seen,
                rejected=drain.sensors.rejected_messages,
            )
        if self.perception is not None:
            if self.perception_sync_watched:
                self.page.remove_listener("response", self.perception.sensors.on_response)
                self.perception_sync_watched = False
            await close_safely(self.perception.close, source="browser", faults=self.faults)

    async def _start_guarded(self, starter: Callable[[], Awaitable[None]], operation: str) -> None:
        try:
            await starter()
        except RuntimeError as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation=operation))

    async def run_monitor(self, admitted_at: float) -> str:
        try:
            await self._start_guarded(self.start_participant_observer, "participant_observer")
            await self._start_capture_guarded(enabled=self.config.per_stream_enabled)
            await self._start_guarded(self.start_voice_activity, "voice_activity")
            await self._start_guarded(self.start_perception, "perception_drain")
            return await self._monitor_loop(admitted_at)
        finally:
            await self.close_perception()
            await self.close_voice_activity()
            await self.close_capture_drain()
            await self.close_participant_observer()

    async def _poll_stop(self, deadline: float) -> str | None:
        if self.commands is not None and self.commands.left:
            return await self.finish("leave_requested", "policy", "twin left the call on request")
        if self.stop.is_set():
            return await self.finish("owner_stopped", "owner_action", "owner requested stop")
        if self.monotonic() >= deadline:
            return await self.finish("duration_reached", "policy", "session duration reached")
        return None

    async def _service_conversation(self) -> None:
        if self.conversation is not None:
            try:
                self.conversation.raise_if_failed()
            except ConversationError as error:
                await self.disable_conversation(error)

    async def _service_audio(self, remote_participants: int | None) -> None:
        if self.capture_drain is not None:
            self.capture_drain.raise_if_failed()
        if self.audio is None:
            return
        self.audio.raise_if_failed()
        if not isinstance(self.audio, SupportsMaintenance):
            return
        await until_stopped(
            self.audio.maintenance_tick(
                is_busy=self.conversation is not None and self.conversation.is_busy,
                remote_participants=remote_participants,
            ),
            self.stop,
        )

    async def check_presence(self, scan: MonitorScan) -> tuple[int | None, str | None]:
        now = self.monotonic()
        dom = (
            scan.remote_participants
            if now - scan.presence_at <= DOM_PRESENCE_FRESH_SECONDS
            else None
        )
        perception = self.perception
        sensors = perception.sensors if perception is not None else None
        if self.kernel is not None:
            self.kernel.dom_remote(dom)
            self.kernel.protocol_health(available=self._protocol_available())
        was_drifting = self.presence.drifting
        reading = self.presence.observe(
            now,
            state=sensors.roster.state() if sensors is not None else None,
            heartbeats=sensors.heartbeats if sensors is not None else 0,
            dom_remote=dom,
        )
        if reading.drift and not was_drifting:
            self.record(
                "presence_drift_observed",
                source="runtime",
                protocol=sensors.roster.state().remote_count if sensors is not None else None,
                dom=dom,
            )
        if not self.presence.due(now):
            return reading.remote, None
        finished = await self.finish(
            "participants_left",
            "policy",
            f"No remote participants for the presence window ({reading.source})",
        )
        return reading.remote, finished

    def _note_slow_poll(self, poll_seconds: float) -> None:
        if poll_seconds >= 1.0 and self.journal is not None:
            self.journal.record("monitor_polled", source="runtime", duration_ms=poll_seconds * 1000)

    def _protocol_available(self) -> bool:
        perception = self.perception
        sensors = perception.sensors if perception is not None else None
        if sensors is None or sensors.heartbeats == 0:
            return False
        heartbeat = self.presence.last_heartbeat
        if heartbeat is None:
            return False
        return self.monotonic() - heartbeat <= PROTOCOL_STALE_SECONDS

    async def track_observation(self, scan: MonitorScan, checked: WatchdogCheck) -> str | None:
        if not checked.observation_failed:
            scan.failing_since = None
            scan.hard_failures = 0
            return None
        cause = next(
            (event.detail for event in checked.events if event.kind == "watchdog_failed"),
            "unknown",
        )
        now = self.monotonic()
        if scan.failing_since is None:
            scan.failing_since = now
        if cause != "TimeoutError":
            scan.hard_failures += 1
        failing_seconds = now - scan.failing_since
        if scan.hard_failures < 2 and failing_seconds < OBSERVATION_GRACE_SECONDS:
            return None
        self.record(
            "ui_observation_expired",
            source="browser",
            failing_ms=round(failing_seconds * 1000),
            detail=cause,
        )
        if self._protocol_available():
            return None
        return await self.finish(
            "browser_failed",
            "system_fault",
            f"Meeting UI observation failed for {failing_seconds:.0f}s: {cause}",
        )

    async def _record_scan_events(
        self, scan: MonitorScan, checked: WatchdogCheck, now: float
    ) -> str | None:
        scan.next_scan = now + checked.next_check_seconds * self.config.monitor_interval_seconds / 5
        scan.remote_participants = (
            max(0, checked.participant_count - 1) if checked.participant_count is not None else None
        )
        scan.presence_at = self.monotonic()
        for event in checked.events:
            self.record(
                event.kind,
                source="browser",
                fingerprint=event.fingerprint,
                detail=event.detail,
                ttl_ms=None if event.ttl_seconds is None else event.ttl_seconds * 1000,
            )
            if event.kind == "manual_action_expired":
                return await self.finish(
                    "browser_failed", "system_fault", "Meeting UI needs manual intervention"
                )
        return None

    async def apply_observation(self, checked: WatchdogCheck) -> str | None:
        observation = checked.observation
        if self.kernel is not None and observation.state != "unknown":
            self.kernel.call_state(
                observation.state,
                new_epoch=observation.state == "in_call" and self.disconnected_from is not None,
            )
        if observation.state in ("removed", "ended"):
            kind: LifecycleEventKind = (
                "removed" if observation.state == "removed" else "meeting_ended"
            )
            self._send(kind, "host_action", observation.detail)
            return observation.detail
        phase = self.phase_now()
        if observation.state == "lobby" and phase in _LIVE_PHASES:
            self.disconnected_from = phase
            self._send("connection_lost", "system_fault", observation.detail)
            if self.conversation is not None:
                await self.conversation.pause()
        elif (
            observation.state == "in_call"
            and phase == "joining"
            and self.disconnected_from is not None
        ):
            self._send("reconnected", "system_fault", observation.detail)
            self.disconnected_from = None
            if self.conversation is not None:
                self.conversation.resume()
        return None

    async def _scan_once(
        self, watchdog: UIWatchdog, scan: MonitorScan, now: float
    ) -> tuple[str | None, bool]:
        poll_started = self.monotonic()
        checked = await until_stopped(watchdog.check(), self.stop)
        if checked is None:
            return None, True
        self._note_slow_poll(self.monotonic() - poll_started)
        finished = await self._record_scan_events(scan, checked, now)
        if finished is not None:
            return finished, False
        finished = await self.track_observation(scan, checked)
        if finished is not None:
            return finished, False
        return await self.apply_observation(checked), False

    async def _monitor_loop(self, admitted_at: float) -> str:
        deadline = admitted_at + self.config.session_duration_seconds
        watchdog = UIWatchdog(
            self.page, signals=self.signals, observe=observe_call_state, monotonic=self.monotonic
        )
        scan = MonitorScan(
            next_scan=admitted_at,
            remote_participants=None,
            presence_at=admitted_at,
        )
        while True:
            finished = await self._poll_stop(deadline)
            if finished is not None:
                return finished
            await self._service_conversation()
            await self.offer_attention()
            remote, finished = await self.check_presence(scan)
            if finished is not None:
                return finished
            await self._service_audio(remote)
            now = self.monotonic()
            if now >= scan.next_scan:
                finished, recheck = await self._scan_once(watchdog, scan, now)
                if finished is not None:
                    return finished
                if recheck:
                    continue
            await self.wait(
                min(1.0, scan.next_scan - self.monotonic(), deadline - self.monotonic())
            )

    async def disable_conversation(self, error: ConversationError) -> None:
        if self.faults is not None and error.__cause__ is None:
            self.faults.record(classify_exception("agent", error))
        if self.conversation is not None:
            await close_safely(self.conversation.close, source="agent", faults=self.faults)
            self.conversation = None
        self.record(
            "conversation_disabled",
            source="runtime",
            detail="Listening continues without agent replies",
        )

    def record(self, event: str, **fields: Any) -> None:
        if self.journal is not None:
            self.journal.record(event, **fields)

    async def wait(self, seconds: float) -> None:
        async def delay() -> None:
            await self.sleep(max(0, seconds))

        await until_stopped(delay(), self.stop)

    async def finish(
        self, kind: LifecycleEventKind, attribution: LifecycleAttribution, detail: str
    ) -> str:
        self.origin_kind = kind
        self.origin_attribution = attribution
        self._send(kind, attribution, detail)
        await self.close_perception()
        await self.close_participant_observer()
        if self.conversation is not None:
            await close_safely(self.conversation.close, source="agent", faults=self.faults)
        if self.audio is not None:
            await close_safely(self.audio.stop, source="audio", faults=self.faults)
        return detail


@dataclass(slots=True)
class SessionWiring:
    stop: asyncio.Event | None = None
    signals: MeetSignals = DEFAULT_MEET_SIGNALS
    open_page: Callable[..., AbstractAsyncContextManager[Page]] = open_direct_page
    saver_factory: Callable[[], AbstractAsyncContextManager[BaseCheckpointSaver[Any]]] | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    open_audio: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None = None
    initial_speech: str | None = None
    conversation: MeetingConversation | None = None
    faults: FaultReporter | None = None
    journal: Journal | None = None
    preflight: Callable[[], Coroutine[Any, Any, list[ProbeResult]]] | None = None
    preflight_hosts: tuple[str, ...] | None = None
    preflight_budget_seconds: float | None = None
    voice_activity: VoiceActivityTracker | None = None
    commands: CommandRunner | None = None
    perception: PerceptionStack | None = None
    on_phase: Callable[[str], None] | None = None
    moments: MomentDetector | None = None
    kernel: KernelPort | None = None


_PreflightFactory = Callable[[], Coroutine[Any, Any, list[ProbeResult]]]


def _validate_session_audio(hooks: SessionWiring) -> None:
    if hooks.initial_speech is not None and hooks.open_audio is None:
        raise ValueError("initial speech requires meeting audio")
    if hooks.conversation is not None and hooks.open_audio is None:
        raise ValueError("conversation requires meeting audio")


def _resolve_journal(hooks: SessionWiring, config: SessionConfig) -> Journal:
    if hooks.journal is not None:
        return hooks.journal
    if hooks.faults is not None:
        return hooks.faults.journal
    return Journal(config.meeting_id)


def _resolve_preflight(
    preflight: _PreflightFactory | None, hosts: tuple[str, ...] | None, budget: float
) -> _PreflightFactory | None:
    if preflight is None and hosts is not None:
        resolved_hosts = hosts

        async def from_hosts(hosts: tuple[str, ...] = resolved_hosts) -> list[ProbeResult]:
            return await run_preflight(hosts, budget_seconds=budget)

        return from_hosts
    return preflight


async def _collect_preflight_probes(
    preflight: _PreflightFactory,
    budget: float,
    stop_event: asyncio.Event,
    faults: FaultReporter | None,
) -> list[ProbeResult] | None:
    try:
        async with asyncio.timeout(budget):
            return await until_stopped(preflight(), stop_event)
    except (OSError, TimeoutError) as error:
        if faults is not None:
            faults.record(classify_exception("preflight", error))
        return []


async def _report_preflight_failure(
    lifecycle: LifecycleThread,
    faults: FaultReporter | None,
    probes: list[ProbeResult],
) -> str:
    failed_hosts = ", ".join(probe.host for probe in probes if not probe.ok)
    detail = f"preflight_unreachable: {failed_hosts or 'probe failed'}"
    if faults is not None:
        faults.record(ProviderFault("preflight", "unavailable", retryable=True))
    await lifecycle.send("infra_failed", "system_fault")
    return detail


async def _run_preflight_phase(
    lifecycle: LifecycleThread,
    stop_event: asyncio.Event,
    faults: FaultReporter | None,
    preflight: _PreflightFactory | None,
    budget: float,
) -> str | None:
    if preflight is None:
        return None
    probes = await _collect_preflight_probes(preflight, budget, stop_event, faults)
    if probes is None or stop_event.is_set():
        await lifecycle.send("owner_stopped", "owner_action")
        return "Owner stopped preflight"
    if not probes or not all(probe.ok for probe in probes):
        return await _report_preflight_failure(lifecycle, faults, probes)
    return None


async def _open_session_page(
    resources: TeardownStack,
    config: SessionConfig,
    open_page: Callable[..., AbstractAsyncContextManager[Page]],
    open_audio: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None,
    stop_event: asyncio.Event,
) -> Page | None:
    return await until_stopped(
        resources.enter_async_context(
            open_page(
                launch=BrowserLaunch(
                    headless=config.headless,
                    license_key=config.license_key,
                    fingerprint_seed=stable_fingerprint_seed(config.twin_id),
                    profile_dir=config.browser_profile_dir,
                    warmup=config.warmup_browser,
                ),
                **(
                    {
                        "environment": {
                            "PULSE_SOURCE": AGENT_MIC_SOURCE_NAME,
                            "PULSE_SINK": MEETING_SINK_NAME,
                        }
                    }
                    if open_audio is not None
                    else {}
                ),
            )
        ),
        stop_event,
    )


async def _open_session_audio(
    resources: TeardownStack,
    open_audio: Callable[[], AbstractAsyncContextManager[MeetingAudio]],
    stop_event: asyncio.Event,
    lifecycle: LifecycleThread,
) -> tuple[MeetingAudio | None, str | None]:
    try:
        audio = await until_stopped(resources.enter_async_context(open_audio()), stop_event)
    except MeetingAudioError as error:
        detail = f"meeting audio could not start: {_failure_detail(error)}"
        await lifecycle.send("infra_failed", "system_fault")
        return None, detail
    if audio is None:
        detail = "owner stopped audio startup"
        await lifecycle.send("owner_stopped", "owner_action")
        return None, detail
    return audio, None


async def _run_lifecycle(lifecycle: LifecycleThread, context: RuntimeContext) -> None:
    state = await lifecycle.read()
    if state["phase"] == "created":
        await lifecycle.graph.ainvoke(lifecycle.initial, lifecycle.config, context=context)
        return
    await resume_lifecycle(lifecycle.graph, lifecycle.thread_id, context)


async def _launch_session_browser(
    config: SessionConfig,
    hooks: SessionWiring,
    stop_event: asyncio.Event,
    resources: TeardownStack,
    lifecycle: LifecycleThread,
) -> str:
    page = await _open_session_page(
        resources, config, hooks.open_page, hooks.open_audio, stop_event
    )
    if page is None:
        detail = "owner stopped browser startup"
        await lifecycle.send("owner_stopped", "owner_action")
        return detail
    if hooks.commands is not None and config.meet_actions_enabled:
        hooks.commands.bind_page(
            page, sleep=hooks.sleep, monotonic=hooks.monotonic, stop=stop_event
        )
    journal = _resolve_journal(hooks, config)
    audio: MeetingAudio | None = None
    if hooks.open_audio is not None:
        audio, ended = await _open_session_audio(resources, hooks.open_audio, stop_event, lifecycle)
        if audio is None:
            return ended or "meeting audio unavailable"
        if hooks.conversation is not None:
            resources.push_async_callback(hooks.conversation.close)
        journal.record("audio_opened", source="runtime")
    worker = MeetingWorker(
        config,
        page,
        stop_event,
        hooks.signals,
        hooks.sleep,
        hooks.monotonic,
        hooks.open_audio,
        hooks.initial_speech,
        hooks.conversation,
        audio=audio,
        faults=hooks.faults,
        journal=journal,
        voice_activity=hooks.voice_activity,
        perception=hooks.perception,
        on_phase=hooks.on_phase,
        moments=hooks.moments,
        kernel=hooks.kernel,
        commands=hooks.commands,
    )
    context = RuntimeContext(browser=page, audio=audio, session=worker)
    try:
        await _run_lifecycle(lifecycle, context)
    finally:
        if hooks.commands is not None:
            hooks.commands.close()
    return worker.detail or "session ended"


async def _is_terminal(lifecycle: LifecycleThread) -> bool:
    return (await lifecycle.read())["phase"] in TERMINAL_PHASES


async def startup_session(
    config: SessionConfig,
    hooks: SessionWiring,
    stop_event: asyncio.Event,
    resources: TeardownStack,
    lifecycle: LifecycleThread,
) -> str:
    if stop_event.is_set():
        detail = "owner requested stop before startup"
        await lifecycle.send("owner_stopped", "owner_action")
        return detail
    budget = (
        hooks.preflight_budget_seconds
        if hooks.preflight_budget_seconds is not None
        else config.preflight_budget_seconds
    )
    preflight = _resolve_preflight(hooks.preflight, hooks.preflight_hosts, budget)
    ended_detail = await _run_preflight_phase(
        lifecycle, stop_event, hooks.faults, preflight, budget
    )
    if ended_detail is None and not await _is_terminal(lifecycle):
        return await _launch_session_browser(config, hooks, stop_event, resources, lifecycle)
    return ended_detail or "session already ended"


async def _record_session_failure(
    lifecycle: LifecycleThread,
    journal: Journal,
    kind: LifecycleEventKind,
    error: Exception,
    label: str,
) -> str:
    detail = f"{label} ({type(error).__name__})"
    journal.record(
        f"{kind.removesuffix('_failed')}_error_recorded",
        source="runtime",
        error=str(error),
        error_type=type(error).__name__,
    )
    await lifecycle.send(kind, "system_fault")
    return detail


async def _record_conversation_failure(
    lifecycle: LifecycleThread, journal: Journal, error: ConversationError
) -> str:
    detail = f"conversation failed ({type(error).__name__})"
    journal.record(
        "conversation_error_recorded",
        source="runtime",
        error=str(error),
        error_type=type(error).__name__,
        traceback=bounded_traceback(error),
    )
    await lifecycle.send("conversation_failed", "system_fault")
    return detail


async def _record_browser_failure(
    lifecycle: LifecycleThread, journal: Journal, error: Exception
) -> str:
    detail = f"browser session failed ({type(error).__name__})"
    journal.record(
        "browser_error_recorded",
        source="runtime",
        error=str(error),
        error_type=type(error).__name__,
        traceback=bounded_traceback(error),
    )
    await lifecycle.send("browser_failed", "system_fault")
    return detail


async def _record_cancellation(record: Awaitable[None]) -> None:
    write = asyncio.ensure_future(record)
    while not write.done():
        with suppress(asyncio.CancelledError):
            await asyncio.shield(write)


async def drive_session(
    config: SessionConfig,
    hooks: SessionWiring,
    stop_event: asyncio.Event,
    resources: TeardownStack,
    lifecycle: LifecycleThread,
) -> str:
    journal = _resolve_journal(hooks, config)
    if await _is_terminal(lifecycle):
        return "session already ended"

    async def attempt() -> str:
        return await startup_session(config, hooks, stop_event, resources, lifecycle)

    try:
        outcome, error = await capture_error(attempt)
    except asyncio.CancelledError:
        await _record_cancellation(
            guard_consumer(
                partial(lifecycle.send, "cancelled", "owner_action"),
                sink=ConsumerSink(faults=hooks.faults, journal=journal),
                source="runtime",
                detail="cancel_lifecycle",
            )
        )
        raise
    if error is not None:
        if isinstance(error, FakeYouError):
            raise error
        if stop_event.is_set() and (await lifecycle.read())["phase"] not in (
            "leaving",
            "ended",
        ):
            journal.record(
                "browser_error_recorded",
                source="runtime",
                error=str(error),
                error_type=type(error).__name__,
                detail="during owner-requested teardown",
            )
            await lifecycle.send("owner_stopped", "owner_action")
            return "owner requested stop"
        if isinstance(error, ConversationError):
            return await _record_conversation_failure(lifecycle, journal, error)
        return await _record_browser_failure(lifecycle, journal, error)
    assert outcome is not None
    return outcome


async def run_meeting_session(
    config: SessionConfig, wiring: SessionWiring | None = None
) -> SessionResult:
    hooks = wiring if wiring is not None else SessionWiring()
    _validate_session_audio(hooks)
    stop_event = hooks.stop if hooks.stop is not None else asyncio.Event()
    async with session_resources(hooks.faults) as resources:
        if hooks.conversation is not None:
            resources.push_async_callback(hooks.conversation.close)
        saver = await resources.enter_async_context(
            hooks.saver_factory()
            if hooks.saver_factory is not None
            else open_checkpointer(
                require_url(config.database_url),
                connect_timeout_seconds=config.database_connect_timeout_seconds,
                statement_timeout_seconds=config.database_statement_timeout_seconds,
                keepalive_idle_seconds=config.database_keepalive_idle_seconds,
            )
        )
        lifecycle = LifecycleThread(
            build_lifecycle_graph(
                DegradingCheckpointer(
                    saver,
                    CheckpointDegradation(
                        send_timeout_seconds=config.lifecycle_send_timeout_seconds,
                        faults=hooks.faults,
                        journal=_resolve_journal(hooks, config),
                    ),
                ),
                join_retry=join_retry_policy(
                    max_attempts=config.join_max_attempts,
                    base_seconds=config.join_retry_base_seconds,
                    cap_seconds=config.join_retry_cap_seconds,
                ),
            ),
            meeting_thread_id(config.twin_id, config.meeting_id),
            initial_lifecycle_state(config.owner_id, config.twin_id, config.meeting_id),
        )
        await initialize_lifecycle(lifecycle, resume=config.resume)
        detail = await drive_session(config, hooks, stop_event, resources, lifecycle)
        final = await lifecycle.read()
    return _terminal_result(
        final["phase"],
        cast(TerminalReason | None, final["terminal_reason"]),
        detail,
        hooks.faults,
    )
