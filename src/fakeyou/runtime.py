import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from playwright.async_api import Page

from fakeyou.audio import MeetingAudioError
from fakeyou.browser import open_direct_page, stable_fingerprint_seed
from fakeyou.checkpoint import open_checkpointer
from fakeyou.cleanup import close_safely
from fakeyou.faults import FaultReporter
from fakeyou.invariants import AGENT_MIC_SOURCE_NAME, MEETING_SINK_NAME
from fakeyou.journal import Journal
from fakeyou.lifecycle import (
    LifecycleAttribution,
    LifecycleEventKind,
    MeetingLifecycle,
    MeetingPhase,
    TerminalReason,
)
from fakeyou.meet import (
    DEFAULT_MEET_SIGNALS,
    GuestJoinRequest,
    JoinStatus,
    MeetSignals,
    disable_meeting_camera,
    enable_meeting_microphone,
    leave_meeting,
    observe_call_state,
    request_guest_join,
)
from fakeyou.preflight import ProbeResult, run_preflight
from fakeyou.provider_faults import ProviderFault, classify_exception
from fakeyou.session import (
    ConversationError,
    MeetingAudio,
    MeetingConversation,
    SessionConfig,
    SessionResult,
    SupportsAdmission,
    SupportsMaintenance,
)
from fakeyou.watchdog import UIWatchdog


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
        if self.initial_speech:
            await _until_stopped(audio.speak(self.initial_speech), self.stop)
        if self.stop.is_set():
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
        result = await _until_stopped(
            request_guest_join(
                self.page, request, self.signals, self.sleep, self.monotonic, self.stop.is_set
            ),
            self.stop,
        )
        if result is None:
            return await self.finish("owner_stopped", "owner_action", "owner stopped guest join")
        self.record(
            "join_finished",
            source="runtime",
            status=result.status,
            duration_ms=(self.monotonic() - started) * 1000,
            audio_ready=self.audio is not None,
        )
        kind, attribution = _JOIN_EVENTS[result.status]
        await self.lifecycle.send(kind, attribution, result.detail)
        return result.detail

    async def monitor(self, admitted_at: float) -> str:
        deadline = admitted_at + self.config.session_duration_seconds
        watchdog = UIWatchdog(
            self.page, signals=self.signals, observe=observe_call_state, monotonic=self.monotonic
        )
        next_scan = admitted_at
        remote_participants: int | None = None
        presence_at = admitted_at
        observation_errors = 0
        while True:
            if self.stop.is_set():
                return await self.finish("owner_stopped", "owner_action", "owner requested stop")
            if self.monotonic() >= deadline:
                return await self.finish("duration_reached", "policy", "session duration reached")
            if self.conversation is not None:
                try:
                    self.conversation.raise_if_failed()
                except ConversationError as error:
                    await self.disable_conversation(error)
            if self.audio is not None:
                self.audio.raise_if_failed()
                if isinstance(self.audio, SupportsMaintenance):
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
            now = self.monotonic()
            if now >= next_scan:
                poll_started = self.monotonic()
                checked = await _until_stopped(watchdog.check(), self.stop)
                if checked is None:
                    continue
                poll_seconds = self.monotonic() - poll_started
                if poll_seconds >= 1.0:
                    if self.journal is not None:
                        self.journal.record(
                            "monitor_polled", source="runtime", duration_ms=poll_seconds * 1000
                        )
                if checked.observation_failed:
                    observation_errors += 1
                    if observation_errors >= 2:
                        cause = next(
                            (
                                event.detail
                                for event in checked.events
                                if event.kind == "watchdog_failed"
                            ),
                            "unknown",
                        )
                        return await self.finish(
                            "browser_failed",
                            "system_fault",
                            f"Meeting UI observation failed: {cause}",
                        )
                else:
                    observation_errors = 0
                next_scan = (
                    now + checked.next_check_seconds * self.config.monitor_interval_seconds / 5
                )
                remote_participants = (
                    max(0, checked.participant_count - 1)
                    if checked.participant_count is not None
                    else None
                )
                presence_at = self.monotonic()
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
            await self.wait(min(1.0, next_scan - self.monotonic(), deadline - self.monotonic()))

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


async def run_meeting_session(
    config: SessionConfig,
    *,
    stop: asyncio.Event | None = None,
    signals: MeetSignals = DEFAULT_MEET_SIGNALS,
    open_page: Callable[..., AbstractAsyncContextManager[Page]] = open_direct_page,
    saver_factory: Callable[[], AbstractAsyncContextManager[BaseCheckpointSaver[Any]]]
    | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    open_audio: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None = None,
    initial_speech: str | None = None,
    conversation: MeetingConversation | None = None,
    faults: FaultReporter | None = None,
    journal: Journal | None = None,
    preflight: Callable[[], Coroutine[Any, Any, list[ProbeResult]]] | None = None,
    preflight_hosts: tuple[str, ...] | None = None,
    preflight_budget_seconds: float | None = None,
) -> SessionResult:
    if initial_speech is not None and open_audio is None:
        raise ValueError("initial speech requires meeting audio")
    if conversation is not None and open_audio is None:
        raise ValueError("conversation requires meeting audio")
    journal = journal or (faults.journal if faults is not None else Journal(config.meeting_id))
    stop_event = stop if stop is not None else asyncio.Event()
    async with _session_resources(faults) as resources:
        if conversation is not None:
            resources.push_async_callback(conversation.close)
        saver = await resources.enter_async_context(
            saver_factory()
            if saver_factory is not None
            else open_checkpointer(config.checkpoint_path)
        )
        lifecycle = MeetingLifecycle(config.owner_id, config.twin_id, config.meeting_id, saver)
        await lifecycle.initialize()
        detail = "session already ended"
        if lifecycle.state["phase"] != "ended":
            try:
                if stop_event.is_set():
                    detail = "owner requested stop before startup"
                    await lifecycle.send("owner_stopped", "owner_action", detail)
                else:
                    budget = (
                        preflight_budget_seconds
                        if preflight_budget_seconds is not None
                        else config.preflight_budget_seconds
                    )
                    if preflight is None and preflight_hosts is not None:
                        hosts = preflight_hosts

                        async def from_hosts(
                            hosts: tuple[str, ...] = hosts,
                        ) -> list[ProbeResult]:
                            return await run_preflight(hosts, budget_seconds=budget)

                        preflight = from_hosts
                    if preflight is not None:
                        probes: list[ProbeResult] | None
                        try:
                            async with asyncio.timeout(budget):
                                probes = await _until_stopped(preflight(), stop_event)
                        except Exception as error:
                            probes = []
                            if faults is not None:
                                faults.record(classify_exception("preflight", error))
                        if probes is None or stop_event.is_set():
                            detail = "Owner stopped preflight"
                            await lifecycle.send(
                                "owner_stopped", "owner_action", "Owner stopped preflight"
                            )
                        elif not probes or not all(probe.ok for probe in probes):
                            failed_hosts = ", ".join(probe.host for probe in probes if not probe.ok)
                            detail = f"preflight_unreachable: {failed_hosts or 'probe failed'}"
                            if faults is not None:
                                faults.record(
                                    ProviderFault("preflight", "unavailable", retryable=True)
                                )
                            await lifecycle.send("infra_failed", "system_fault", detail)
                    junction_phase: MeetingPhase = lifecycle.state["phase"]
                    if junction_phase != "ended":
                        page = await _until_stopped(
                            resources.enter_async_context(
                                open_page(
                                    headless=config.headless,
                                    license_key=config.license_key,
                                    fingerprint_seed=stable_fingerprint_seed(config.twin_id),
                                    profile_dir=config.browser_profile_dir,
                                    warmup=config.warmup_browser,
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
                        if page is None:
                            detail = "owner stopped browser startup"
                            await lifecycle.send("owner_stopped", "owner_action", detail)
                        else:
                            worker = _MeetingWorker(
                                config,
                                lifecycle,
                                page,
                                stop_event,
                                signals,
                                sleep,
                                monotonic,
                                open_audio,
                                initial_speech,
                                conversation,
                                faults=faults,
                                journal=journal,
                            )
                            detail = await worker.run()
            except asyncio.CancelledError:
                await lifecycle.send(
                    "cancelled", "owner_action", "session task cancelled; closing browser"
                )
                raise
            except ConversationError as error:
                detail = f"conversation failed: {error}"
                await lifecycle.send("conversation_failed", "system_fault", detail)
            except Exception as error:
                detail = f"browser session failed: {error}"
                await lifecycle.send("browser_failed", "system_fault", detail)
    return _terminal_result(
        lifecycle.state["phase"],
        lifecycle.state["terminal_reason"],
        detail,
        faults,
    )
