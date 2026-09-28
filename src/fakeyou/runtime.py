import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import Page

from fakeyou.audio import AGENT_MIC_SOURCE_NAME, MEETING_SINK_NAME, MeetingAudioError
from fakeyou.browser import open_direct_page, stable_fingerprint_seed
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
    enable_meeting_microphone,
    leave_meeting,
    observe_call_state,
    request_guest_join,
)
from fakeyou.session import MeetingAudio
from fakeyou.session import SessionConfig as SessionConfig

_JOIN_EVENTS: dict[JoinStatus, tuple[LifecycleEventKind, LifecycleAttribution]] = {
    "admitted": ("admitted", "host_action"),
    "denied": ("denied", "host_action"),
    "blocked": ("blocked", "system_fault"),
    "timeout": ("lobby_timeout", "system_fault"),
    "aborted": ("owner_stop", "user_action"),
    "failed": ("browser_failed", "system_fault"),
}


@dataclass(frozen=True, slots=True)
class SessionResult:
    phase: MeetingPhase
    terminal_reason: TerminalReason | None
    detail: str


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
    audio: MeetingAudio | None = None

    async def run(self) -> str:
        detail = await self.join()
        if self.lifecycle.state["phase"] == "admitted":
            detail = await self.run_admitted(self.monotonic())
        return detail

    async def run_admitted(self, admitted_at: float) -> str:
        if self.open_audio is None:
            return await self.monitor(admitted_at)
        try:
            async with AsyncExitStack() as resources:
                self.audio = await _until_stopped(
                    resources.enter_async_context(self.open_audio()), self.stop
                )
                if self.audio is None:
                    return await self.finish(
                        "owner_stop", "user_action", "owner stopped audio startup"
                    )
                try:
                    enabled = await _until_stopped(
                        enable_meeting_microphone(self.page, self.sleep), self.stop
                    )
                except Exception as error:
                    raise MeetingAudioError("meeting microphone could not be enabled") from error
                if enabled is None or self.stop.is_set():
                    return await self.finish(
                        "owner_stop", "user_action", "owner stopped microphone startup"
                    )
                self.audio.raise_if_failed()
                if self.initial_speech:
                    await self.audio.speak(self.initial_speech)
                return await self.monitor(admitted_at)
        except MeetingAudioError as error:
            self.audio = None
            return await self.finish("audio_failed", "system_fault", str(error))
        finally:
            self.audio = None

    async def join(self) -> str:
        if self.stop.is_set():
            return await self.finish("owner_stop", "user_action", "owner stopped before guest join")
        await self.lifecycle.send("join_requested", "user_action", "worker requested guest join")
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
            return await self.finish("owner_stop", "user_action", "owner stopped guest join")
        kind, attribution = _JOIN_EVENTS[result.status]
        await self.lifecycle.send(kind, attribution, result.detail)
        return result.detail

    async def monitor(self, admitted_at: float) -> str:
        deadline = admitted_at + self.config.session_duration_seconds
        while True:
            if self.audio is not None:
                self.audio.raise_if_failed()
            if self.stop.is_set():
                return await self.finish("owner_stop", "user_action", "owner requested stop")
            if self.monotonic() >= deadline:
                return await self.finish(
                    "duration_limit", "system_fault", "session duration reached"
                )
            observation = await observe_call_state(self.page, self.signals)
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
            await self.wait(min(self.config.monitor_interval_seconds, deadline - self.monotonic()))

    async def wait(self, seconds: float) -> None:
        async def delay() -> None:
            await self.sleep(max(0, seconds))

        await _until_stopped(delay(), self.stop)

    async def finish(
        self, kind: LifecycleEventKind, attribution: LifecycleAttribution, detail: str
    ) -> str:
        if self.audio is not None:
            await self.audio.stop()
        await self.lifecycle.send(kind, attribution, detail)
        if self.lifecycle.state["phase"] != "leaving":
            return detail
        left = await leave_meeting(
            self.page, self.signals, self.sleep, self.monotonic, self.config.leave_timeout_seconds
        )
        await self.lifecycle.send(
            "leave_sent" if left.status == "left" else "browser_failed",
            attribution if left.status == "left" else "system_fault",
            left.detail,
        )
        return detail if kind == "audio_failed" else left.detail


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
) -> SessionResult:
    if initial_speech is not None and open_audio is None:
        raise ValueError("initial speech requires meeting audio")
    stop_event = stop if stop is not None else asyncio.Event()
    if saver_factory is None:
        config.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        saver_context: AbstractAsyncContextManager[BaseCheckpointSaver[Any]] = (
            AsyncSqliteSaver.from_conn_string(str(config.checkpoint_path))
        )
    else:
        saver_context = saver_factory()
    async with AsyncExitStack() as resources:
        saver = await resources.enter_async_context(saver_context)
        lifecycle = MeetingLifecycle(config.owner_id, config.twin_id, config.meeting_id, saver)
        await lifecycle.initialize()
        detail = "session already ended"
        if lifecycle.state["phase"] != "ended":
            try:
                if stop_event.is_set():
                    detail = "owner requested stop before startup"
                    await lifecycle.send("owner_stop", "user_action", detail)
                else:
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
                        await lifecycle.send("owner_stop", "user_action", detail)
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
                        )
                        detail = await worker.run()
            except asyncio.CancelledError:
                await lifecycle.send(
                    "cancelled", "user_action", "session task cancelled; closing browser"
                )
                raise
            except Exception as error:
                detail = f"browser session failed: {error}"
                await lifecycle.send("browser_failed", "system_fault", detail)
        return SessionResult(
            phase=lifecycle.state["phase"],
            terminal_reason=lifecycle.state["terminal_reason"],
            detail=detail,
        )
