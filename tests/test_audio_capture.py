import asyncio
import math
import struct

import pytest

from fakeyou.audio.audio import (
    MeetingAudioError,
    MeetingAudioSession,
    SessionCapture,
    SessionRuntime,
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.audio.graph import AudioGraph, GraphParts
from tests._audio_support import FakePlayback, NullSTT, ScriptedSpeech, build_run, wait_gate
from tests._persistence_fakes import RecordingJournal


async def test_capture_meters_chunk_energy_and_duration() -> None:
    activity = CaptureActivity()
    activity.mark_ready()
    speech = ScriptedSpeech()
    graph = AudioGraph(GraphParts(activity=activity, vad=speech.controller, stt=NullSTT()))
    tone = struct.pack("<320h", *[int(16384 * math.sin(i / 10)) for i in range(320)])

    await graph.ingest(tone)
    await graph.ingest(b"\x00" * 640)

    snapshot = activity.snapshot()
    assert snapshot.frames_delivered == 2
    assert snapshot.energetic_ms == 20.0
    assert snapshot.last_energetic_at is not None


async def test_capture_journals_speech_boundaries(journal: RecordingJournal) -> None:
    speech = ScriptedSpeech()
    graph = AudioGraph(
        GraphParts(
            activity=CaptureActivity(),
            vad=speech.controller,
            stt=NullSTT(),
            journal=journal,
        )
    )

    await graph.ingest(speech.speaking(6))
    await graph.ingest(speech.silent(6))

    rows = journal.events()
    assert [(row["event"], row["source"]) for row in rows] == [
        ("speech_started", "audio"),
        ("speech_stopped", "audio"),
    ]
    assert all(row["at"] for row in rows)


async def test_capture_reports_vad_edges_only_to_on_vad() -> None:
    seen: list[bool] = []

    async def on_vad(*, active: bool) -> None:
        seen.append(active)

    speech = ScriptedSpeech()
    graph = AudioGraph(
        GraphParts(activity=CaptureActivity(), vad=speech.controller, stt=NullSTT(), on_vad=on_vad)
    )

    await graph.ingest(speech.speaking(8))
    await graph.ingest(speech.silent(8))

    assert seen == [True, False]


async def test_capture_contains_on_vad_failure(journal: RecordingJournal) -> None:
    async def on_vad(*, active: bool) -> None:
        del active
        raise RuntimeError("vad consumer down")

    speech = ScriptedSpeech()
    graph = AudioGraph(
        GraphParts(
            activity=CaptureActivity(),
            vad=speech.controller,
            stt=NullSTT(),
            on_vad=on_vad,
            journal=journal,
        )
    )

    await graph.ingest(speech.speaking(6))
    await graph.ingest(speech.silent(6))

    assert journal.events()[-1]["event"] == "fault_recorded"


async def test_admission_restarts_the_capture_grace_clock_unless_audio_failed() -> None:
    clock = [100.0]
    activity = CaptureActivity(now=lambda: clock[0])
    activity.mark_ready()
    gate = asyncio.Event()
    runner_task = asyncio.create_task(wait_gate(gate))
    session = MeetingAudioSession(
        build_run(FakePlayback(), runner_task),
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
