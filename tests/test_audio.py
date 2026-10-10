import asyncio
import struct

import pytest
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, TranscriptionFrame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.errors import ErrorCategory
from pipecat.workers.runner import WorkerRunner

import fakeyou.audio as audio
from fakeyou.audio import (
    MeetingAudioError,
    MeetingAudioSession,
    TurnAudioTap,
    _CaptureObserver,
    create_playback_engine,
    open_meeting_audio,
)
from fakeyou.deaf import CaptureActivity
from fakeyou.journal import Journal
from fakeyou.settings import MiniMaxTTS, OpenAITTS, STTSettings
from tests.fakes import FakeReconnectableSTT, FakeSTT


class _Transport:
    def __init__(self) -> None:
        self.source = _Input()

    def input(self) -> FrameProcessor:
        return self.source


class _Playback:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.stops = 0
        self.closed = False
        self.speaking = False

    @property
    def is_speaking(self) -> bool:
        return self.speaking

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        self.spoken.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closed = True


class _Input(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


async def test_session_transcribes_final_only_and_speaks_explicitly() -> None:
    engine = _Playback()
    heard: list[str] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        heard.append(frame.text)
        received.set()

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
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


async def test_session_threads_sentence_index_to_playback_engine() -> None:
    engine = _Playback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
        playback=engine,
    ) as session:
        await session.speak("First sentence.", sentence_index=0)
        await session.speak("Second sentence.", sentence_index=1)
        await session.speak("Legacy reply.")
        assert engine.spoken == ["First sentence.", "Second sentence.", "Legacy reply."]
        assert engine.indices == [0, 1, None]
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=transport,
        stt=FakeSTT(error="secret-provider-key", finalized=()),
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
        playback=_Playback(),
    ) as session:
        await session.worker.queue_frame(EndFrame())
        with pytest.raises(MeetingAudioError, match="stopped unexpectedly"):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_callback_failure_degrades_without_killing_session(
    journal,
) -> None:
    from fakeyou.deaf import CaptureActivity
    from fakeyou.faults import FaultReporter

    activity = CaptureActivity()
    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        raise ValueError("secret-callback-data")

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
        playback=_Playback(),
        faults=reporter,
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
        assert kinds.get("callback_failed", 0) >= 1


async def test_startup_provider_error_fails_before_timeout() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError) as caught:
        async with asyncio.timeout(2):
            async with open_meeting_audio(
                stt_settings=STTSettings(api_key="muse-secret"),
                tts_settings=OpenAITTS(api_key="openai-secret"),
                on_transcript=on_transcript,
                transport=_Transport(),
                stt=FakeSTT(error="startup-secret", finalized=(), startup=True),
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
            stt_settings=STTSettings(api_key="muse-secret"),
            tts_settings=OpenAITTS(api_key="openai-secret"),
            on_transcript=on_transcript,
            stt=FakeSTT(),
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
            stt_settings=STTSettings(api_key="muse-secret"),
            tts_settings=OpenAITTS(api_key="openai-secret"),
            on_transcript=on_transcript,
            stt=FakeSTT(),
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(error="secret-provider-key", finalized=()),
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
            stt_settings=STTSettings(api_key="muse-secret"),
            tts_settings=OpenAITTS(api_key="openai-secret"),
            on_transcript=on_transcript,
            stt=FakeSTT(),
            playback=_Playback(),
        ):
            pass


async def test_nonpermanent_stt_error_keeps_session_alive() -> None:
    engine = _Playback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(error="provider hiccup", finalized=(), category=ErrorCategory.UNKNOWN),
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(error="provider hiccup", finalized=(), category=ErrorCategory.AUTHENTICATION),
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
        playback=engine,
    ):
        assert engine.closed is False
    assert engine.closed


async def test_default_playback_engine_closes_speech_client() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
    ) as session:
        owned = session._engine._synthesizer._client
        assert not owned.is_closed()
    assert owned.is_closed()


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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        stt=FakeSTT(),
        playback=_Playback(),
    ):
        pass


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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        on_interim=on_interim,
        transport=_Transport(),
        stt=FakeSTT("partial", finalized=(False,)),
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
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(error="secret-provider-key", finalized=()),
        playback=_Playback(),
        activity=activity,
    ) as session:
        assert activity.snapshot().available is True
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError):
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert activity.snapshot().available is False


async def test_permanent_error_records_before_kill_with_typed_cause(
    journal,
) -> None:
    from fakeyou.faults import FaultReporter
    from fakeyou.provider_faults import ProviderFault

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(error="provider hiccup", finalized=(), category=ErrorCategory.AUTHENTICATION),
        playback=_Playback(),
        faults=reporter,
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


async def test_nonpermanent_error_records_without_killing(
    journal,
) -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(error="provider hiccup", finalized=(), category=ErrorCategory.CONNECTIVITY),
        playback=_Playback(),
        faults=reporter,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        assert reporter.total() >= 1
        report = reporter.report()
        assert report is not None
        assert "unavailable" in {entry["kind"] for entry in report["faults"]["kinds"]}


async def test_uncategorized_error_falls_back_to_exception_classification(
    journal,
) -> None:
    import httpx2

    from fakeyou.faults import FaultReporter
    from fakeyou.provider_faults import ProviderFault

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(
            error="provider blew up", finalized=(), exception=httpx2.ConnectError("socket gone")
        ),
        playback=_Playback(),
        faults=reporter,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        cause = caught.value.__cause__
        assert isinstance(cause, ProviderFault)
        assert cause.kind == "unavailable"
        assert cause.retryable is True


async def test_unsupported_stt_reconnect_records_without_attempts(
    journal,
) -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
        playback=_Playback(),
        faults=reporter,
    ) as session:
        assert session.reconnect_state() == "idle"
        session.request_stt_reconnect("deaf")
        assert session.reconnect_state() == "unsupported"
        report = reporter.report()
        assert report is not None
        kinds = {entry["kind"] for entry in report["faults"]["kinds"]}
        assert "reconnect_unsupported" in kinds


async def test_supported_stt_reconnect_queues_request(
    journal,
) -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeReconnectableSTT(),
        playback=_Playback(),
        faults=reporter,
    ) as session:
        session.request_stt_reconnect("deaf")
        session.request_stt_reconnect("deaf")
        assert session.reconnect_state() == "queued"
        assert reporter.total() == 0


async def test_stt_reconnect_lifecycle_is_journaled(
    journal,
) -> None:

    from fakeyou.deaf import CaptureActivity

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
    rows = journal.events()
    assert [(row["event"], row["source"]) for row in rows] == [
        ("stt_reconnect_queued", "stt"),
        ("stt_reconnect_finished", "stt"),
    ]
    assert rows[0]["reason"] == "deaf"
    assert rows[1]["attempts"] == 1


def test_default_playback_engine_streams_tts_to_paplay() -> None:
    import inspect

    engine = create_playback_engine(tts_settings=OpenAITTS(api_key="k"))

    assert engine._stream_audio is not None
    assert inspect.isasyncgen(engine._synthesizer.stream("hello"))


def test_default_playback_engine_uses_the_configured_tts_provider() -> None:
    from fakeyou.voice import MiniMaxSpeechSynthesizer

    engine = create_playback_engine(
        tts_settings=MiniMaxTTS(api_key="k", voice="Indonesian_CaringMan")
    )

    assert isinstance(engine._synthesizer, MiniMaxSpeechSynthesizer)
    assert engine._synthesizer._voice == "Indonesian_CaringMan"


async def test_default_playback_engine_wires_faults_and_journal() -> None:
    from fakeyou.faults import FaultReporter
    from fakeyou.journal import Journal

    reporter = FaultReporter(journal=Journal("meeting-1"))
    journal = Journal("meeting-1", emit=lambda _: None, echo=False)
    engine = create_playback_engine(
        tts_settings=OpenAITTS(api_key="k"), faults=reporter, journal=journal
    )
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

    observer.push_frame = sink
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


async def test_capture_observer_journals_speech_boundaries(
    journal,
) -> None:

    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    observer = _CaptureObserver(CaptureActivity(), journal=journal)

    async def sink(frame, direction):
        return None

    observer.push_frame = sink
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    rows = journal.events()
    assert [(row["event"], row["source"]) for row in rows] == [
        ("speech_started", "audio"),
        ("speech_stopped", "audio"),
    ]
    assert all(row["at"] for row in rows)


async def test_capture_observer_reports_vad_only_to_on_vad() -> None:
    from pipecat.frames.frames import (
        UserSpeakingFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )

    seen: list[bool] = []

    async def on_vad(active: bool) -> None:
        seen.append(active)

    observer = _CaptureObserver(CaptureActivity(), on_vad=on_vad)

    async def sink(frame, direction):
        return None

    observer.push_frame = sink
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    await observer.process_frame(UserSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert seen == [True, False]


async def test_capture_observer_contains_on_vad_failure(journal) -> None:
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    async def on_vad(active: bool) -> None:
        raise RuntimeError("vad consumer down")

    pushed: list = []
    observer = _CaptureObserver(CaptureActivity(), on_vad=on_vad, journal=journal)

    async def sink(frame, direction):
        pushed.append(frame)
        return None

    observer.push_frame = sink
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert len(pushed) == 1
    assert journal.events()[-1]["event"] == "fault_recorded"


async def test_admission_restarts_the_capture_grace_clock_unless_audio_failed() -> None:
    clock = [100.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task: asyncio.Task[None] = asyncio.create_task(gate.wait())
    session = MeetingAudioSession(
        object(), object(), runner_task, _Playback(), activity=activity, now=lambda: clock[0]
    )
    clock[0] = 400.0
    session.mark_admitted()
    admitted = activity.snapshot()
    gate.set()
    with pytest.raises(MeetingAudioError):
        await asyncio.wait_for(session.wait_failed(), timeout=2)
    clock[0] = 500.0
    session.mark_admitted()
    failed = activity.snapshot()

    assert (admitted.available, admitted.ready_at) == (True, 400.0)
    assert (failed.available, failed.ready_at) == (False, 400.0)


async def test_callback_fault_should_use_audio_journal_without_reporter(tmp_path):
    from fakeyou.report import read_journal

    path = tmp_path / "events.jsonl"
    journal = Journal("meeting-7", path=path, echo=False)
    failed = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        failed.set()
        raise ValueError("private callback data")

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
        playback=_Playback(),
        journal=journal,
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.wait_for(failed.wait(), timeout=2)
        session.raise_if_failed()
    journal.close()

    callbacks = [
        row
        for row in read_journal(path)
        if row["event"] == "fault_recorded" and row["kind"] == "callback_failed"
    ]
    assert len(callbacks) == 1
    assert callbacks[0]["meeting_id"] == "meeting-7"
    assert callbacks[0]["at"]
    assert "private callback data" not in path.read_text()


async def test_session_threads_transcript_id_to_playback_engine() -> None:
    engine = _Playback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        stt_settings=STTSettings(api_key="muse-secret"),
        tts_settings=OpenAITTS(api_key="openai-secret"),
        on_transcript=on_transcript,
        transport=_Transport(),
        stt=FakeSTT(),
        playback=engine,
    ) as session:
        await session.speak("Hello.", sentence_index=0, transcript_id="t9")
        await session.speak("Legacy reply.")
        assert engine.spoken == ["Hello.", "Legacy reply."]
        assert engine.transcripts == ["t9", None]
        await session.stop()


async def test_transcript_observer_journals_turn_stats(journal) -> None:
    from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection

    from fakeyou.audio import _TranscriptObserver

    seen = []

    async def callback(frame):
        seen.append(frame)

    observer = _TranscriptObserver(callback, CaptureActivity(), None, journal=journal)

    async def sink(frame, direction):
        return None

    observer.push_frame = sink
    await observer.process_frame(
        InterimTranscriptionFrame(text="Hel", user_id="", timestamp="2026-10-06T01:00:01+00:00"),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        InterimTranscriptionFrame(text="Hello", user_id="", timestamp="2026-10-06T01:00:02+00:00"),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        TranscriptionFrame("Hello.", "", "2026-10-06T01:00:03+00:00", finalized=True),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        TranscriptionFrame("Next.", "", "2026-10-06T01:00:04+00:00", finalized=True),
        FrameDirection.DOWNSTREAM,
    )

    assert [frame.text for frame in seen] == ["Hello.", "Next."]
    stats = [row for row in journal.events() if row["event"] == "stt_turn_recorded"]
    assert len(stats) == 2
    assert stats[0]["interims"] == 2
    assert stats[0]["first_interim_to_final_ms"] >= 0
    assert stats[1]["interims"] == 0
    assert stats[1]["first_interim_to_final_ms"] is None


def test_vad_analyzer_maps_settings_to_params() -> None:
    from pipecat.audio.vad.vad_analyzer import VADParams

    from fakeyou.audio import vad_analyzer
    from fakeyou.invariants import CAPTURE_SAMPLE_RATE
    from fakeyou.settings import DEFAULTS, VADSettings

    default = vad_analyzer(CAPTURE_SAMPLE_RATE, DEFAULTS.vad)
    expected = VADParams()
    assert default._params.confidence == expected.confidence
    assert default._params.start_secs == expected.start_secs
    assert default._params.stop_secs == expected.stop_secs
    assert default._params.min_volume == expected.min_volume
    custom = vad_analyzer(
        CAPTURE_SAMPLE_RATE,
        VADSettings(confidence=0.5, start_secs=0.1, stop_secs=0.4, min_volume=0.3),
    )
    assert custom._params.confidence == 0.5
    assert custom._params.start_secs == 0.1
    assert custom._params.stop_secs == 0.4
    assert custom._params.min_volume == 0.3


async def test_turn_tap_collects_audio_between_speech_boundaries() -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()
    pushed: list = []

    async def sink(frame, direction):
        pushed.append(frame)
        return None

    tap.push_frame = sink
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x01" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x02" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x03" * 160, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x01" * 320 + b"\x02" * 320 + b"\x03" * 160
    assert tap.take_clip() == b""
    assert [type(frame).__name__ for frame in pushed] == [
        "InputAudioRawFrame",
        "VADUserStartedSpeakingFrame",
        "InputAudioRawFrame",
        "InputAudioRawFrame",
        "VADUserStoppedSpeakingFrame",
    ]


async def test_turn_tap_stop_without_start_leaves_no_clip() -> None:
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame, direction):
        return None

    tap.push_frame = sink
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b""


async def test_turn_tap_restarts_on_repeated_start() -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame, direction):
        return None

    tap.push_frame = sink
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x04" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x05" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x04" * 320 + b"\x05" * 320


async def test_turn_tap_accumulates_segments_across_vad_cycles() -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame, direction):
        return None

    tap.push_frame = sink
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0a" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0b" * 160, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x0a" * 320 + b"\x0b" * 160


async def test_turn_tap_trims_accumulation_to_recent_tail() -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    from fakeyou.audio import TURN_TAP_MAX_SECONDS

    limit = int(16000 * 2 * TURN_TAP_MAX_SECONDS)
    first = b"\x0c" * limit
    second = b"\x0d" * 3200
    tap = TurnAudioTap()

    async def sink(frame, direction):
        return None

    tap.push_frame = sink
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=first, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=second, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == (first + second)[-limit:]


async def test_turn_tap_accumulated_take_clears() -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    tap = TurnAudioTap()

    async def sink(frame, direction):
        return None

    tap.push_frame = sink
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0e" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=b"\x0f" * 160, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert tap.take_clip() == b"\x0e" * 320 + b"\x0f" * 160
    assert tap.take_clip() == b""


def _tap_stream_frame(index: int) -> bytes:
    base = index * 160
    samples = [((base + offset) * 257 + 12345) % 65536 - 32768 for offset in range(160)]
    return struct.pack("<160h", *samples)


async def _drive_tap(tap: TurnAudioTap, total: int, start_at: int, stop_at: int) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    for index in range(total):
        if index == start_at:
            await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        if index == stop_at:
            await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await tap.process_frame(
            InputAudioRawFrame(audio=_tap_stream_frame(index), sample_rate=16000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
    if start_at == total:
        await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    if stop_at == total:
        await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


async def test_turn_tap_clips_within_one_grid_cell_are_byte_identical() -> None:
    first = TurnAudioTap()
    second = TurnAudioTap()

    async def sink(frame, direction):
        return None

    first.push_frame = sink
    second.push_frame = sink
    await _drive_tap(first, 300, 105, 231)
    await _drive_tap(second, 300, 109, 235)

    first_clip = first.take_clip()
    second_clip = second.take_clip()

    assert first_clip == second_clip
    assert first_clip == b"".join(_tap_stream_frame(index) for index in range(80, 260))


async def test_turn_tap_includes_preroll_before_speech_start() -> None:
    tap = TurnAudioTap()

    async def sink(frame, direction):
        return None

    tap.push_frame = sink
    await _drive_tap(tap, 100, 50, 100)

    clip = tap.take_clip()

    assert clip == b"".join(_tap_stream_frame(index) for index in range(30, 100))
    assert clip[:6400] == b"".join(_tap_stream_frame(index) for index in range(30, 50))


async def test_turn_tap_includes_trailing_after_speech_stop() -> None:
    tap = TurnAudioTap()

    async def sink(frame, direction):
        return None

    tap.push_frame = sink
    await _drive_tap(tap, 110, 10, 60)

    clip = tap.take_clip()

    assert clip == b"".join(_tap_stream_frame(index) for index in range(80))
    assert clip[600 * 32 : 800 * 32] == b"".join(
        _tap_stream_frame(index) for index in range(60, 80)
    )


async def test_turn_tap_keep_open_second_take_is_byte_identical() -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    first = TurnAudioTap()
    second = TurnAudioTap()

    async def sink(frame, direction):
        return None

    first.push_frame = sink
    second.push_frame = sink
    first_bounds = (20, 40, 105, 231)
    second_bounds = (20, 40, 109, 235)
    total = 300
    mid = 150

    for index in range(mid):
        for tap, bounds in ((first, first_bounds), (second, second_bounds)):
            start_first, stop_first, start_second, stop_second = bounds
            if index == start_first or index == start_second:
                await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            if index == stop_first or index == stop_second:
                await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            await tap.process_frame(
                InputAudioRawFrame(
                    audio=_tap_stream_frame(index), sample_rate=16000, num_channels=1
                ),
                FrameDirection.DOWNSTREAM,
            )

    assert first.take_clip() == second.take_clip()

    for index in range(mid, total):
        for tap, bounds in ((first, first_bounds), (second, second_bounds)):
            start_first, stop_first, start_second, stop_second = bounds
            if index == start_first or index == start_second:
                await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            if index == stop_first or index == stop_second:
                await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            await tap.process_frame(
                InputAudioRawFrame(
                    audio=_tap_stream_frame(index), sample_rate=16000, num_channels=1
                ),
                FrameDirection.DOWNSTREAM,
            )

    first_clip = first.take_clip()
    second_clip = second.take_clip()

    assert first_clip == second_clip
    assert first_clip == b"".join(_tap_stream_frame(index) for index in range(80, 260))
