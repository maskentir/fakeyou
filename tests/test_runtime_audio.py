import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import fakeyou.runtime as runtime
from fakeyou.audio import MeetingAudioError
from fakeyou.meet import CallObservation, JoinResult, LeaveResult
from fakeyou.session import ConversationError, SessionConfig


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

    async def camera(*args):
        events.append("camera")
        return True

    monkeypatch.setattr(runtime, "request_guest_join", join)
    monkeypatch.setattr(runtime, "enable_meeting_microphone", microphone, raising=False)
    monkeypatch.setattr(runtime, "disable_meeting_camera", camera, raising=False)
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
        self.reconnect_requests: list[str] = []

    @property
    def is_speaking(self):
        return False

    def request_stt_reconnect(self, reason: str) -> None:
        self.reconnect_requests.append(reason)

    def reconnect_state(self) -> str:
        return "queued" if self.reconnect_requests else "idle"

    async def speak(self, text):
        self.events.append(f"speak:{text}")

    async def stop_playback(self):
        self.events.append("playback_stopped")

    async def stop(self):
        if not self.stopped:
            self.events.append("audio_stopped")
            self.stopped = True

    def raise_if_failed(self):
        pass

    async def wait_failed(self):
        await asyncio.Event().wait()


class Conversation:
    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail
        self.bound = None
        self.closed = False

    def bind(self, session):
        self.bound = session
        self.events.append("conversation_bound")

    async def close(self):
        if not self.closed:
            self.events.append("conversation_closed")
            self.closed = True

    def raise_if_failed(self):
        if self.fail:
            raise ConversationError("conversation failed")


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
        "camera",
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
    assert session.events == [
        "admitted",
        "camera",
        "audio_start_cancelled",
        "leave",
        "browser_closed",
    ]


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


async def test_camera_failure_is_recorded_without_failing_session(session, monkeypatch):
    from fakeyou.faults import FaultReporter

    async def camera(*args):
        raise RuntimeError("no toggle")

    monkeypatch.setattr(runtime, "disable_meeting_camera", camera)
    reporter = FaultReporter(meeting_id="meeting")

    @asynccontextmanager
    async def open_audio():
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, faults=reporter, **session.kwargs
    )
    assert result.terminal_reason != "audio_failure"
    assert "leave" in session.events
    assert any(entry.source == "camera" for entry in reporter.summaries())


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


async def test_conversation_binds_after_audio_and_closes_before_leave(session):
    @asynccontextmanager
    async def open_audio():
        session.events.append("audio_started")
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    conversation = Conversation(session.events)
    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, conversation=conversation, **session.kwargs
    )

    assert result.terminal_reason == "admitted_then_stopped"
    assert conversation.bound is not None
    assert session.events == [
        "admitted",
        "camera",
        "audio_started",
        "microphone",
        "conversation_bound",
        "conversation_closed",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_conversation_failure_keeps_listening_until_owner_stop(session):
    @asynccontextmanager
    async def open_audio():
        session.events.append("audio_started")
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    conversation = Conversation(session.events, fail=True)
    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, conversation=conversation, **session.kwargs
    )

    assert result.terminal_reason == "admitted_then_stopped"
    assert "left" in result.detail
    assert session.events == [
        "admitted",
        "camera",
        "audio_started",
        "microphone",
        "conversation_bound",
        "conversation_closed",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_owner_stop_interrupts_blocked_initial_speech(session):
    class BlockingAudio(Audio):
        async def speak(self, text):
            self.events.append("greeting_started")
            session.stop.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.events.append("greeting_cancelled")

    @asynccontextmanager
    async def open_audio():
        audio = BlockingAudio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    conversation = Conversation(session.events)
    result = await asyncio.wait_for(
        runtime.run_meeting_session(
            session.config,
            open_audio=open_audio,
            initial_speech="Hello",
            conversation=conversation,
            **session.kwargs,
        ),
        timeout=1,
    )

    assert result.terminal_reason == "admitted_then_stopped"
    assert conversation.bound is None
    assert session.events == [
        "admitted",
        "camera",
        "microphone",
        "greeting_started",
        "greeting_cancelled",
        "conversation_closed",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_conversation_requires_audio(session):
    with pytest.raises(ValueError, match="conversation requires meeting audio"):
        await runtime.run_meeting_session(
            session.config, conversation=Conversation(session.events), **session.kwargs
        )


async def test_conversation_closes_without_audio_start_after_denial(session, monkeypatch):
    async def denied(*args):
        return JoinResult("denied", "denied")

    monkeypatch.setattr(runtime, "request_guest_join", denied)

    @asynccontextmanager
    async def open_audio():
        pytest.fail("denied guest must not start audio")
        yield

    conversation = Conversation(session.events)
    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, conversation=conversation, **session.kwargs
    )

    assert result.terminal_reason == "denied_by_host"
    assert session.events == ["conversation_closed", "browser_closed"]


async def test_conversation_closes_if_checkpoint_setup_fails(session):
    def broken_saver():
        raise RuntimeError("checkpoint unavailable")

    @asynccontextmanager
    async def open_audio():
        pytest.fail("audio must not start before the checkpoint")
        yield

    conversation = Conversation(session.events)
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await runtime.run_meeting_session(
            session.config,
            open_audio=open_audio,
            conversation=conversation,
            saver_factory=broken_saver,
            open_page=session.kwargs["open_page"],
            stop=session.stop,
        )

    assert session.events == ["conversation_closed"]


async def test_conversation_closes_before_audio_on_unexpected_monitor_error(session, monkeypatch):
    async def broken_observation(*args):
        raise RuntimeError("browser observation failed")

    monkeypatch.setattr(runtime, "observe_call_state", broken_observation)

    @asynccontextmanager
    async def open_audio():
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    conversation = Conversation(session.events)
    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, conversation=conversation, **session.kwargs
    )

    assert result.terminal_reason == "browser_failure"
    assert session.events.index("conversation_closed") < session.events.index("audio_stopped")


def _faulty_reporter():
    from fakeyou.faults import FaultReporter
    from fakeyou.provider_faults import ProviderFault

    reporter = FaultReporter(meeting_id="meeting")
    reporter.record(ProviderFault("stt", "timeout", detail="slow", retryable=True))
    reporter.record(ProviderFault("stt", "timeout", detail="slow", retryable=True))
    return reporter


class _ScriptedMaintenanceAudio(Audio):
    """Audio fake whose maintenance_tick follows a script.

    Each entry is ("repair", verdict) to queue a reconnect and return the
    verdict, or ("terminal", detail) to raise MeetingAudioError. Once the
    script is exhausted the fake keeps returning "hearing" without new requests.
    """

    def __init__(self, events, script):
        super().__init__(events)
        self._script = list(script)
        self.tick_calls = 0

    async def maintenance_tick(self, *, is_busy=False, remote_participants=None):
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


async def test_finish_merges_fault_suffix_on_audio_failure(session):
    from fakeyou.faults import FaultReporter

    reporter = _faulty_reporter()

    @asynccontextmanager
    async def open_audio():
        raise MeetingAudioError("startup failed")
        yield

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, faults=reporter, **session.kwargs
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail == "startup failed [stt_degraded: timeoutx2]"
    assert result.faults == reporter.report()
    assert isinstance(reporter, FaultReporter)


async def test_finish_merges_fault_suffix_on_leave_path(session):
    reporter = _faulty_reporter()

    @asynccontextmanager
    async def open_audio():
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, faults=reporter, **session.kwargs
    )

    assert result.terminal_reason == "admitted_then_stopped"
    assert result.detail == "left [stt_degraded: timeoutx2]"


async def test_empty_faults_leave_detail_byte_identical(session):
    from fakeyou.faults import FaultReporter

    @asynccontextmanager
    async def open_audio():
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config,
        open_audio=open_audio,
        faults=FaultReporter(meeting_id="meeting"),
        **session.kwargs,
    )

    assert result.detail == "left"


async def test_broken_reporter_never_changes_exit_path(session):
    class _BrokenReporter:
        def report(self):
            raise RuntimeError("reporter exploded")

    @asynccontextmanager
    async def open_audio():
        raise MeetingAudioError("startup failed")
        yield

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, faults=_BrokenReporter(), **session.kwargs
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail == "startup failed"
    assert result.faults is None


async def test_preflight_all_fail_refuses_before_browser(session):
    from fakeyou.preflight import ProbeResult

    opened = []

    @asynccontextmanager
    async def counting_page(**kwargs):
        opened.append(kwargs)
        yield SimpleNamespace()

    async def all_fail():
        return [
            ProbeResult("api.openai.com", False, 1.0, "down"),
            ProbeResult("api.meta.ai", False, 1.0, "down"),
        ]

    @asynccontextmanager
    async def open_audio():
        pytest.fail("refused session must not start audio")
        yield

    kwargs = dict(session.kwargs)
    kwargs["open_page"] = counting_page
    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, preflight=all_fail, **kwargs
    )

    assert result.terminal_reason == "infra_failure"
    assert result.detail.startswith("preflight_unreachable:")
    assert opened == []


async def test_preflight_partial_failure_refuses_to_join(session):
    from fakeyou.preflight import ProbeResult

    async def mixed():
        return [
            ProbeResult("api.openai.com", True, 5.0, "ok"),
            ProbeResult("api.meta.ai", False, 5.0, "down"),
        ]

    @asynccontextmanager
    async def open_audio():
        audio = Audio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, preflight=mixed, **session.kwargs
    )

    assert result.terminal_reason == "infra_failure"
    assert "microphone" not in session.events
    assert "admitted" not in session.events


async def test_stop_during_preflight_returns_owner_stop(session):
    opened = []

    @asynccontextmanager
    async def counting_page(**kwargs):
        opened.append(kwargs)
        yield SimpleNamespace()

    async def hanging():
        session.stop.set()
        await asyncio.Event().wait()
        return []

    kwargs = dict(session.kwargs)
    kwargs["open_page"] = counting_page
    result = await runtime.run_meeting_session(session.config, preflight=hanging, **kwargs)

    assert result.terminal_reason == "owner_stop"
    assert opened == []


async def test_deaf_grace_expiry_terminates_with_repair_first(session, monkeypatch):
    async def observe(*args):
        return CallObservation("in_call", "in call")

    monkeypatch.setattr(runtime, "observe_call_state", observe)
    audios: list[_ScriptedMaintenanceAudio] = []

    @asynccontextmanager
    async def open_audio():
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
        session.config, open_audio=open_audio, **session.kwargs
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail.startswith("deaf:")
    assert audios[0].reconnect_requests == ["deaf"]
    assert audios[0].tick_calls >= 2


async def test_deaf_repair_then_hearing_avoids_terminal(session, monkeypatch):
    audios: list[_ScriptedMaintenanceAudio] = []

    async def observe(*args):
        if audios and audios[0].tick_calls >= 2:
            session.stop.set()
        return CallObservation("in_call", "in call")

    monkeypatch.setattr(runtime, "observe_call_state", observe)

    @asynccontextmanager
    async def open_audio():
        audio = _ScriptedMaintenanceAudio(
            session.events, [("repair", "silent"), ("repair", "hearing")]
        )
        audios.append(audio)
        try:
            yield audio
        finally:
            await audio.stop()

    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, **session.kwargs
    )

    assert result.terminal_reason == "admitted_then_stopped"


@pytest.mark.parametrize("leave_raises", [False, True])
async def test_audio_failure_remains_primary_when_leave_also_fails(
    session, monkeypatch, leave_raises
):
    @asynccontextmanager
    async def open_audio():
        raise MeetingAudioError("capture stopped")
        yield

    async def broken_leave(*args):
        if leave_raises:
            raise RuntimeError("leave failed")
        return LeaveResult("failed", "leave failed")

    monkeypatch.setattr(runtime, "leave_meeting", broken_leave)
    result = await runtime.run_meeting_session(
        session.config, open_audio=open_audio, **session.kwargs
    )
    assert result.terminal_reason == "audio_failure"
    assert result.detail == "capture stopped"
