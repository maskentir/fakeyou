import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import fakeyou.orchestration.runtime as runtime
from fakeyou.config.identifiers import SessionId
from fakeyou.conversation.session import SessionConfig, SessionResult
from fakeyou.conversation.state import LifecycleState
from fakeyou.meet.meet import CallObservation, JoinResult, LeaveResult
from fakeyou.orchestration.lifecycle import initial_lifecycle_state
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import FakePage, FakePageSession, meet_page
from tests._persistence_fakes import FakeSaverSession

INSTANT_JOIN_RETRY: dict[str, Any] = {
    "join_retry_base_seconds": 0.001,
    "join_retry_cap_seconds": 0.001,
}


def pin_join_jitter(monkeypatch: pytest.MonkeyPatch, jitter: float = 0.0) -> None:
    monkeypatch.setattr(
        "langgraph.pregel._retry.random", SimpleNamespace(uniform=lambda _low, _high: jitter)
    )


def session_config(**overrides: Any) -> SessionConfig:
    values: dict[str, Any] = {
        "owner_id": "owner-1",
        "twin_id": "twin-1",
        "meeting_id": "meeting-1",
        "link": LINK,
        "twin_name": "Twin",
        **INSTANT_JOIN_RETRY,
    }
    values.update(overrides)
    return SessionConfig(**values)


async def run_session(
    config: SessionConfig,
    browser: FakePageSession,
    saver: FakeSaverSession,
    clock: FakeClock,
    stop: asyncio.Event | None = None,
) -> SessionResult:
    return await run_meeting_session(
        config,
        SessionWiring(
            stop=stop,
            open_page=browser.open,
            saver_factory=saver.factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        ),
    )


def live_state(phase: str) -> LifecycleState:
    return cast(
        LifecycleState,
        {**initial_lifecycle_state("owner-1", "twin-1", "meeting-1"), "phase": phase},
    )


def mining_payload() -> dict[str, Any]:
    return {
        "tiles": [{"key": "a", "label": "Budi", "self": False, "speaking": True}],
        "people": [],
        "selfSeen": False,
    }


def audio_session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    events = []
    stop = asyncio.Event()

    @asynccontextmanager
    async def page(**kwargs: Any) -> AsyncIterator[FakePage]:
        del kwargs
        yield meet_page([])
        events.append("browser_closed")

    @asynccontextmanager
    async def saver() -> AsyncIterator[InMemorySaver]:
        yield InMemorySaver()

    async def join(*args: Any) -> JoinResult:
        del args
        events.append("admitted")
        return JoinResult("admitted", "admitted")

    async def microphone(*args: Any) -> bool:
        del args
        events.append("microphone")
        return True

    async def observe(*args: Any) -> CallObservation:
        del args
        stop.set()
        return CallObservation("in_call", "in call")

    async def leave(*args: Any) -> LeaveResult:
        del args
        events.append("leave")
        return LeaveResult("left", "left")

    async def camera(*args: Any) -> bool:
        del args
        events.append("camera")
        return True

    monkeypatch.setattr(runtime, "request_guest_join", join)
    monkeypatch.setattr(runtime, "enable_meeting_microphone", microphone, raising=False)
    monkeypatch.setattr(runtime, "disable_meeting_camera", camera, raising=False)
    monkeypatch.setattr(runtime, "observe_call_state", observe)
    monkeypatch.setattr(runtime, "leave_meeting", leave)
    config = SessionConfig(
        SessionId("owner"),
        SessionId("twin"),
        SessionId("meeting"),
        "https://meet.google.com/abc-defg-hij",
        "Twin",
    )
    return SimpleNamespace(
        events=events,
        stop=stop,
        config=config,
        kwargs={"open_page": page, "saver_factory": saver, "stop": stop},
    )
