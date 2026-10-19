import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, override

import pytest
from pydantic import SecretStr

from fakeyou.audio.audio import (
    MeetingAudioError,
    MeetingAudioSession,
    SessionCapture,
    SessionRuntime,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
    run_stt_reconnect,
    stt_ready,
    stt_reconnect_supported,
    stt_socket_dead,
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.transcription.contract import SpeechSinks, TranscriptionError
from fakeyou.transcription.muse import MuseClient, MuseOptions
from tests._audio_fakes import (
    FakeReconnectableSTT,
    NonReconnectableSTT,
    RawReconnectOnlySTT,
    ScriptedSTT,
)
from tests._audio_support import FakeCapture, FakePlayback, build_run
from tests._capture_support import settle_reconnect
from tests._persistence_fakes import RecordingJournal


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
        del sentence_index, transcript_id, text

    async def stop(self) -> None:
        return None

    async def close(self) -> None:
        return None


async def _wait_gate(gate: asyncio.Event) -> None:
    await gate.wait()


async def _ignore_update(update: TranscriptUpdate) -> None:
    del update


def _ignore_lost(error: TranscriptionError) -> None:
    del error


class _FailingReconnectSTT(FakeReconnectableSTT):
    @override
    async def reconnect_when_idle(self) -> None:
        self.calls += 1
        raise ConnectionError("socket gone")


class _ReadySTT:
    def __init__(self, ready: asyncio.Event) -> None:
        self.session_ready = ready


class _SocketSTT(FakeReconnectableSTT):
    def __init__(self, *, socket_dead: bool) -> None:
        super().__init__()
        self.socket_dead = socket_dead


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
        build_run(_Playback(), runner_task),
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


def test_muse_client_exposes_policy_controlled_reconnect() -> None:
    client = MuseClient(
        api_key="test-meta-key",
        options=MuseOptions(),
        sinks=SpeechSinks(
            on_update=_ignore_update, on_interim=_ignore_update, on_lost=_ignore_lost
        ),
    )

    assert asyncio.iscoroutinefunction(client.reconnect_when_idle)
    assert stt_reconnect_supported(client) is True


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


def test_stt_socket_dead_reports_only_confirmed_dead_sockets() -> None:
    assert stt_socket_dead(None) is False
    assert stt_socket_dead(NonReconnectableSTT()) is False
    assert stt_socket_dead(FakeReconnectableSTT()) is False
    assert stt_socket_dead(_SocketSTT(socket_dead=False)) is False
    assert stt_socket_dead(_SocketSTT(socket_dead=True)) is True


async def test_dead_socket_reconnect_bypasses_quiet_gate(journal: RecordingJournal) -> None:
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    activity.observe_speech(active=True)
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = _SocketSTT(socket_dead=True)
    session = MeetingAudioSession(
        build_run(_Playback(), runner_task),
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
        build_run(_Playback(), runner_task),
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
        build_run(_Playback(), runner_task),
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
        build_run(_Playback(), runner_task),
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
        build_run(_Playback(), runner_task),
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
        build_run(_Playback(), runner_task),
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
        build_run(_Playback(), runner_task),
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
        build_run(_Playback(), runner_task),
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
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(_wait_gate(gate))
    stt = _GatedReconnectSTT()
    session = MeetingAudioSession(
        build_run(_Playback(), runner_task),
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
        await asyncio.gather(runner_task, return_exceptions=True)

    assert not stt.finished.is_set()
    assert session.reconnect_state() == "queued"
    assert journal.events("stt_reconnect_failed") == []


def _scripted_policy() -> StreamPolicy:
    return StreamPolicy(
        speech=SpeechPipelineSettings(
            stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
            tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
        )
    )


def _final(text: str, timestamp: str | None) -> TranscriptUpdate:
    return TranscriptUpdate(text=text, final=True, timestamp=timestamp)


async def test_reconnect_does_not_republish_same_final(journal: RecordingJournal) -> None:
    stt = ScriptedSTT()
    heard: list[str] = []

    async def on_transcript(update: TranscriptUpdate) -> None:
        heard.append(update.text)

    first = _final("hello there", "2026-10-18T10:00:00+00:00")
    second = _final("next point", "2026-10-18T10:00:05+00:00")
    stt.replay = [first, second]
    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=stt, playback=FakePlayback()),
        diagnostics=Diagnostics(journal=journal),
        policy=_scripted_policy(),
    ) as session:
        await stt.final(first)
        stt.drop_socket()
        assert session.reconnect_state() == "queued"
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert session.reconnect_state() == "idle"

    assert stt.reconnects == 1
    assert heard == ["hello there", "next point"]
    assert [row["reason"] for row in journal.events("stt_final_dropped")] == ["duplicate"]
    assert len(journal.events("stt_reconnect_finished")) == 1


async def test_repeated_words_with_distinct_identity_are_both_delivered() -> None:
    stt = ScriptedSTT()
    heard: list[str] = []

    async def on_transcript(update: TranscriptUpdate) -> None:
        heard.append(update.text)

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=stt, playback=FakePlayback()),
        policy=_scripted_policy(),
    ):
        await stt.final(_final("yes", "2026-10-18T10:00:00+00:00"))
        await stt.final(_final("yes", "2026-10-18T10:00:02+00:00"))
        await stt.final(_final("yes", None))
        await stt.final(_final("yes", None))
        await stt.final(_final("yes", ""))

    assert heard == ["yes"] * 5


async def test_provider_overload_schedules_a_reconnect_instead_of_failing(
    journal: RecordingJournal,
) -> None:
    stt = ScriptedSTT()

    async def idle(_update: TranscriptUpdate) -> None:
        return None

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(idle),
        components=StreamComponents(stt=stt, playback=FakePlayback()),
        diagnostics=Diagnostics(journal=journal),
        policy=_scripted_policy(),
    ) as session:
        assert session.reconnect_state() == "idle"
        stt.drop_socket()
        assert session.reconnect_state() == "queued"
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert session.reconnect_state() == "idle"
        assert stt.reconnects == 1
        session.raise_if_failed()
