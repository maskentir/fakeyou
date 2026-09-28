import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import fakeyou.runtime as runtime
from fakeyou.audio import MeetingAudioError
from fakeyou.meet import CallObservation, JoinResult, LeaveResult
from fakeyou.session import SessionConfig


@pytest.fixture
def session(tmp_path, monkeypatch):
    events = []
    stop = asyncio.Event()

    @asynccontextmanager
    async def page(**kwargs):
        yield SimpleNamespace()
        events.append("browser_closed")

    @asynccontextmanager
    async def saver():
        yield InMemorySaver()

    async def join(*args):
        events.append("admitted")
        return JoinResult("admitted", "admitted")

    async def microphone(*args):
        events.append("microphone")
        return True

    async def observe(*args):
        stop.set()
        return CallObservation("in_call", "in call")

    async def leave(*args):
        events.append("leave")
        return LeaveResult("left", "left")

    monkeypatch.setattr(runtime, "request_guest_join", join)
    monkeypatch.setattr(runtime, "enable_meeting_microphone", microphone, raising=False)
    monkeypatch.setattr(runtime, "observe_call_state", observe)
    monkeypatch.setattr(runtime, "leave_meeting", leave)
    config = SessionConfig(
        "owner", "twin", "meeting", "https://meet.google.com/abc-defg-hij", "Twin"
    )
    return SimpleNamespace(
        events=events,
        stop=stop,
        config=config,
        kwargs={"open_page": page, "saver_factory": saver, "stop": stop},
    )


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Audio:
    def __init__(self, events):
        self.events = events
        self.stopped = False

    async def speak(self, text):
        self.events.append(f"speak:{text}")

    async def stop(self):
        if not self.stopped:
            self.events.append("audio_stopped")
            self.stopped = True

    def raise_if_failed(self):
        pass

    async def wait_failed(self):
        await asyncio.Event().wait()


async def test_audio_starts_after_admission_and_stops_before_leave(session):
    @asynccontextmanager
    async def open_audio():
        session.events.append("audio_started")
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, initial_speech="Hello", **session.kwargs
    )

    assert result.terminal_reason == "admitted_then_stopped"
    assert session.events == [
        "admitted",
        "audio_started",
        "microphone",
        "speak:Hello",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_denied_guest_never_starts_audio(session, monkeypatch):
    async def denied(*args):
        return JoinResult("denied", "denied")

    monkeypatch.setattr(runtime, "request_guest_join", denied)

    @asynccontextmanager
    async def open_audio():
        pytest.fail("denied guest must not start audio")
        yield

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, **session.kwargs
    )
    assert result.terminal_reason == "denied_by_host"


async def test_owner_stop_cancels_audio_startup_and_leaves(session):
    @asynccontextmanager
    async def open_audio():
        try:
            session.stop.set()
            await asyncio.Event().wait()
            yield Audio(session.events)
        finally:
            session.events.append("audio_start_cancelled")

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, **session.kwargs
    )
    assert result.terminal_reason == "admitted_then_stopped"
    assert session.events == ["admitted", "audio_start_cancelled", "leave", "browser_closed"]


@pytest.mark.parametrize("failure_at", ["startup", "monitor", "microphone"])
async def test_audio_failure_stops_pipeline_and_leaves_with_distinct_reason(
    session, monkeypatch, failure_at
):
    class FailedAudio(Audio):
        def raise_if_failed(self):
            if failure_at == "monitor":
                raise MeetingAudioError("provider failed")

    @asynccontextmanager
    async def open_audio():
        if failure_at == "startup":
            raise MeetingAudioError("startup failed")
        audio = FailedAudio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    async def microphone(*args):
        raise RuntimeError("not enabled")

    if failure_at == "microphone":
        monkeypatch.setattr(runtime, "enable_meeting_microphone", microphone)
    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, **session.kwargs
    )
    assert result.terminal_reason == "audio_failure"
    assert session.events[-2:] == ["leave", "browser_closed"]
    if failure_at != "startup":
        assert session.events.index("audio_stopped") < session.events.index("leave")


async def test_session_duration_counts_from_admission(session, monkeypatch):
    clock = Clock()

    async def observe(*args):
        return CallObservation("in_call", "in call")

    monkeypatch.setattr(runtime, "observe_call_state", observe)

    @asynccontextmanager
    async def open_audio():
        await clock.sleep(50.0)
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    config = SessionConfig(
        "owner",
        "twin",
        "meeting",
        "https://meet.google.com/abc-defg-hij",
        "Twin",
        session_duration_seconds=100.0,
        monitor_interval_seconds=10.0,
    )
    result = await runtime.run_meeting_session(
        config,
        open_audio=open_audio,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **session.kwargs,
    )
    assert result.terminal_reason == "duration_limit"
    assert sum(clock.sleeps) == 100.0
