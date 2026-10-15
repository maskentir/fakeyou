import asyncio
from typing import override

import pytest
from pipecat.frames.frames import TranscriptionFrame
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
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReconnectableSTT, FakeSTT
from tests._audio_support import FakePlayback, FakeTransport, build_pipeline, wait_gate
from tests._capture_support import settle_reconnect
from tests._persistence_fakes import RecordingJournal


async def test_unsupported_stt_reconnect_records_without_attempts(
    journal: RecordingJournal,
) -> None:
    from fakeyou.observability.faults import FaultReporter

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        diagnostics=Diagnostics(faults=reporter),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        assert session.reconnect_state() == "idle"
        session.request_stt_reconnect("deaf")
        assert session.reconnect_state() == "unsupported"
        report = reporter.report()
        assert report is not None
        kinds = {entry["kind"] for entry in report["faults"]["kinds"]}
        assert "reconnect_unsupported" in kinds


async def test_supported_stt_reconnect_queues_request(
    journal: RecordingJournal,
) -> None:
    from fakeyou.observability.faults import FaultReporter

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeReconnectableSTT(), playback=FakePlayback()),
        diagnostics=Diagnostics(faults=reporter),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        session.request_stt_reconnect("deaf")
        session.request_stt_reconnect("deaf")
        assert session.reconnect_state() == "queued"
        assert reporter.total() == 0


async def test_stt_reconnect_lifecycle_is_journaled(
    journal: RecordingJournal,
) -> None:

    from fakeyou.audio.deaf import CaptureActivity

    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()

    gate = asyncio.Event()
    runner_task = asyncio.create_task(wait_gate(gate))
    stt = FakeReconnectableSTT()
    session = MeetingAudioSession(
        build_pipeline(FakePlayback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    try:
        session.request_stt_reconnect("deaf")
        clock[0] += 10
        activity.observe_audio()
        verdict = await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
    finally:
        gate.set()
        await runner_task

    assert verdict == "silent"
    assert stt.calls == 1
    assert session.reconnect_state() == "idle"
    rows = journal.events()
    assert [(row["event"], row["source"]) for row in rows] == [
        ("stt_reconnect_queued", "stt"),
        ("stt_reconnect_finished", "stt"),
    ]
    assert rows[0]["reason"] == "deaf"
    assert rows[1]["attempts"] == 1


class _FailingReconnectSTT(FakeReconnectableSTT):
    @override
    async def reconnect_when_idle(self) -> None:
        self.calls += 1
        raise RuntimeError("provider down")


def _reconnect_session(
    journal: RecordingJournal, clock: list[float], stt: FakeReconnectableSTT
) -> tuple[MeetingAudioSession, CaptureActivity, asyncio.Task[None], asyncio.Event]:
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task = asyncio.create_task(wait_gate(gate))
    session = MeetingAudioSession(
        build_pipeline(FakePlayback(), runner_task),
        capture=SessionCapture(stt=stt, activity=activity),
        diagnostics=Diagnostics(journal=journal),
        runtime=SessionRuntime(now=lambda: clock[0]),
    )
    return session, activity, runner_task, gate


async def test_stt_reconnect_backs_off_between_attempts(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    import random

    monkeypatch.setattr(random, "uniform", lambda _low, _high: 0.0)
    clock = [1000.0]
    stt = _FailingReconnectSTT()
    session, activity, runner_task, gate = _reconnect_session(journal, clock, stt)
    try:
        session.request_stt_reconnect("deaf")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 1
        clock[0] += 4
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 1
        clock[0] += 1
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 2
        clock[0] += 9
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 2
        clock[0] += 1
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 3
        assert session.reconnect_state() == "exhausted"
        with pytest.raises(MeetingAudioError):
            await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task


async def test_stt_reconnect_adds_jitter_to_the_backoff(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    import random

    monkeypatch.setattr(random, "uniform", lambda _low, high: high)
    clock = [1000.0]
    stt = _FailingReconnectSTT()
    session, activity, runner_task, gate = _reconnect_session(journal, clock, stt)
    try:
        session.request_stt_reconnect("deaf")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 1
        clock[0] += 9
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 1
        clock[0] += 1
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
        assert stt.calls == 2
    finally:
        gate.set()
        await runner_task


class _BuggyReconnectSTT(FakeReconnectableSTT):
    @override
    async def reconnect_when_idle(self) -> None:
        self.calls += 1
        raise ValueError("bug")


async def test_unexpected_reconnect_bug_is_recorded_and_requeued(
    journal: RecordingJournal,
) -> None:
    clock = [1000.0]
    stt = _BuggyReconnectSTT()
    session, activity, runner_task, gate = _reconnect_session(journal, clock, stt)
    try:
        session.request_stt_reconnect("deaf")
        clock[0] += 10
        activity.observe_audio()
        await session.maintenance_tick(is_busy=False)
        await settle_reconnect(session)
    finally:
        gate.set()
        await runner_task

    assert stt.calls == 1
    [failed] = journal.events("stt_reconnect_failed")
    assert failed["attempts"] == 1
    assert session.reconnect_state() == "queued"
