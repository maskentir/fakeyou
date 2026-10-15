import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast, override

import pytest
from pipecat.pipeline.worker import PipelineWorker
from pipecat.services.meta.stt import MetaSTTService
from pipecat.workers.runner import WorkerRunner
from websockets.protocol import State

import fakeyou.audio.audio as audio
from fakeyou.audio.audio import (
    ManagedMuseSTT,
    MeetingAudioError,
    MeetingAudioSession,
    SessionCapture,
    SessionPipeline,
    SessionRuntime,
    run_stt_reconnect,
    stt_ready,
    stt_reconnect_supported,
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.audio.playback import PlaybackPort
from fakeyou.audio.voice import create_muse_stt
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReconnectableSTT, NonReconnectableSTT, RawReconnectOnlySTT
from tests._capture_support import settle_reconnect
from tests._persistence_fakes import RecordingJournal


class _FakeSocket:
    def __init__(self, state: State) -> None:
        self.state = state
        self.sent: list[Any] = []

    async def send(self, data: Any) -> None:
        self.sent.append(data)


class _Playback:
    @property
    def is_speaking(self) -> bool:
        return False

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        del sentence_index
        del transcript_id
        del text
        return None

    async def stop(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _pipeline(engine: PlaybackPort, runner_task: asyncio.Task[None]) -> SessionPipeline:
    worker = cast(PipelineWorker, object())
    runner = cast(WorkerRunner, object())
    return SessionPipeline(worker, runner, runner_task, engine)


async def _wait_gate(gate: asyncio.Event) -> None:
    await gate.wait()


class _FailingReconnectSTT:
    async def reconnect_when_idle(self) -> None:
        raise ConnectionError("socket gone")


class _ReadySTT:
    def __init__(self, ready: asyncio.Event) -> None:
        self.session_ready = ready


class _AttachedSTT(ManagedMuseSTT):
    @property
    @override
    def sample_rate(self) -> int:
        return 16000

    def attach(self, socket: _FakeSocket) -> None:
        self._websocket = socket
        self._session_ready.set()


class _HandshakeSTT(ManagedMuseSTT):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cycle: list[str] = []

    @override
    async def _connect_websocket(self) -> None:
        self.cycle.append("connect")
        self._session_ready.set()

    @override
    async def _disconnect_websocket(self) -> None:
        self.cycle.append("disconnect")
        self._session_ready.clear()


class _SocketSTT(FakeReconnectableSTT):
    def __init__(self, *, socket_dead: bool) -> None:
        super().__init__()
        self.socket_dead = socket_dead


def _attached_stt(socket: _FakeSocket | None = None) -> _AttachedSTT:
    stt = create_muse_stt(api_key="test-meta-key", service_class=_AttachedSTT)
    assert isinstance(stt, _AttachedSTT)
    if socket is not None:
        stt.attach(socket)
    return stt


class _GatedReconnectSTT:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()
        self.socket_dead = True

    async def reconnect_when_idle(self) -> None:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.finished.set()


@asynccontextmanager
async def _quiet_session(
    stt: Any, *, stt_idle_reconnect_seconds: float, idle_seconds: float
) -> AsyncIterator[MeetingAudioSession]:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    stt.idle_seconds = lambda _now: idle_seconds
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        runtime=SessionRuntime(
            now=lambda: clock[0], stt_idle_reconnect_seconds=stt_idle_reconnect_seconds
        ),
    )
    clock[0] += 10.0
    activity.observe_audio()
    try:
        yield session
    finally:
        gate.set()
        await runner_task


def _speech_chunk(index: int, size: int = 320) -> bytes:
    return bytes([index % 256]) * size


async def test_reconnect_when_idle_runs_a_full_library_reconnect_cycle() -> None:
    stt = create_muse_stt(api_key="test-meta-key", service_class=_HandshakeSTT)
    assert isinstance(stt, _HandshakeSTT)

    await stt.reconnect_when_idle()

    assert stt.cycle == ["disconnect", "connect"]
    assert stt.session_ready.is_set()


def test_managed_muse_stt_exposes_policy_controlled_reconnect() -> None:
    stt = create_muse_stt(api_key="test-meta-key", service_class=ManagedMuseSTT)
    assert isinstance(stt, ManagedMuseSTT)

    assert asyncio.iscoroutinefunction(stt.reconnect_when_idle)
    assert stt_reconnect_supported(stt) is True


def test_reconnect_guard_accepts_only_policy_controlled_reconnect() -> None:
    assert stt_reconnect_supported(FakeReconnectableSTT()) is True
    assert stt_reconnect_supported(RawReconnectOnlySTT()) is False
    assert stt_reconnect_supported(NonReconnectableSTT()) is False
    assert stt_reconnect_supported(None) is False


async def test_frozen_reconnect_path_calls_fake_stt() -> None:
    stt = FakeReconnectableSTT()

    await run_stt_reconnect(stt)

    assert stt.calls == 1


async def test_frozen_reconnect_path_rejects_non_reconnectable_stt() -> None:
    with pytest.raises(MeetingAudioError):
        await run_stt_reconnect(NonReconnectableSTT())


async def test_managed_stt_preserves_dead_window_audio_and_replays_in_order() -> None:
    dead = _FakeSocket(State.CLOSED)
    stt = _attached_stt(dead)
    first = _speech_chunk(1)
    second = _speech_chunk(2)
    async for _ in stt.run_stt(first):
        pass
    async for _ in stt.run_stt(second):
        pass
    assert dead.sent == []
    live = _FakeSocket(State.OPEN)
    stt.attach(live)
    third = _speech_chunk(3)
    async for _ in stt.run_stt(third):
        pass
    assert live.sent == [first, second, third]


async def test_managed_stt_bounds_dead_window_buffer() -> None:
    stt = _attached_stt(_FakeSocket(State.CLOSED))
    chunks = [_speech_chunk(index, 32000) for index in range(120)]
    for chunk in chunks:
        async for _ in stt.run_stt(chunk):
            pass
    live = _FakeSocket(State.OPEN)
    stt.attach(live)
    tail = _speech_chunk(200, 320)
    async for _ in stt.run_stt(tail):
        pass
    replayed = live.sent[:-1]
    assert replayed
    assert sum(len(part) for part in replayed) <= 60 * 16000 * 2
    assert replayed[-1] == chunks[-1]
    assert chunks[0] not in replayed


async def test_managed_stt_slow_consumer_does_not_trip_frame_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def paced_frames(self: Any, _audio: bytes) -> Any:
        del self
        yield "frame-1"
        yield "frame-2"

    monkeypatch.setattr(audio, "STT_FRAME_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(MetaSTTService, "run_stt", paced_frames)
    stt = _attached_stt(_FakeSocket(State.OPEN))
    seen: list[Any] = []
    async for frame in stt.run_stt(_speech_chunk(1)):
        seen.append(frame)
        await asyncio.sleep(0.2)
    assert seen == ["frame-1", "frame-2"]


async def test_bounded_frames_close_cascades_to_inner_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    async def two_frames(self: Any, _audio: bytes) -> Any:
        del self
        try:
            yield "f1"
            yield "f2"
        finally:
            closed.append(True)

    monkeypatch.setattr(MetaSTTService, "run_stt", two_frames)
    stt = _attached_stt(_FakeSocket(State.OPEN))
    frames = stt.run_stt(_speech_chunk(1))
    first: Any = await frames.__anext__()
    assert first == "f1"
    await frames.aclose()
    assert closed == [True]


async def test_bounded_frames_timeout_leaves_no_suspended_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Any] = []

    async def hanging(self: Any, _audio: bytes) -> Any:
        del self
        await asyncio.sleep(5)
        yield "never"

    def factory(self: Any, audio: bytes) -> Any:
        gen = hanging(self, audio)
        created.append(gen)
        return gen

    monkeypatch.setattr(audio, "STT_FRAME_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(MetaSTTService, "run_stt", factory)
    stt = _attached_stt(_FakeSocket(State.OPEN))
    with pytest.raises(TimeoutError):
        async for _ in stt.run_stt(_speech_chunk(1)):
            pass
    assert inspect.getasyncgenstate(created[0]) == inspect.AGEN_CLOSED


async def test_managed_stt_slow_frame_still_trips_frame_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hanging_frames(self: Any, _audio: bytes) -> Any:
        del self
        await asyncio.sleep(5)
        yield "never"

    monkeypatch.setattr(audio, "STT_FRAME_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(MetaSTTService, "run_stt", hanging_frames)
    stt = _attached_stt(_FakeSocket(State.OPEN))
    with pytest.raises(TimeoutError):
        async for _ in stt.run_stt(_speech_chunk(1)):
            pass


def test_stt_socket_dead_reports_only_confirmed_dead_sockets() -> None:
    assert audio.stt_socket_dead(None) is False
    assert audio.stt_socket_dead(NonReconnectableSTT()) is False
    assert audio.stt_socket_dead(FakeReconnectableSTT()) is False
    assert audio.stt_socket_dead(_attached_stt()) is False
    assert audio.stt_socket_dead(_attached_stt(_FakeSocket(State.OPEN))) is False
    assert audio.stt_socket_dead(_attached_stt(_FakeSocket(State.CLOSED))) is True
    assert audio.stt_socket_dead(_attached_stt(_FakeSocket(State.CLOSING))) is True


async def test_dead_socket_reconnect_bypasses_quiet_gate(journal: RecordingJournal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    activity.observe_speech(active=True)
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = _SocketSTT(socket_dead=True)
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 1.0
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
    finally:
        gate.set()
        await runner_task
    assert stt.calls == 1
    assert session.reconnect_state() == "idle"


async def test_healthy_socket_reconnect_keeps_quiet_gate(journal: RecordingJournal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    activity.observe_speech(active=True)
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = _SocketSTT(socket_dead=False)
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 1.0
        await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task
    assert stt.calls == 0
    assert session.reconnect_state() == "queued"


async def test_reconnect_journal_events_carry_stream_id(journal: RecordingJournal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = FakeReconnectableSTT()
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0], stream_id="slot-2"),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
    finally:
        gate.set()
        await runner_task
    rows = journal.events()
    queued = [row for row in rows if row["event"] == "stt_reconnect_queued"]
    finished = [row for row in rows if row["event"] == "stt_reconnect_finished"]
    assert len(queued) == 1
    assert len(finished) == 1
    assert queued[0]["stream_id"] == "slot-2"
    assert finished[0]["stream_id"] == "slot-2"


async def test_reconnect_failed_journal_event_carries_stream_id(journal: RecordingJournal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=_FailingReconnectSTT(), activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0], stream_id="slot-1"),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
    finally:
        gate.set()
        await runner_task
    rows = journal.events()
    failed = [row for row in rows if row["event"] == "stt_reconnect_failed"]
    assert len(failed) == 1
    assert failed[0]["stream_id"] == "slot-1"
    assert session.reconnect_state() == "queued"


async def test_reconnect_journal_events_omit_missing_stream_id(journal: RecordingJournal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = FakeReconnectableSTT()
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
    finally:
        gate.set()
        await runner_task
    rows = journal.events()
    assert rows
    assert all("stream_id" not in row for row in rows)


async def test_proactive_reconnect_requests_only_when_idle_and_quiet() -> None:
    stt = FakeReconnectableSTT()
    async with _quiet_session(stt, stt_idle_reconnect_seconds=60.0, idle_seconds=61.0) as session:
        await session.maintenance_tick(is_busy=False, remote_participants=2)
        await settle_reconnect(session)
    assert session.reconnect_state() == "idle"
    assert stt.calls == 1


async def test_proactive_reconnect_stays_off_by_default() -> None:
    stt = FakeReconnectableSTT()
    async with _quiet_session(stt, stt_idle_reconnect_seconds=0.0, idle_seconds=3600.0) as session:
        await session.maintenance_tick(is_busy=False, remote_participants=2)
    assert stt.calls == 0


async def test_stt_ready_reports_duck_typed_session() -> None:
    assert await stt_ready(object(), timeout_seconds=0.1) is True
    ready = asyncio.Event()
    assert await stt_ready(_ReadySTT(ready), timeout_seconds=0.1) is False
    ready.set()
    assert await stt_ready(_ReadySTT(ready), timeout_seconds=0.1) is True


async def test_stale_queued_reconnect_deadline_fails_without_admit() -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = _SocketSTT(socket_dead=True)
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 130.0
        with pytest.raises(MeetingAudioError, match="STT reconnect deadline exceeded"):
            await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task
    assert stt.calls == 0
    assert session.reconnect_state() == "exhausted"


async def test_admit_resets_stale_queued_reconnect_epoch(journal: RecordingJournal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = _SocketSTT(socket_dead=True)
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 130.0
        session.mark_admitted()
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
    finally:
        gate.set()
        await runner_task
    assert stt.calls == 1
    assert session.reconnect_state() == "idle"
    rows = journal.events()
    finished = [row for row in rows if row["event"] == "stt_reconnect_finished"]
    assert len(finished) == 1
    assert finished[0]["attempts"] == 1


def _gated_session(
    stt: _GatedReconnectSTT, journal: RecordingJournal
) -> tuple[MeetingAudioSession, list[float], asyncio.Task[None], asyncio.Event]:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    session = MeetingAudioSession(
        _pipeline(_Playback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    return session, clock, runner_task, gate


async def test_tick_returns_while_reconnect_runs_in_background(
    journal: RecordingJournal,
) -> None:
    stt = _GatedReconnectSTT()
    session, clock, runner_task, gate = _gated_session(stt, journal)
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 1.0
        await asyncio.wait_for(session.maintenance_tick(is_busy=False), 1.0)
        assert session.reconnect_state() == "running"
        await asyncio.wait_for(stt.started.wait(), 1.0)
        assert stt.calls == 1
        assert not stt.finished.is_set()
        stt.release.set()
        await asyncio.wait_for(stt.finished.wait(), 1.0)
        await settle_reconnect(session)
        assert session.reconnect_state() == "idle"
    finally:
        stt.release.set()
        gate.set()
        await runner_task
    rows = journal.events()
    finished = [row for row in rows if row["event"] == "stt_reconnect_finished"]
    assert len(finished) == 1


async def test_running_reconnect_is_not_spawned_twice(journal: RecordingJournal) -> None:
    stt = _GatedReconnectSTT()
    session, clock, runner_task, gate = _gated_session(stt, journal)
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 1.0
        await asyncio.wait_for(session.maintenance_tick(is_busy=False), 1.0)
        await asyncio.wait_for(stt.started.wait(), 1.0)
        await asyncio.wait_for(session.maintenance_tick(is_busy=False), 1.0)
        assert stt.calls == 1
        assert session.reconnect_state() == "running"
    finally:
        stt.release.set()
        gate.set()
        await runner_task
    await settle_reconnect(session)
    assert session.reconnect_state() == "idle"


async def test_stop_cancels_an_inflight_reconnect(journal: RecordingJournal) -> None:
    class _CancellingRunner:
        def __init__(self, gate: asyncio.Event) -> None:
            self.gate = gate

        async def cancel(self, reason: str) -> None:
            del reason
            self.gate.set()

    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = _GatedReconnectSTT()
    session = MeetingAudioSession(
        SessionPipeline(
            cast(PipelineWorker, object()),
            cast(WorkerRunner, _CancellingRunner(gate)),
            runner_task,
            _Playback(),
        ),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 1.0
        await asyncio.wait_for(session.maintenance_tick(is_busy=False), 1.0)
        await asyncio.wait_for(stt.started.wait(), 1.0)
        await asyncio.wait_for(session.stop(), 5.0)
    finally:
        stt.release.set()
        gate.set()
        await runner_task

    assert not stt.finished.is_set()
    assert session.reconnect_state() == "queued"
    assert journal.events("stt_reconnect_failed") == []


async def test_bounded_frames_aclose_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stt = _attached_stt(_FakeSocket(State.OPEN))

    async def failing_gen() -> Any:
        try:
            yield None
        except GeneratorExit:
            raise OSError("close failed") from None

    monkeypatch.setattr(MetaSTTService, "run_stt", lambda _self, _chunk: failing_gen())
    gen = stt.run_stt(b"\x00" * 16)

    assert await anext(gen) is None
    await gen.aclose()
