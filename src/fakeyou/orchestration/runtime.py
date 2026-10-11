import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from playwright.async_api import Page

from fakeyou.audio.audio import MeetingAudioError
from fakeyou.audio.stream_capture import (
    CaptureDrainLoop,
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
from fakeyou.conversation.session import (
    ConversationError,
    MeetingAudio,
    MeetingConversation,
    SessionConfig,
    SessionResult,
    SupportsAdmission,
    SupportsMaintenance,
)
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import require_url
from fakeyou.foundation.attribution_probe import AttributionProbe, ProbeDrainLoop, load_probe_script
from fakeyou.foundation.invariants import AGENT_MIC_SOURCE_NAME, MEETING_SINK_NAME
from fakeyou.foundation.taxonomy import MeetingPhase, TerminalReason
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
from fakeyou.meet.meet_action_ledger import (
    MeetActionAddress,
    MeetActionCall,
    MeetActionEffects,
    MeetActionKind,
    execute_meet_action,
)
from fakeyou.meet.meet_actions import (
    DEFAULT_MEET_ACTION_SIGNALS,
    MeetActionResult,
    lower_hand,
    raise_hand,
    send_chat,
)
from fakeyou.meet.participant_observer import (
    QUERY_METHOD_MINING,
    ParticipantObserver,
    append_participant_evidence,
    observer_epoch,
)
from fakeyou.meet.watchdog import UIWatchdog, WatchdogCheck
from fakeyou.observability.cleanup import close_safely
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.orchestration.lifecycle import (
    LifecycleAttribution,
    LifecycleEventKind,
    MeetingLifecycle,
)
from fakeyou.orchestration.preflight import ProbeResult, run_preflight
from fakeyou.visual.visual_caption import VisualCaption, VisualCaptioner
from fakeyou.visual.visual_observer import CaptionSink, PollSchedule, VisualObserver
from fakeyou.visual.visual_store import VisualFrame, VisualStore


@asynccontextmanager
async def _session_resources(faults: FaultReporter | None) -> AsyncIterator[AsyncExitStack]:
    stack = AsyncExitStack()
    try:
        yield stack
    finally:
        await close_safely(stack.aclose, source="runtime", operation="teardown", faults=faults)


def _fault_report(faults: FaultReporter | None) -> dict[str, Any] | None:
    return faults.report() if faults is not None else None


def _fault_suffix(faults: FaultReporter | None) -> str:
    try:
        summaries = faults.summaries() if faults is not None else ()
    except Exception:
        return ""
    if not summaries:
        return ""
    top = summaries[0]
    return f" [{top.source}_degraded: {top.kind}x{top.count}]"


_JOIN_EVENTS: dict[JoinStatus, tuple[LifecycleEventKind, LifecycleAttribution]] = {
    "admitted": ("admitted", "host_action"),
    "denied": ("denied", "host_action"),
    "blocked": ("blocked", "system_fault"),
    "timeout": ("lobby_timed_out", "system_fault"),
    "aborted": ("owner_stopped", "owner_action"),
    "failed": ("browser_failed", "system_fault"),
}


def _terminal_result(
    phase: MeetingPhase,
    terminal_reason: TerminalReason | None,
    detail: str,
    faults: FaultReporter | None,
) -> SessionResult:
    return SessionResult(
        phase, terminal_reason, detail + _fault_suffix(faults), _fault_report(faults)
    )


_JOIN_RETRYABLE_STATUSES: tuple[JoinStatus, ...] = ("timeout", "failed")


@dataclass(frozen=True, slots=True)
class JoinRetryPolicy:
    max_attempts: int
    base_seconds: float
    cap_seconds: float
    rng: Callable[[float, float], float] = random.uniform


async def run_join_with_retry(
    attempt: Callable[[], Awaitable[JoinResult]],
    *,
    policy: JoinRetryPolicy,
    sleep: Callable[[float], Awaitable[None]],
    record: Callable[..., Any],
    should_stop: Callable[[], bool],
) -> tuple[JoinResult, int]:
    attempts = 0
    while True:
        result = await attempt()
        attempts += 1
        if result.status not in _JOIN_RETRYABLE_STATUSES:
            return result, attempts
        if attempts >= policy.max_attempts:
            return result, attempts
        if should_stop():
            stopped = JoinResult(status="aborted", detail="owner stopped before join retry")
            return stopped, attempts
        bound = min(policy.cap_seconds, policy.base_seconds * (2.0 ** (attempts - 1)))
        delay = policy.rng(0.0, bound)
        record(
            "join_retried",
            source="runtime",
            attempt=attempts,
            status=result.status,
            delay_ms=delay * 1000,
        )
        await sleep(delay)
        if should_stop():
            stopped = JoinResult(status="aborted", detail="owner stopped before join retry")
            return stopped, attempts


async def _until_stopped[T](operation: Coroutine[Any, Any, T], stop: asyncio.Event) -> T | None:
    running = asyncio.create_task(operation)
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((running, stopping), return_when=asyncio.FIRST_COMPLETED)
        return running.result() if running.done() else None
    finally:
        running.cancel()
        stopping.cancel()
        await asyncio.gather(running, stopping, return_exceptions=True)


WRITE_ACTION_BUDGET_P95_SECONDS = 12.0
WRITE_ACTION_BUDGET_MAX_SECONDS = 30.0
WRITE_ACTION_STABILITY_MIN = 0.95


class MeetActionDispatcher:
    def __init__(
        self,
        record_path: Path,
        *,
        meeting_id: str,
        enabled: bool,
        journal: Journal | None = None,
    ) -> None:
        self._record_path = record_path
        self._meeting_id = meeting_id
        self._enabled = enabled
        self._journal = journal
        self._signals = DEFAULT_MEET_ACTION_SIGNALS
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._monotonic: Callable[[], float] = time.monotonic
        self._page: Page | None = None
        self._stop: asyncio.Event | None = None
        self._generation = 0
        self._closed = False
        self._wake = asyncio.Event()
        self._pending: dict[asyncio.Task[None], tuple[MeetActionKind, str]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def closed(self) -> bool:
        return self._closed

    def request(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> str:
        task = asyncio.create_task(self._execute(kind, action_id, text, self._generation))
        self._pending[task] = (kind, action_id)
        task.add_done_callback(self._finish)
        return action_id

    def bind_page(
        self,
        page: Page,
        *,
        sleep: Callable[[float], Awaitable[None]],
        monotonic: Callable[[], float],
        stop: asyncio.Event,
    ) -> None:
        self._page = page
        self._sleep = sleep
        self._monotonic = monotonic
        self._stop = stop
        self._wake.set()

    def cancel_pending(self) -> None:
        self._generation += 1
        for task, (kind, action_id) in list(self._pending.items()):
            self._pending.pop(task, None)
            if task.done():
                continue
            self._reject(kind, action_id, "stale_cancelled")
            task.cancel()
        self._wake.set()

    def close(self) -> None:
        self._closed = True
        self.cancel_pending()

    async def drain(self) -> None:
        await asyncio.gather(*list(self._pending), return_exceptions=True)

    def _finish(self, task: asyncio.Task[None]) -> None:
        self._pending.pop(task, None)

    def _reject(self, kind: MeetActionKind, action_id: str, reason: str) -> None:
        if self._journal is not None:
            self._journal.meet_action_rejected(action=kind, action_id=action_id, reason=reason)

    def _fail(self, kind: MeetActionKind, action_id: str, reason: str) -> None:
        if self._journal is not None:
            self._journal.meet_action_failed(
                action=kind, action_id=action_id, reason=reason, attempts=1
            )

    async def _execute(
        self, kind: MeetActionKind, action_id: str, text: str, generation: int
    ) -> None:
        await self._wake.wait()
        page = self._page
        stop = self._stop
        if generation != self._generation or self._closed or page is None or stop is None:
            self._reject(kind, action_id, "stale_cancelled")
            return
        if stop.is_set():
            self._reject(kind, action_id, "stale_cancelled")
            return
        if kind == "send_chat":
            params = text
            chars: int | None = len(text)
        else:
            params = kind
            chars = None
        box: dict[str, MeetActionResult | None] = {"result": None}

        async def act() -> MeetActionResult:
            if kind == "send_chat":
                result = await send_chat(page, text, self._signals, self._sleep, self._monotonic)
            elif kind == "raise_hand":
                result = await raise_hand(page, self._signals, self._sleep, self._monotonic)
            else:
                result = await lower_hand(page, self._signals, self._sleep, self._monotonic)
            box["result"] = result
            return result

        async def observe() -> bool:
            current = box["result"]
            return current.succeeded if current is not None else False

        try:
            outcome = await _until_stopped(
                execute_meet_action(
                    self._record_path,
                    MeetActionAddress(self._meeting_id, action_id),
                    MeetActionCall(kind, params, chars),
                    MeetActionEffects(act, observe),
                    self._journal,
                ),
                stop,
            )
        except Exception as error:
            self._fail(kind, action_id, type(error).__name__)
            return
        if outcome is None:
            self._reject(kind, action_id, "stale_cancelled")


@dataclass(slots=True)
class _MonitorScan:
    next_scan: float
    remote_participants: int | None
    presence_at: float
    observation_errors: int


@dataclass
class _MeetingWorker:
    config: SessionConfig
    lifecycle: MeetingLifecycle
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
    probe_drain: ProbeDrainLoop | None = None
    voice_activity: VoiceActivityTracker | None = None
    voice_loop: VoiceActivityLoop | None = None
    capture_drain: CaptureDrainLoop | None = None
    capture_registry: StreamRegistry | None = None
    visual_store: VisualStore | None = None
    visual_observer: VisualObserver | None = None
    visual_captioner: VisualCaptioner | None = None
    visual_on_caption: Callable[[VisualFrame, VisualCaption], None] | None = None

    async def run(self) -> str:
        if self.open_audio is not None:
            return await self.run_with_audio(self.open_audio)
        detail = await self.join()
        if self.lifecycle.state["phase"] == "admitted":
            admitted_at = self.monotonic()
            await self.disable_camera_best_effort()
            detail = await self.monitor(admitted_at)
        return detail

    async def disable_camera_best_effort(self) -> None:
        if self.stop.is_set():
            return
        try:
            await _until_stopped(disable_meeting_camera(self.page, self.sleep), self.stop)
        except Exception as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation="camera"))

    async def run_with_audio(
        self, open_audio: Callable[[], AbstractAsyncContextManager[MeetingAudio]]
    ) -> str:
        if self.stop.is_set():
            return await self.finish(
                "owner_stopped", "owner_action", "owner stopped before guest join"
            )
        try:
            async with _session_resources(self.faults) as resources:
                started = self.monotonic()
                try:
                    self.audio = await _until_stopped(
                        resources.enter_async_context(open_audio()), self.stop
                    )
                except MeetingAudioError as error:
                    return await self.finish(
                        "infra_failed", "system_fault", f"meeting audio could not start: {error}"
                    )
                if self.audio is None:
                    return await self.finish(
                        "owner_stopped", "owner_action", "owner stopped audio startup"
                    )
                if self.conversation is not None:
                    resources.push_async_callback(self.conversation.close)
                self.record(
                    "audio_opened",
                    source="runtime",
                    duration_ms=(self.monotonic() - started) * 1000,
                )
                detail = await self.join()
                if self.lifecycle.state["phase"] != "admitted":
                    return detail
                return await self.run_admitted(self.audio, self.monotonic())
        except ConversationError as error:
            self.audio = None
            return await self.finish("conversation_failed", "system_fault", str(error))
        except MeetingAudioError as error:
            self.audio = None
            return await self.finish("audio_failed", "system_fault", str(error))
        finally:
            self.audio = None

    async def _start_capture_guarded(self, *, enabled: bool) -> None:
        try:
            await self.start_capture_drain(enabled=enabled)
        except Exception as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation="stream_capture"))

    async def run_admitted(self, audio: MeetingAudio, admitted_at: float) -> str:
        audio.raise_if_failed()
        if isinstance(audio, SupportsAdmission):
            audio.mark_admitted()
        await self.disable_camera_best_effort()
        try:
            enabled = await _until_stopped(
                enable_meeting_microphone(self.page, self.sleep), self.stop
            )
        except Exception as error:
            raise MeetingAudioError("meeting microphone could not be enabled") from error
        if enabled is None or self.stop.is_set():
            return await self.finish(
                "owner_stopped", "owner_action", "owner stopped microphone startup"
            )
        self.record("microphone_enabled", source="runtime")
        audio.raise_if_failed()
        await self._start_capture_guarded(enabled=self.config.per_stream_enabled)
        if self.initial_speech:
            await _until_stopped(audio.speak(self.initial_speech), self.stop)
        if self.stop.is_set():
            await self.close_capture_drain()
            return await self.finish(
                "owner_stopped", "owner_action", "owner stopped conversation startup"
            )
        if self.conversation is not None:
            try:
                self.conversation.bind(audio)
            except ConversationError as error:
                await self.disable_conversation(error)
            else:
                self.record("conversation_bound", source="runtime")
        return await self.monitor(admitted_at)

    async def join(self) -> str:
        if self.stop.is_set():
            return await self.finish(
                "owner_stopped", "owner_action", "owner stopped before guest join"
            )
        await self.lifecycle.send("join_requested", "owner_action", "worker requested guest join")
        self.record("join_requested", source="runtime")
        started = self.monotonic()
        request = GuestJoinRequest(
            link=self.config.link,
            twin_name=self.config.twin_name,
            lobby_timeout_seconds=self.config.lobby_timeout_seconds,
            poll_interval_seconds=self.config.join_poll_interval_seconds,
        )
        probe_scripts = (
            ((load_probe_script(),) if self.config.attribution_probe_path is not None else ())
            + ((load_voice_activity_script(),) if self.voice_activity is not None else ())
            + ((load_capture_script(),) if self.config.per_stream_enabled else ())
        )

        async def attempt() -> JoinResult:
            outcome = await _until_stopped(
                request_guest_join(
                    self.page,
                    request,
                    self.signals,
                    JoinPolling(self.sleep, self.monotonic, self.stop.is_set),
                    probe_scripts,
                ),
                self.stop,
            )
            if outcome is None:
                return JoinResult(status="aborted", detail="owner stopped guest join")
            return outcome

        async def backoff_sleep(delay: float) -> None:
            await self.wait(delay)

        result, attempts = await run_join_with_retry(
            attempt,
            policy=JoinRetryPolicy(
                self.config.join_max_attempts,
                self.config.join_retry_base_seconds,
                self.config.join_retry_cap_seconds,
            ),
            sleep=backoff_sleep,
            record=self.record,
            should_stop=self.stop.is_set,
        )
        self.record(
            "join_finished",
            source="runtime",
            status=result.status,
            detail=result.detail,
            duration_ms=(self.monotonic() - started) * 1000,
            audio_ready=self.audio is not None,
            attempts=attempts,
        )
        kind, attribution = _JOIN_EVENTS[result.status]
        await self.lifecycle.send(kind, attribution, result.detail)
        return result.detail

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

        def persist(raw: Any, mono_ms: float) -> None:
            try:
                append_participant_evidence(
                    evidence_path, raw, mono_ms, epoch=epoch, method=QUERY_METHOD_MINING
                )
            except Exception as error:
                if faults is not None:
                    faults.record(
                        classify_exception("browser", error, operation="participant_evidence")
                    )

        self.observer = ParticipantObserver(
            self.page, epoch=epoch, monotonic=self.monotonic, on_poll=persist
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

    async def start_probe_drain(self) -> None:
        if self.probe_drain is not None:
            return
        probe_path = self.config.attribution_probe_path
        if probe_path is None:
            return
        probe = AttributionProbe(probe_path, monotonic=self.monotonic)
        self.probe_drain = ProbeDrainLoop(self.page, probe)
        await self.probe_drain.start()
        self.record("attribution_probe_started", source="runtime")

    async def start_voice_activity(self) -> None:
        if self.voice_loop is not None or self.voice_activity is None:
            return
        self.voice_loop = VoiceActivityLoop(self.page, self.voice_activity)
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

    async def close_probe_drain(self) -> None:
        if self.probe_drain is not None:
            await close_safely(self.probe_drain.close, source="browser", faults=self.faults)
            self.record(
                "attribution_probe_stopped",
                source="runtime",
                polls=self.probe_drain.polls,
                poll_failures=self.probe_drain.poll_failures,
                last_poll_error=self.probe_drain.last_poll_error,
            )
            self.probe_drain = None

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
            journal=self.journal,
            twin_name=self.config.twin_name,
        )
        await self.capture_drain.start()
        self.record("stream_capture_started", source="runtime")

    async def close_capture_drain(self) -> None:
        if self.capture_drain is not None:
            await close_safely(self.capture_drain.close, source="browser", faults=self.faults)
            self.capture_drain = None
        self.capture_registry = None

    async def start_visual_observer(self) -> None:
        if self.visual_observer is not None or self.visual_store is None:
            return
        if not self.config.visual_enabled:
            return
        self.visual_observer = VisualObserver(
            self.page,
            self.visual_store,
            journal=self.journal,
            schedule=PollSchedule(monotonic=self.monotonic),
            captions=CaptionSink(self.visual_captioner, self.visual_on_caption),
        )
        await self.visual_observer.start()
        self.record("visual_observer_started", source="runtime")

    async def close_visual_observer(self) -> None:
        if self.visual_observer is not None:
            await close_safely(self.visual_observer.close, source="browser", faults=self.faults)
            self.record(
                "visual_observer_stopped",
                source="runtime",
                polls=self.visual_observer.polls,
                poll_failures=self.visual_observer.poll_failures,
                last_poll_error=self.visual_observer.last_poll_error,
                caption_failures=self.visual_observer.caption_failures,
            )
            self.visual_observer = None

    async def _start_guarded(self, starter: Callable[[], Awaitable[None]], operation: str) -> None:
        try:
            await starter()
        except Exception as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation=operation))

    async def monitor(self, admitted_at: float) -> str:
        try:
            await self._start_guarded(self.start_participant_observer, "participant_observer")
            await self._start_guarded(self.start_probe_drain, "attribution_probe")
            await self._start_capture_guarded(enabled=self.config.per_stream_enabled)
            await self._start_guarded(self.start_voice_activity, "voice_activity")
            await self._start_guarded(self.start_visual_observer, "visual_observer")
            return await self._monitor_loop(admitted_at)
        finally:
            await self.close_visual_observer()
            await self.close_voice_activity()
            await self.close_probe_drain()
            await self.close_capture_drain()
            await self.close_participant_observer()

    async def _poll_stop(self, deadline: float) -> str | None:
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

    async def _service_audio(
        self, remote_participants: int | None, presence_at: float
    ) -> str | None:
        if self.audio is None:
            return None
        self.audio.raise_if_failed()
        if not isinstance(self.audio, SupportsMaintenance):
            return None
        verdict = await _until_stopped(
            self.audio.maintenance_tick(
                is_busy=self.conversation is not None and self.conversation.is_busy,
                remote_participants=remote_participants
                if self.monotonic() - presence_at <= 30
                else None,
            ),
            self.stop,
        )
        if verdict == "alone":
            return await self.finish(
                "participants_left",
                "policy",
                "No remote participants for the presence window",
            )
        return None

    def _note_slow_poll(self, poll_seconds: float) -> None:
        if poll_seconds >= 1.0 and self.journal is not None:
            self.journal.record("monitor_polled", source="runtime", duration_ms=poll_seconds * 1000)

    async def _track_observation(self, scan: _MonitorScan, checked: WatchdogCheck) -> str | None:
        if not checked.observation_failed:
            scan.observation_errors = 0
            return None
        scan.observation_errors += 1
        if scan.observation_errors < 2:
            return None
        cause = next(
            (event.detail for event in checked.events if event.kind == "watchdog_failed"),
            "unknown",
        )
        return await self.finish(
            "browser_failed",
            "system_fault",
            f"Meeting UI observation failed: {cause}",
        )

    async def _record_scan_events(
        self, scan: _MonitorScan, checked: WatchdogCheck, now: float
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

    async def _apply_observation(self, checked: WatchdogCheck) -> str | None:
        observation = checked.observation
        if observation.state in ("removed", "ended"):
            kind: LifecycleEventKind = (
                "removed" if observation.state == "removed" else "meeting_ended"
            )
            await self.lifecycle.send(kind, "host_action", observation.detail)
            return observation.detail
        phase = self.lifecycle.state["phase"]
        if observation.state == "lobby" and phase == "admitted":
            await self.lifecycle.send("connection_lost", "system_fault", observation.detail)
        elif observation.state == "in_call" and phase == "disconnected":
            await self.lifecycle.send("reconnected", "system_fault", observation.detail)
        return None

    async def _scan_once(
        self, watchdog: UIWatchdog, scan: _MonitorScan, now: float
    ) -> tuple[str | None, bool]:
        poll_started = self.monotonic()
        checked = await _until_stopped(watchdog.check(), self.stop)
        if checked is None:
            return None, True
        self._note_slow_poll(self.monotonic() - poll_started)
        finished = await self._track_observation(scan, checked)
        if finished is not None:
            return finished, False
        finished = await self._record_scan_events(scan, checked, now)
        if finished is not None:
            return finished, False
        return await self._apply_observation(checked), False

    async def _monitor_loop(self, admitted_at: float) -> str:
        deadline = admitted_at + self.config.session_duration_seconds
        watchdog = UIWatchdog(
            self.page, signals=self.signals, observe=observe_call_state, monotonic=self.monotonic
        )
        scan = _MonitorScan(
            next_scan=admitted_at,
            remote_participants=None,
            presence_at=admitted_at,
            observation_errors=0,
        )
        while True:
            finished = await self._poll_stop(deadline)
            if finished is not None:
                return finished
            await self._service_conversation()
            finished = await self._service_audio(scan.remote_participants, scan.presence_at)
            if finished is not None:
                return finished
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

        await _until_stopped(delay(), self.stop)

    async def finish(
        self, kind: LifecycleEventKind, attribution: LifecycleAttribution, detail: str
    ) -> str:
        await self.lifecycle.send(kind, attribution, detail)
        await self.close_visual_observer()
        await self.close_participant_observer()
        if self.conversation is not None:
            await close_safely(self.conversation.close, source="agent", faults=self.faults)
        if self.audio is not None:
            await close_safely(self.audio.stop, source="audio", faults=self.faults)
        if self.lifecycle.state["phase"] != "leaving":
            return detail
        try:
            async with asyncio.timeout(min(8.0, self.config.leave_timeout_seconds)):
                left = await leave_meeting(
                    self.page,
                    self.signals,
                    self.sleep,
                    self.monotonic,
                    self.config.leave_timeout_seconds,
                )
        except Exception as error:
            if self.faults is not None:
                self.faults.record(classify_exception("browser", error, operation="leave"))
            await self.lifecycle.send(
                "browser_failed", "system_fault", "Meeting leave cleanup failed"
            )
            return detail
        await self.lifecycle.send(
            "leave_sent" if left.status == "left" else "browser_failed",
            attribution if left.status == "left" else "system_fault",
            left.detail,
        )
        return detail if kind in ("audio_failed", "conversation_failed") else left.detail


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
    meet_dispatcher: MeetActionDispatcher | None = None
    visual_store: VisualStore | None = None
    visual_captioner: VisualCaptioner | None = None
    visual_on_caption: Callable[[VisualFrame, VisualCaption], None] | None = None


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
            return await _until_stopped(preflight(), stop_event)
    except Exception as error:
        if faults is not None:
            faults.record(classify_exception("preflight", error))
        return []


async def _report_preflight_failure(
    lifecycle: MeetingLifecycle,
    faults: FaultReporter | None,
    probes: list[ProbeResult],
) -> str:
    failed_hosts = ", ".join(probe.host for probe in probes if not probe.ok)
    detail = f"preflight_unreachable: {failed_hosts or 'probe failed'}"
    if faults is not None:
        faults.record(ProviderFault("preflight", "unavailable", retryable=True))
    await lifecycle.send("infra_failed", "system_fault", detail)
    return detail


async def _run_preflight_phase(
    lifecycle: MeetingLifecycle,
    stop_event: asyncio.Event,
    faults: FaultReporter | None,
    preflight: _PreflightFactory | None,
    budget: float,
) -> str | None:
    if preflight is None:
        return None
    probes = await _collect_preflight_probes(preflight, budget, stop_event, faults)
    if probes is None or stop_event.is_set():
        await lifecycle.send("owner_stopped", "owner_action", "Owner stopped preflight")
        return "Owner stopped preflight"
    if not probes or not all(probe.ok for probe in probes):
        return await _report_preflight_failure(lifecycle, faults, probes)
    return None


async def _open_session_page(
    resources: AsyncExitStack,
    config: SessionConfig,
    open_page: Callable[..., AbstractAsyncContextManager[Page]],
    open_audio: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None,
    stop_event: asyncio.Event,
) -> Page | None:
    return await _until_stopped(
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


async def _run_session_worker(
    worker: _MeetingWorker, meet_dispatcher: MeetActionDispatcher | None
) -> str:
    try:
        return await worker.run()
    finally:
        if meet_dispatcher is not None:
            meet_dispatcher.close()


async def _launch_session_browser(
    config: SessionConfig,
    hooks: SessionWiring,
    stop_event: asyncio.Event,
    resources: AsyncExitStack,
    lifecycle: MeetingLifecycle,
) -> str:
    page = await _open_session_page(
        resources, config, hooks.open_page, hooks.open_audio, stop_event
    )
    if page is None:
        detail = "owner stopped browser startup"
        await lifecycle.send("owner_stopped", "owner_action", detail)
        return detail
    if hooks.meet_dispatcher is not None and config.meet_actions_enabled:
        hooks.meet_dispatcher.bind_page(
            page, sleep=hooks.sleep, monotonic=hooks.monotonic, stop=stop_event
        )
    worker = _MeetingWorker(
        config,
        lifecycle,
        page,
        stop_event,
        hooks.signals,
        hooks.sleep,
        hooks.monotonic,
        hooks.open_audio,
        hooks.initial_speech,
        hooks.conversation,
        faults=hooks.faults,
        journal=_resolve_journal(hooks, config),
        voice_activity=hooks.voice_activity,
        visual_store=hooks.visual_store,
        visual_captioner=hooks.visual_captioner,
        visual_on_caption=hooks.visual_on_caption,
    )
    return await _run_session_worker(worker, hooks.meet_dispatcher)


async def _startup_session(
    config: SessionConfig,
    hooks: SessionWiring,
    stop_event: asyncio.Event,
    resources: AsyncExitStack,
    lifecycle: MeetingLifecycle,
) -> str:
    if stop_event.is_set():
        detail = "owner requested stop before startup"
        await lifecycle.send("owner_stopped", "owner_action", detail)
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
    if ended_detail is None and lifecycle.state["phase"] != "ended":
        return await _launch_session_browser(config, hooks, stop_event, resources, lifecycle)
    return ended_detail or "session already ended"


async def _record_session_failure(
    lifecycle: MeetingLifecycle,
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
    await lifecycle.send(kind, "system_fault", detail)
    return detail


async def _record_conversation_failure(
    lifecycle: MeetingLifecycle, journal: Journal, error: ConversationError
) -> str:
    detail = f"conversation failed ({type(error).__name__})"
    journal.record(
        "conversation_error_recorded",
        source="runtime",
        error=str(error),
        error_type=type(error).__name__,
    )
    await lifecycle.send("conversation_failed", "system_fault", detail)
    return detail


async def _record_browser_failure(
    lifecycle: MeetingLifecycle, journal: Journal, error: Exception
) -> str:
    detail = f"browser session failed ({type(error).__name__})"
    journal.record(
        "browser_error_recorded",
        source="runtime",
        error=str(error),
        error_type=type(error).__name__,
    )
    await lifecycle.send("browser_failed", "system_fault", detail)
    return detail


async def _drive_session(
    config: SessionConfig,
    hooks: SessionWiring,
    stop_event: asyncio.Event,
    resources: AsyncExitStack,
    lifecycle: MeetingLifecycle,
) -> str:
    journal = _resolve_journal(hooks, config)
    if lifecycle.state["phase"] == "ended":
        return "session already ended"
    try:
        return await _startup_session(config, hooks, stop_event, resources, lifecycle)
    except asyncio.CancelledError:
        await lifecycle.send("cancelled", "owner_action", "session task cancelled; closing browser")
        raise
    except FakeYouError:
        raise
    except ConversationError as error:
        return await _record_conversation_failure(lifecycle, journal, error)
    except Exception as error:
        return await _record_browser_failure(lifecycle, journal, error)


async def run_meeting_session(
    config: SessionConfig, wiring: SessionWiring | None = None
) -> SessionResult:
    hooks = wiring if wiring is not None else SessionWiring()
    _validate_session_audio(hooks)
    stop_event = hooks.stop if hooks.stop is not None else asyncio.Event()
    async with _session_resources(hooks.faults) as resources:
        if hooks.conversation is not None:
            resources.push_async_callback(hooks.conversation.close)
        saver = await resources.enter_async_context(
            hooks.saver_factory()
            if hooks.saver_factory is not None
            else open_checkpointer(require_url(config.database_url))
        )
        lifecycle = MeetingLifecycle(config.owner_id, config.twin_id, config.meeting_id, saver)
        await lifecycle.initialize(resume=config.resume)
        detail = await _drive_session(config, hooks, stop_event, resources, lifecycle)
    return _terminal_result(
        lifecycle.state["phase"],
        lifecycle.state["terminal_reason"],
        detail,
        hooks.faults,
    )
