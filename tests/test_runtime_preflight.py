import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import fakeyou.orchestration.runtime as runtime
from fakeyou.audio.audio import MeetingAudioError
from fakeyou.meet.meet import CallObservation
from tests._audio_fakes import FakeAudio
from tests._runtime_support import audio_session


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return audio_session(monkeypatch)


class _ScriptedMaintenanceAudio(FakeAudio):
    def __init__(self, events: list[str], script: Iterable[tuple[str, str]]) -> None:
        super().__init__(events)
        self._script = list(script)
        self.tick_calls = 0

    async def maintenance_tick(
        self, *, is_busy: bool = False, remote_participants: int | None = None
    ) -> str:
        del is_busy
        del remote_participants
        self.tick_calls += 1
        if self._script:
            action, outcome = self._script.pop(0)
        else:
            action, outcome = ("steady", "hearing")
        if action == "terminal":
            raise MeetingAudioError(outcome)
        if action == "repair":
            self.request_stt_reconnect("deaf")
        return outcome


async def test_preflight_all_fail_refuses_before_browser(session: SimpleNamespace) -> None:
    from fakeyou.orchestration.preflight import ProbeResult

    opened = []

    @asynccontextmanager
    async def counting_page(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        opened.append(kwargs)
        yield SimpleNamespace()

    async def all_fail() -> list[ProbeResult]:
        return [
            ProbeResult("api.openai.com", ok=False, latency_ms=1.0, detail="down"),
            ProbeResult("api.meta.ai", ok=False, latency_ms=1.0, detail="down"),
        ]

    @asynccontextmanager
    async def open_audio() -> AsyncIterator[FakeAudio]:
        pytest.fail("refused session must not start audio")
        yield

    kwargs = dict(session.kwargs)
    kwargs["open_page"] = counting_page
    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(open_audio=open_audio, preflight=all_fail, **kwargs)
    )

    assert result.terminal_reason == "infra_failure"
    assert result.detail.startswith("preflight_unreachable:")
    assert opened == []


async def test_preflight_partial_failure_refuses_to_join(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:
    from fakeyou.orchestration.preflight import ProbeResult

    async def mixed() -> list[ProbeResult]:
        return [
            ProbeResult("api.openai.com", ok=True, latency_ms=5.0, detail="ok"),
            ProbeResult("api.meta.ai", ok=False, latency_ms=5.0, detail="down"),
        ]

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, preflight=mixed, **session.kwargs),
    )

    assert result.terminal_reason == "infra_failure"
    assert "microphone" not in session.events
    assert "admitted" not in session.events


async def test_stop_during_preflight_returns_owner_stop(session: SimpleNamespace) -> None:
    opened = []

    @asynccontextmanager
    async def counting_page(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        opened.append(kwargs)
        yield SimpleNamespace()

    async def hanging() -> list[Any]:
        session.stop.set()
        await asyncio.Event().wait()
        return []

    kwargs = dict(session.kwargs)
    kwargs["open_page"] = counting_page
    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(preflight=hanging, **kwargs)
    )

    assert result.terminal_reason == "stopped_by_owner"
    assert opened == []


async def test_deaf_grace_expiry_terminates_with_repair_first(
    session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def observe(*args: Any) -> CallObservation:
        del args
        return CallObservation("in_call", "in call")

    monkeypatch.setattr(runtime, "observe_call_state", observe)
    audios: list[_ScriptedMaintenanceAudio] = []

    @asynccontextmanager
    async def open_audio() -> AsyncIterator[FakeAudio]:
        audio = _ScriptedMaintenanceAudio(
            session.events,
            [("repair", "silent"), ("terminal", "deaf: persistent stt_fault")],
        )
        audios.append(audio)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(open_audio=open_audio, **session.kwargs)
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail.startswith("deaf:")
    assert audios[0].reconnect_requests == ["deaf"]
    assert audios[0].tick_calls >= 2


async def test_deaf_repair_then_hearing_avoids_terminal(
    session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    audios: list[_ScriptedMaintenanceAudio] = []

    async def observe(*args: Any) -> CallObservation:
        del args
        if audios and audios[0].tick_calls >= 2:
            session.stop.set()
        return CallObservation("in_call", "in call")

    monkeypatch.setattr(runtime, "observe_call_state", observe)

    @asynccontextmanager
    async def open_audio() -> AsyncIterator[FakeAudio]:
        audio = _ScriptedMaintenanceAudio(
            session.events, [("repair", "silent"), ("repair", "hearing")]
        )
        audios.append(audio)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(open_audio=open_audio, **session.kwargs)
    )

    assert result.terminal_reason == "stopped_by_owner"


async def test_preflight_bug_reaches_the_session_boundary(session: SimpleNamespace) -> None:
    from fakeyou.orchestration.preflight import ProbeResult

    async def boom() -> list[ProbeResult]:
        raise ValueError("bug")

    @asynccontextmanager
    async def refused_page(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        del kwargs
        pytest.fail("preflight bug must not open the browser")
        yield SimpleNamespace()

    kwargs = dict(session.kwargs)
    kwargs["open_page"] = refused_page
    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(preflight=boom, **kwargs)
    )

    assert result.terminal_reason == "browser_failure"
    assert result.detail == "browser session failed (ValueError)"
