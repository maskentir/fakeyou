import asyncio

import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorCategory,
    ErrorFrame,
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

import fakeyou.audio as audio
from fakeyou.audio import (
    MeetingAudioError,
    MeetingAudioSession,
    _CaptureObserver,
    default_playback_engine,
    open_meeting_audio,
)
from fakeyou.deaf import CaptureActivity


class _Transport:
    def __init__(self) -> None:
        self.source = _Input()

    def input(self) -> FrameProcessor:
        return self.source


class _Playback:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stops = 0
        self.closed = False
        self.speaking = False

    @property
    def is_speaking(self) -> bool:
        return self.speaking

    async def speak(self, text: str) -> None:
        self.spoken.append(text)

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closed = True


class _Input(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class _STT(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                TranscriptionFrame("heard", "guest", "", finalized=False), direction
            )
            await self.push_frame(
                TranscriptionFrame("heard", "guest", "", finalized=True), direction
            )
        await self.push_frame(frame, direction)


class _FailingSTT(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(ErrorFrame("secret-provider-key"), FrameDirection.UPSTREAM)
        await self.push_frame(frame, direction)


class _StartupFailSTT(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(ErrorFrame("startup-secret"), FrameDirection.UPSTREAM)
        await self.push_frame(frame, direction)


class _ClassifiedErrorSTT(FrameProcessor):
    def __init__(self, category: ErrorCategory) -> None:
        super().__init__()
        self._category = category

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                ErrorFrame("provider hiccup", category=self._category),
                FrameDirection.UPSTREAM,
            )
        await self.push_frame(frame, direction)


async def test_session_transcribes_final_only_and_speaks_explicitly() -> None:
    engine = _Playback()
    heard: list[str] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        heard.append(frame.text)
        received.set()

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        playback=engine,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.wait_for(received.wait(), timeout=2)
        assert heard == ["heard"]
        assert engine.spoken == []
        assert session.is_speaking is False
        await session.speak("reply")
        assert engine.spoken == ["reply"]
        await session.stop_playback()
        assert engine.stops == 1
        await session.stop()
        await session.stop()
    assert engine.closed


async def test_upstream_error_unblocks_failure_waiter_without_secret() -> None:
    transport = _Transport()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=transport,
        stt=_FailingSTT(),
        playback=_Playback(),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)
        with pytest.raises(MeetingAudioError):
            session.raise_if_failed()


async def test_unexpected_worker_exit_is_reported() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        playback=_Playback(),
    ) as session:
        await session.worker.queue_frame(EndFrame())
        with pytest.raises(MeetingAudioError, match="stopped unexpectedly"):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_callback_failure_degrades_without_killing_session() -> None:
    from fakeyou.deaf import CaptureActivity
    from fakeyou.faults import FaultReporter

    activity = CaptureActivity()
    reporter = FaultReporter(meeting_id="meeting-1")

    async def on_transcript(frame: TranscriptionFrame) -> None:
        raise ValueError("secret-callback-data")

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        playback=_Playback(),
        fault_reporter=reporter,
        activity=activity,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        await session.speak("still alive")
        snapshot = activity.snapshot()
        assert snapshot.frames_delivered >= 1
        assert snapshot.finals_delivered >= 1
        report = reporter.report()
        assert report is not None
        kinds = {entry["kind"]: entry["count"] for entry in report["faults"]["kinds"]}
        assert kinds.get("callback", 0) >= 1


async def test_startup_provider_error_fails_before_timeout() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError) as caught:
        async with asyncio.timeout(2):
            async with open_meeting_audio(
                muse_api_key="muse-secret",
                openai_api_key="openai-secret",
                on_transcript=on_transcript,
                transport=_Transport(),
                stt=_StartupFailSTT(),
                playback=_Playback(),
            ):
                pass
    assert "startup-secret" not in str(caught.value)


async def test_native_transport_is_terminated_when_construction_fails(monkeypatch) -> None:
    terminated = False

    class _PyAudio:
        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

    class _BrokenTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self._pyaudio = _PyAudio()

        def input(self) -> FrameProcessor:
            raise ValueError("secret-construction-error")

    monkeypatch.setattr(audio, "_native_transport", _BrokenTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError) as caught:
        async with open_meeting_audio(
            muse_api_key="muse-secret",
            openai_api_key="openai-secret",
            on_transcript=on_transcript,
            stt=_STT(),
            playback=_Playback(),
        ):
            pass
    assert terminated
    assert "secret" not in str(caught.value)


async def test_native_transport_without_backend_still_reports_root_error(monkeypatch) -> None:
    class _BackendlessTransport(_Transport):
        def input(self) -> FrameProcessor:
            raise ValueError("construction-error")

    monkeypatch.setattr(audio, "_native_transport", _BackendlessTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError, match="initialization failed") as caught:
        async with open_meeting_audio(
            muse_api_key="muse-secret",
            openai_api_key="openai-secret",
            on_transcript=on_transcript,
            stt=_STT(),
            playback=_Playback(),
        ):
            pass
    assert isinstance(caught.value.__cause__, ValueError)


async def test_cancellation_during_startup_stops_runner(monkeypatch) -> None:
    running = asyncio.Event()
    released = asyncio.Event()
    cancelled = False

    class _Runner:
        def __init__(self, **kwargs) -> None:
            pass

        async def add_workers(self, worker) -> None:
            pass

        async def run(self) -> None:
            running.set()
            await released.wait()

        async def cancel(self, reason: str) -> None:
            nonlocal cancelled
            cancelled = True
            released.set()

    monkeypatch.setattr(audio, "WorkerRunner", _Runner)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    context = open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        playback=_Playback(),
    )
    entering = asyncio.create_task(context.__aenter__())
    await asyncio.wait_for(running.wait(), timeout=2)
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert cancelled


async def test_error_handler_registered_before_add_workers(monkeypatch) -> None:
    order: list[str] = []
    original_add_handler = PipelineWorker.add_event_handler

    def recording_add_handler(self, event_name: str, handler) -> None:
        order.append(event_name)
        original_add_handler(self, event_name, handler)

    monkeypatch.setattr(PipelineWorker, "add_event_handler", recording_add_handler)
    original_add_workers = WorkerRunner.add_workers

    async def recording_add_workers(self, *workers) -> None:
        order.append("add_workers")
        await original_add_workers(self, *workers)

    monkeypatch.setattr(WorkerRunner, "add_workers", recording_add_workers)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        playback=_Playback(),
    ):
        pass
    assert "on_pipeline_error" in order
    assert order.index("on_pipeline_error") < order.index("add_workers")
    started_before = [
        name for name in order[: order.index("add_workers")] if name == "on_pipeline_started"
    ]
    assert len(started_before) >= 2


async def test_pipeline_error_chains_frame_detail() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_FailingSTT(),
        playback=_Playback(),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is not None
        assert "secret-provider-key" not in str(caught.value.__cause__)


async def test_runner_failure_chains_original_exception() -> None:
    async def boom() -> None:
        raise ValueError("root-boom")

    task = asyncio.create_task(boom())
    session = MeetingAudioSession(object(), object(), task, _Playback())
    with pytest.raises(MeetingAudioError) as caught:
        await asyncio.wait_for(session.wait_failed(), timeout=2)
    assert isinstance(caught.value.__cause__, ValueError)
    assert "root-boom" in str(caught.value.__cause__)


async def test_terminate_failure_does_not_mask_initialization_error(monkeypatch) -> None:
    class _RaisingPyAudio:
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    class _BrokenTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self._pyaudio = _RaisingPyAudio()

        def input(self) -> FrameProcessor:
            raise ValueError("construction-error")

    monkeypatch.setattr(audio, "_native_transport", _BrokenTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError, match="initialization failed"):
        async with open_meeting_audio(
            muse_api_key="muse-secret",
            openai_api_key="openai-secret",
            on_transcript=on_transcript,
            stt=_STT(),
            playback=_Playback(),
        ):
            pass


async def test_nonpermanent_stt_error_keeps_session_alive() -> None:
    engine = _Playback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_ClassifiedErrorSTT(ErrorCategory.UNKNOWN),
        playback=engine,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        await session.speak("hello")
        assert engine.spoken == ["hello"]


async def test_permanent_stt_error_fails_session() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_ClassifiedErrorSTT(ErrorCategory.AUTHENTICATION),
        playback=_Playback(),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_injected_playback_engine_is_closed_on_shutdown() -> None:
    engine = _Playback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        playback=engine,
    ):
        assert engine.closed is False
    assert engine.closed


async def test_default_playback_engine_closes_speech_client() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
    ) as session:
        owned = session._engine._synthesizer._http_client
        assert owned is not None
        assert not owned.is_closed
    assert owned.is_closed


async def test_terminate_failure_does_not_fail_clean_shutdown(monkeypatch) -> None:
    class _RaisingPyAudio:
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    class _NativeTransport(_Transport):
        def __init__(self) -> None:
            super().__init__()
            self._pyaudio = _RaisingPyAudio()

    monkeypatch.setattr(audio, "_native_transport", _NativeTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        stt=_STT(),
        playback=_Playback(),
    ):
        pass


class _InterimOnlySTT(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                TranscriptionFrame("partial", "guest", "", finalized=False), direction
            )
        await self.push_frame(frame, direction)


class _ExceptionalSTT(FrameProcessor):
    def __init__(self, exception: Exception) -> None:
        super().__init__()
        self._exception = exception

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                ErrorFrame("provider blew up", exception=self._exception),
                FrameDirection.UPSTREAM,
            )
        await self.push_frame(frame, direction)


async def test_interim_frames_route_to_on_interim_and_tap() -> None:
    from fakeyou.deaf import CaptureActivity

    activity = CaptureActivity()
    interims: list[str] = []
    finals: list[str] = []
    interim_seen = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        finals.append(frame.text)

    async def on_interim(frame: TranscriptionFrame) -> None:
        interims.append(frame.text)
        interim_seen.set()

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        on_interim=on_interim,
        transport=_Transport(),
        stt=_InterimOnlySTT(),
        playback=_Playback(),
        activity=activity,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.wait_for(interim_seen.wait(), timeout=2)
        await asyncio.sleep(0.2)
        assert interims == ["partial"]
        assert finals == []
        snapshot = activity.snapshot()
        assert snapshot.frames_delivered >= 1
        assert snapshot.finals_delivered == 0
        assert snapshot.interim_count >= 1


async def test_pipeline_marks_activity_ready_and_unavailable_on_failure() -> None:
    from fakeyou.deaf import CaptureActivity

    activity = CaptureActivity()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_FailingSTT(),
        playback=_Playback(),
        activity=activity,
    ) as session:
        assert activity.snapshot().available is True
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError):
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert activity.snapshot().available is False


async def test_permanent_error_records_before_kill_with_typed_cause() -> None:
    from fakeyou.faults import FaultReporter
    from fakeyou.provider_faults import ProviderFault

    reporter = FaultReporter(meeting_id="meeting-1")

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_ClassifiedErrorSTT(ErrorCategory.AUTHENTICATION),
        playback=_Playback(),
        fault_reporter=reporter,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert reporter.total() >= 1
        cause = caught.value.__cause__
        assert isinstance(cause, ProviderFault)
        assert cause.source == "stt"
        assert cause.kind == "unauthorized"
        assert cause.retryable is False


async def test_nonpermanent_error_records_without_killing() -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(meeting_id="meeting-1")

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_ClassifiedErrorSTT(ErrorCategory.CONNECTIVITY),
        playback=_Playback(),
        fault_reporter=reporter,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        assert reporter.total() >= 1
        report = reporter.report()
        assert report is not None
        assert "unavailable" in {entry["kind"] for entry in report["faults"]["kinds"]}


async def test_uncategorized_error_falls_back_to_exception_classification() -> None:
    import httpx2

    from fakeyou.faults import FaultReporter
    from fakeyou.provider_faults import ProviderFault

    reporter = FaultReporter(meeting_id="meeting-1")

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_ExceptionalSTT(httpx2.ConnectError("socket gone")),
        playback=_Playback(),
        fault_reporter=reporter,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        cause = caught.value.__cause__
        assert isinstance(cause, ProviderFault)
        assert cause.kind == "unavailable"
        assert cause.retryable is True


async def test_unsupported_stt_reconnect_records_without_attempts() -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(meeting_id="meeting-1")

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_STT(),
        playback=_Playback(),
        fault_reporter=reporter,
    ) as session:
        assert session.reconnect_state() == "idle"
        session.request_stt_reconnect("deaf")
        assert session.reconnect_state() == "unsupported"
        report = reporter.report()
        assert report is not None
        kinds = {entry["kind"] for entry in report["faults"]["kinds"]}
        assert "reconnect_unsupported" in kinds


async def test_supported_stt_reconnect_queues_request() -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(meeting_id="meeting-1")

    class _Reconnectable(FrameProcessor):
        async def reconnect_when_idle(self) -> None:
            pass

        async def process_frame(self, frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        muse_api_key="muse-secret",
        openai_api_key="openai-secret",
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=_Reconnectable(),
        playback=_Playback(),
        fault_reporter=reporter,
    ) as session:
        session.request_stt_reconnect("deaf")
        session.request_stt_reconnect("deaf")
        assert session.reconnect_state() == "queued"
        assert reporter.total() == 0


async def test_stt_reconnect_lifecycle_is_journaled() -> None:
    import json

    from fakeyou.deaf import CaptureActivity
    from fakeyou.journal import ProviderJournal

    lines: list[str] = []
    journal = ProviderJournal("meeting-1", emit=lines.append)
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()

    class _ReconnectableSTT:
        def __init__(self) -> None:
            self.calls = 0

        async def reconnect_when_idle(self) -> None:
            self.calls += 1

    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    stt = _ReconnectableSTT()
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
        session.request_stt_reconnect("deaf")
        clock[0] += 10
        activity.observe_audio()
        verdict = await session.maintenance_tick(is_busy=False)
    finally:
        gate.set()
        await runner_task

    assert verdict == "silent"
    assert stt.calls == 1
    assert session.reconnect_state() == "idle"
    rows = [json.loads(line) for line in lines]
    assert [(row["event"], row["outcome"]) for row in rows] == [
        ("stt_reconnect", "queued"),
        ("stt_reconnect", "ok"),
    ]
    assert rows[0]["reason"] == "deaf"
    assert rows[1]["attempts"] == 1


def test_default_playback_engine_streams_tts_to_paplay() -> None:
    import inspect

    engine = default_playback_engine(openai_api_key="k")

    assert engine._streaming is True
    assert engine._stream_audio is not None
    assert inspect.isasyncgenfunction(engine._synthesizer.stream)


async def test_default_playback_engine_wires_faults_and_journal() -> None:
    from fakeyou.faults import FaultReporter
    from fakeyou.journal import ProviderJournal

    reporter = FaultReporter(meeting_id="meeting-1")
    journal = ProviderJournal("meeting-1", emit=lambda _: None, stdout=False)
    engine = default_playback_engine(openai_api_key="k", fault_reporter=reporter, journal=journal)
    try:
        assert engine._faults is reporter
        assert engine._journal is journal
        assert engine._synthesizer._journal is journal
    finally:
        await engine.close()


async def test_capture_observer_meters_frame_energy() -> None:
    import math
    import struct

    activity = CaptureActivity()
    activity.mark_ready()
    observer = _CaptureObserver(activity)

    async def sink(frame, direction):
        return None

    observer.push_frame = sink  # type: ignore[method-assign]
    tone = struct.pack("<320h", *[int(16384 * math.sin(i / 10)) for i in range(320)])
    await observer.process_frame(
        InputAudioRawFrame(audio=tone, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        InputAudioRawFrame(audio=b"\x00" * 640, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )

    snapshot = activity.snapshot()
    assert snapshot.frames_delivered == 2
    assert snapshot.energetic_ms == 20.0
    assert snapshot.last_energetic_at is not None


async def test_capture_observer_journals_speech_boundaries() -> None:
    import json

    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    from fakeyou.journal import ProviderJournal

    lines: list[str] = []
    observer = _CaptureObserver(CaptureActivity(), journal=ProviderJournal("m", emit=lines.append))

    async def sink(frame, direction):
        return None

    observer.push_frame = sink  # type: ignore[method-assign]
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    rows = [json.loads(line) for line in lines]
    assert [(row["event"], row["source"]) for row in rows] == [
        ("speech_started", "capture"),
        ("speech_stopped", "capture"),
    ]
    assert all(row["at"] for row in rows)
