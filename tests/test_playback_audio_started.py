import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import override

import pytest

from fakeyou.audio.playback import FRAME_BYTES, PlaybackError, PlaybackEvent, Utterance
from fakeyou.audio.voice_activity import VoiceActivityTracker
from fakeyou.observability.faults import FaultReporter
from fakeyou.orchestration.join import _JoinRun, _log_playback_event
from fakeyou.orchestration.spoken_log import SpokenLog
from fakeyou.persistence.record import MeetingRecord, TwinSpeechEntry
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._persistence_fakes import RecordingJournal
from tests.test_playback_cues import CUE_AUDIO


async def test_audio_start_waits_for_synthesis_and_the_first_successful_write() -> None:
    synthesis_started, synthesis_release = asyncio.Event(), asyncio.Event()

    class DelayedSynth(FakeSynth):
        @override
        async def stream(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
            language: str | None = None,
        ) -> AsyncIterator[bytes]:
            del text, sentence_index, transcript_id, language
            synthesis_started.set()
            await synthesis_release.wait()
            yield b""
            yield b"\x01\x00" * FRAME_BYTES

    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    writer.release.clear()
    engine = build_engine(DelayedSynth(), writer, events)
    speaking = asyncio.create_task(engine.speak("Answer.", transcript_id="t7", sentence_index=2))
    try:
        await synthesis_started.wait()
        assert [event.kind for event in events] == ["playback_started"]
        synthesis_release.set()
        await writer.started.wait()
        assert [event.kind for event in events] == ["playback_started"]
        writer.release.set()
        await speaking
        assert [event.kind for event in events] == [
            "playback_started",
            "playback_audio_started",
            "playback_finished",
        ]
        assert events[1].utterance == Utterance("t7", 2)
        assert events[1].text == "Answer."
        assert events[1].cue is False
    finally:
        writer.release.set()
        synthesis_release.set()
        await engine.close()


@pytest.mark.parametrize("outcome", ["empty", "incomplete_sample", "failed", "cancelled"])
async def test_no_audio_start_without_successfully_written_samples(outcome: str) -> None:
    events: list[PlaybackEvent] = []
    audio = b"" if outcome == "empty" else b"\x01" if outcome == "incomplete_sample" else CUE_AUDIO
    writer = FakeWriter(error=PlaybackError("write failed") if outcome == "failed" else None)
    if outcome == "cancelled":
        writer.release.clear()
    engine = build_engine(FakeSynth(audio=audio), writer, events)
    speaking = asyncio.create_task(engine.speak("Answer.", transcript_id="t7"))
    try:
        if outcome == "cancelled":
            await writer.started.wait()
            speaking.cancel()
            with pytest.raises(asyncio.CancelledError):
                await speaking
        else:
            await speaking
        assert not [event for event in events if event.kind == "playback_audio_started"]
    finally:
        writer.release.set()
        await engine.close()


async def test_prepared_cue_emits_identified_audio_start_only_when_written() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=CUE_AUDIO), writer, events)
    try:
        await engine.prepare_cue("Hmm.")
        assert events == []
        assert writer.written == []
        assert await engine.play_cue("Hmm.", transcript_id="t7")
        assert [event.kind for event in events] == [
            "playback_started",
            "playback_audio_started",
            "playback_finished",
        ]
        assert all(event.cue and event.utterance == Utterance("t7") for event in events)
    finally:
        await engine.close()


async def test_cancelled_cue_has_no_audio_start() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    writer.release.clear()
    engine = build_engine(FakeSynth(audio=CUE_AUDIO), writer, events)
    try:
        await engine.prepare_cue("Hmm.")
        playing = asyncio.create_task(engine.play_cue("Hmm."))
        await writer.started.wait()
        playing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await playing
        assert not [event for event in events if event.kind == "playback_audio_started"]
    finally:
        writer.release.set()
        await engine.close()


def test_audio_start_journals_identity_without_recording_another_speech(tmp_path: Path) -> None:
    journal = RecordingJournal()
    record = MeetingRecord(tmp_path / "record.jsonl")
    log = SpokenLog(journal, record)
    utterance = Utterance("t7", 2)
    try:
        log.observe(PlaybackEvent("playback_started", "Answer.", utterance=utterance))
        log.observe(PlaybackEvent("playback_audio_started", "Answer.", utterance=utterance))
        assert log.speaking == "Answer."
        assert record.entries() == ()
        [row] = journal.events("playback_audio_started")
        assert row["transcript_id"] == "t7"
        assert row["sentence_index"] == 2
        assert row["sentence_sha256"] == hashlib.sha256(b"Answer.").hexdigest()
        assert row["contains_question"] is False
        assert "text" not in row
        log.observe(PlaybackEvent("playback_finished", "Answer.", utterance=utterance))
        assert [entry.text for entry in record.entries() if isinstance(entry, TwinSpeechEntry)] == [
            "Answer."
        ]
        log.observe(PlaybackEvent("playback_audio_started", "Hmm.", cue=True, utterance=utterance))
        [cue] = journal.events("thinking_cue_audio_started")
        assert cue["transcript_id"] == "t7"
        assert cue["sentence_index"] == 2
        log.observe(
            PlaybackEvent("playback_finished", "Anything else? Answer.", utterance=utterance)
        )
        last = journal.events("playback_finished")[-1]
        assert last["contains_question"] is True
        assert last["ends_with_question"] is False
    finally:
        record.close()


def test_audio_start_preserves_self_speech_until_playback_finishes(tmp_path: Path) -> None:
    clock = [0.0]
    activity = VoiceActivityTracker(monotonic=lambda: clock[0])
    journal = RecordingJournal()
    record = MeetingRecord(tmp_path / "record.jsonl")
    run = _JoinRun(journal, FaultReporter(journal=journal), record, voice_activity=activity)
    try:
        _log_playback_event(run, PlaybackEvent("playback_started", "Answer."))
        clock[0] = 0.1
        _log_playback_event(run, PlaybackEvent("playback_audio_started", "Answer."))
        clock[0] = 0.2
        activity.observe_vad(active=True)
        clock[0] = 0.3
        activity.observe_vad(active=False)
        assert not activity.others_speaking()
        _log_playback_event(run, PlaybackEvent("playback_finished", "Answer."))
        clock[0] = 0.4
        activity.observe_vad(active=True)
        clock[0] = 0.5
        activity.observe_vad(active=False)
        assert activity.others_speaking()
    finally:
        record.close()
