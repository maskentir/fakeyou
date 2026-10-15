import asyncio

import pytest
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from fakeyou.audio.audio import (
    CaptureObserver,
    MeetingAudioError,
    MeetingAudioSession,
    SessionCapture,
    SessionRuntime,
)
from fakeyou.audio.deaf import CaptureActivity
from tests._audio_support import FakePlayback, build_pipeline, wait_gate
from tests._persistence_fakes import RecordingJournal


async def test_capture_observer_meters_frame_energy(monkeypatch: pytest.MonkeyPatch) -> None:
    import math
    import struct

    activity = CaptureActivity()
    activity.mark_ready()
    observer = CaptureObserver(activity)

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(observer, "push_frame", sink)
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
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:

    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    observer = CaptureObserver(CaptureActivity(), journal=journal)

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(observer, "push_frame", sink)
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    rows = journal.events()
    assert [(row["event"], row["source"]) for row in rows] == [
        ("speech_started", "audio"),
        ("speech_stopped", "audio"),
    ]
    assert all(row["at"] for row in rows)


async def test_capture_observer_reports_vad_only_to_on_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipecat.frames.frames import (
        UserSpeakingFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )

    seen: list[bool] = []

    async def on_vad(*, active: bool) -> None:
        seen.append(active)

    observer = CaptureObserver(CaptureActivity(), on_vad=on_vad)

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(observer, "push_frame", sink)
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    await observer.process_frame(UserSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert seen == [True, False]


async def test_capture_observer_contains_on_vad_failure(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    async def on_vad(*, active: bool) -> None:
        del active
        raise RuntimeError("vad consumer down")

    pushed: list[Frame] = []
    observer = CaptureObserver(CaptureActivity(), on_vad=on_vad, journal=journal)

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        pushed.append(frame)
        return None

    monkeypatch.setattr(observer, "push_frame", sink)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert len(pushed) == 1
    assert journal.events()[-1]["event"] == "fault_recorded"


async def test_admission_restarts_the_capture_grace_clock_unless_audio_failed() -> None:
    clock = [100.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task = asyncio.create_task(wait_gate(gate))
    session = MeetingAudioSession(
        build_pipeline(FakePlayback(), runner_task),
        capture=SessionCapture(activity=activity),
        runtime=SessionRuntime(now=lambda: clock[0]),
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


async def test_transcript_observer_journals_turn_stats(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection

    from fakeyou.audio.audio import TranscriptObserver

    seen = []

    async def callback(frame: TranscriptionFrame) -> None:
        seen.append(frame)

    observer = TranscriptObserver(callback, CaptureActivity(), None, journal=journal)

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(observer, "push_frame", sink)
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
