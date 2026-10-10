import asyncio
from typing import Any

import pytest
from websockets.protocol import State

import fakeyou.audio.audio as audio
from fakeyou.audio.audio import (
    ManagedMuseSTT,
    MeetingAudioError,
    MeetingAudioSession,
    run_stt_reconnect,
    stt_ready,
    stt_reconnect_supported,
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.audio.voice import create_muse_stt
from tests.fakes import FakeReconnectableSTT, LegacyRawReconnectSTT, LegacySTT


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


class _FailingReconnectSTT:
    async def reconnect_when_idle(self) -> None:
        raise ConnectionError("socket gone")


class _ReadySTT:
    def __init__(self, ready: asyncio.Event) -> None:
        self._session_ready = ready


def _quiet_session(
    stt: Any, *, stt_idle_reconnect_seconds: float, idle_seconds: float
) -> MeetingAudioSession:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    stt.idle_seconds = lambda now: idle_seconds
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=stt,
        activity=activity,
        now=lambda: clock[0],
        stt_idle_reconnect_seconds=stt_idle_reconnect_seconds,
    )
    clock[0] += 10.0
    activity.observe_audio()
    session._test_gate = gate
    session._test_runner = runner_task
    return session


async def _close_quiet_session(session: MeetingAudioSession) -> None:
    session._test_gate.set()
    await session._test_runner


def _speech_chunk(index: int, size: int = 320) -> bytes:
    return bytes([index % 256]) * size


def test_pipecat_1_12_0_meta_stt_exposes_reconnect_coroutine() -> None:
    stt = create_muse_stt(api_key="test-meta-key")

    assert hasattr(stt, "_reconnect")
    assert asyncio.iscoroutinefunction(stt._reconnect)


def test_managed_muse_stt_exposes_policy_controlled_reconnect() -> None:
    stt = create_muse_stt(api_key="test-meta-key", service_class=ManagedMuseSTT)

    assert asyncio.iscoroutinefunction(stt.reconnect_when_idle)
    assert stt_reconnect_supported(stt) is True


def test_reconnect_guard_accepts_only_policy_controlled_reconnect() -> None:
    assert stt_reconnect_supported(FakeReconnectableSTT()) is True
    assert stt_reconnect_supported(LegacyRawReconnectSTT()) is False
    assert stt_reconnect_supported(LegacySTT()) is False
    assert stt_reconnect_supported(None) is False


async def test_frozen_reconnect_path_calls_fake_stt() -> None:
    stt = FakeReconnectableSTT()

    await run_stt_reconnect(stt)

    assert stt.calls == 1


async def test_frozen_reconnect_path_rejects_legacy_stt() -> None:
    with pytest.raises(AttributeError):
        await run_stt_reconnect(LegacySTT())


async def test_managed_stt_preserves_dead_window_audio_and_replays_in_order() -> None:
    stt = create_muse_stt(api_key="test-meta-key", service_class=ManagedMuseSTT)
    stt._sample_rate = 16000
    dead = _FakeSocket(State.CLOSED)
    stt._websocket = dead
    stt._session_ready.set()
    first = _speech_chunk(1)
    second = _speech_chunk(2)
    async for _ in stt.run_stt(first):
        pass
    async for _ in stt.run_stt(second):
        pass
    assert dead.sent == []
    live = _FakeSocket(State.OPEN)
    stt._websocket = live
    third = _speech_chunk(3)
    async for _ in stt.run_stt(third):
        pass
    assert live.sent == [first, second, third]


async def test_managed_stt_bounds_dead_window_buffer() -> None:
    stt = create_muse_stt(api_key="test-meta-key", service_class=ManagedMuseSTT)
    stt._sample_rate = 16000
    stt._websocket = _FakeSocket(State.CLOSED)
    stt._session_ready.set()
    chunks = [_speech_chunk(index, 32000) for index in range(120)]
    for chunk in chunks:
        async for _ in stt.run_stt(chunk):
            pass
    live = _FakeSocket(State.OPEN)
    stt._websocket = live
    tail = _speech_chunk(200, 320)
    async for _ in stt.run_stt(tail):
        pass
    replayed = live.sent[:-1]
    assert replayed
    assert sum(len(part) for part in replayed) <= 60 * 16000 * 2
    assert replayed[-1] == chunks[-1]
    assert chunks[0] not in replayed


def test_stt_socket_dead_reports_only_confirmed_dead_sockets() -> None:
    assert audio.stt_socket_dead(None) is False
    assert audio.stt_socket_dead(LegacySTT()) is False
    assert audio.stt_socket_dead(FakeReconnectableSTT()) is False
    live = FakeReconnectableSTT()
    live._websocket = _FakeSocket(State.OPEN)
    assert audio.stt_socket_dead(live) is False
    dead = FakeReconnectableSTT()
    dead._websocket = _FakeSocket(State.CLOSED)
    assert audio.stt_socket_dead(dead) is True
    closing = FakeReconnectableSTT()
    closing._websocket = _FakeSocket(State.CLOSING)
    assert audio.stt_socket_dead(closing) is True


async def test_dead_socket_reconnect_bypasses_quiet_gate(journal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    activity.observe_speech(True)
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    stt = FakeReconnectableSTT()
    stt._websocket = _FakeSocket(State.CLOSED)
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=stt,
        activity=activity,
        journal=journal,
        now=lambda: clock[0],
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 1.0
        await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task
    assert stt.calls == 1
    assert session.reconnect_state() == "idle"


async def test_healthy_socket_reconnect_keeps_quiet_gate(journal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    activity.observe_speech(True)
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    stt = FakeReconnectableSTT()
    stt._websocket = _FakeSocket(State.OPEN)
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=stt,
        activity=activity,
        journal=journal,
        now=lambda: clock[0],
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


async def test_reconnect_journal_events_carry_stream_id(journal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    stt = FakeReconnectableSTT()
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=stt,
        activity=activity,
        journal=journal,
        now=lambda: clock[0],
        stream_id="slot-2",
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
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


async def test_reconnect_failed_journal_event_carries_stream_id(journal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=_FailingReconnectSTT(),
        activity=activity,
        journal=journal,
        now=lambda: clock[0],
        stream_id="slot-1",
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task
    rows = journal.events()
    failed = [row for row in rows if row["event"] == "stt_reconnect_failed"]
    assert len(failed) == 1
    assert failed[0]["stream_id"] == "slot-1"
    assert session.reconnect_state() == "queued"


async def test_reconnect_journal_events_omit_missing_stream_id(journal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    stt = FakeReconnectableSTT()
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=stt,
        activity=activity,
        journal=journal,
        now=lambda: clock[0],
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task
    rows = journal.events()
    assert rows
    assert all("stream_id" not in row for row in rows)


async def test_proactive_reconnect_requests_only_when_idle_and_quiet() -> None:
    stt = FakeReconnectableSTT()
    session = _quiet_session(stt, stt_idle_reconnect_seconds=60.0, idle_seconds=61.0)
    try:
        await session.maintenance_tick(is_busy=False, remote_participants=2)
    finally:
        await _close_quiet_session(session)
    assert session.reconnect_state() in ("queued", "idle")
    assert stt.calls >= 1


async def test_proactive_reconnect_stays_off_by_default() -> None:
    stt = FakeReconnectableSTT()
    session = _quiet_session(stt, stt_idle_reconnect_seconds=0.0, idle_seconds=3600.0)
    try:
        await session.maintenance_tick(is_busy=False, remote_participants=2)
    finally:
        await _close_quiet_session(session)
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
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    stt = FakeReconnectableSTT()
    stt._websocket = _FakeSocket(State.CLOSED)
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=stt,
        activity=activity,
        now=lambda: clock[0],
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


async def test_admit_resets_stale_queued_reconnect_epoch(journal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    stt = FakeReconnectableSTT()
    stt._websocket = _FakeSocket(State.CLOSED)
    session = MeetingAudioSession(
        object(),
        object(),
        runner_task,
        _Playback(),
        stt=stt,
        activity=activity,
        journal=journal,
        now=lambda: clock[0],
    )
    try:
        session.request_stt_reconnect("native_request")
        clock[0] += 130.0
        session.mark_admitted()
        await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task
    assert stt.calls == 1
    assert session._reconnect_attempts == 1
    assert session.reconnect_state() == "idle"
    rows = journal.events()
    finished = [row for row in rows if row["event"] == "stt_reconnect_finished"]
    assert len(finished) == 1
    assert finished[0]["attempts"] == 1
