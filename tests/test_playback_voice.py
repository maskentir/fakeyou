import asyncio
from collections.abc import AsyncIterator

import numpy as np

from fakeyou.audio.playback import (
    FADE_BYTES,
    FRAME_BYTES,
    HALT_SECONDS,
    PlaybackEngine,
    PlaybackEvent,
)
from fakeyou.speech.contract import SpeechSynthesizer


class GatedWriter:
    def __init__(self, buffer_seconds: float = 0.0) -> None:
        self.buffer_seconds = buffer_seconds
        self.writes: list[bytes] = []
        self.speaking_at_write: list[bool] = []
        self.drains = 0
        self.closed = False
        self.engine: PlaybackEngine | None = None
        self.hold_after: int | None = None
        self.held = asyncio.Event()
        self.gate = asyncio.Event()

    async def write(self, pcm: bytes) -> None:
        if self.hold_after is not None and len(self.writes) == self.hold_after:
            self.held.set()
            await self.gate.wait()
        self.writes.append(pcm)
        if self.engine is not None:
            self.speaking_at_write.append(self.engine.is_speaking)

    async def drain(self) -> None:
        self.drains += 1

    async def close(self) -> None:
        self.closed = True


class ScriptedSynth:
    def __init__(self, pcm: bytes, *, stall_after_first: bool = False) -> None:
        self.pcm = pcm
        self.stall_after_first = stall_after_first
        self.stalled = asyncio.Event()

    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del text, sentence_index, transcript_id, language
        if self.stall_after_first:
            yield self.pcm[:FRAME_BYTES]
            self.stalled.set()
            await asyncio.Event().wait()
        yield self.pcm

    async def aclose(self) -> None:
        return None


def tone(samples: int) -> bytes:
    wave = 8000 * np.sin(2 * np.pi * 440 * np.arange(samples) / 24000)
    return wave.astype("<i2").tobytes()


def engine_for(
    synth: SpeechSynthesizer, writer: GatedWriter, events: list[PlaybackEvent]
) -> PlaybackEngine:
    engine = PlaybackEngine(synth, writer, on_event=events.append)
    writer.engine = engine
    return engine


def kinds(events: list[PlaybackEvent]) -> list[str]:
    return [event.kind for event in events]


async def test_a_clip_is_written_in_frames_of_whole_samples() -> None:
    writer, events = GatedWriter(), list[PlaybackEvent]()
    engine = engine_for(ScriptedSynth(tone(1250) + b"\x01"), writer, events)

    await engine.speak("Hello there.")

    assert [len(frame) for frame in writer.writes] == [FRAME_BYTES, FRAME_BYTES, 580]
    assert b"".join(writer.writes) == tone(1250)
    assert kinds(events) == ["playback_started", "playback_finished"]


async def test_stop_fades_out_the_next_frame_and_drains_to_silence() -> None:
    pcm = tone(24000)
    writer, events = GatedWriter(), list[PlaybackEvent]()
    writer.hold_after = 2
    engine = engine_for(ScriptedSynth(pcm), writer, events)
    speaking = asyncio.create_task(engine.speak("A long answer."))
    await writer.held.wait()

    stopping = asyncio.create_task(engine.stop())
    await asyncio.sleep(0)
    writer.gate.set()
    await stopping
    await speaking

    fade = writer.writes[-1]
    assert len(writer.writes) == 4
    assert len(fade) == FADE_BYTES
    original = np.frombuffer(pcm[3 * FRAME_BYTES : 3 * FRAME_BYTES + FADE_BYTES], dtype="<i2")
    faded = np.frombuffer(fade, dtype="<i2")
    assert faded[0] == original[0]
    assert np.all(np.abs(faded) <= np.abs(original))
    assert abs(int(faded[-1])) < 400
    assert kinds(events) == ["playback_started", "playback_dropped"]
    assert events[-1].detail == "interrupted"
    assert writer.drains == 1
    assert not engine.is_speaking


async def test_stop_cancels_a_clip_stuck_on_synthesis_after_the_halt_window() -> None:
    writer, events = GatedWriter(), list[PlaybackEvent]()
    synth = ScriptedSynth(tone(2400), stall_after_first=True)
    engine = engine_for(synth, writer, events)
    speaking = asyncio.create_task(engine.speak("Waiting on the voice."))
    await synth.stalled.wait()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await engine.stop()
    await speaking

    assert loop.time() - started < HALT_SECONDS + 0.5
    assert kinds(events) == ["playback_started", "playback_dropped"]
    assert not engine.is_speaking


async def test_back_to_back_clips_play_without_draining_between_them() -> None:
    writer, events = GatedWriter(buffer_seconds=0.05), list[PlaybackEvent]()
    engine = engine_for(ScriptedSynth(tone(2400)), writer, events)

    await asyncio.gather(engine.speak("First."), engine.speak("Second."))

    assert writer.drains == 0
    assert all(writer.speaking_at_write)
    assert kinds(events) == [
        "playback_started",
        "playback_finished",
        "playback_started",
        "playback_finished",
    ]


async def test_speaking_ends_once_the_buffer_plays_out_with_nothing_queued() -> None:
    writer, events = GatedWriter(buffer_seconds=0.02), list[PlaybackEvent]()
    engine = engine_for(ScriptedSynth(tone(2400)), writer, events)

    await engine.speak("Done.")
    assert engine.is_speaking
    await asyncio.sleep(0.1)

    assert not engine.is_speaking
    assert writer.drains == 1


async def test_close_releases_the_writer() -> None:
    writer, events = GatedWriter(), list[PlaybackEvent]()
    engine = engine_for(ScriptedSynth(tone(240)), writer, events)
    await engine.speak("Bye.")

    await engine.close()

    assert writer.closed


class StallingSynth:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.closed: list[str] = []

    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del sentence_index, transcript_id, language
        try:
            yield self.pcm
            await asyncio.Event().wait()
        finally:
            self.closed.append(text)

    async def aclose(self) -> None:
        return None


class RecordingSynth:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.started: list[str] = []
        self.closed: list[str] = []

    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del sentence_index, transcript_id, language
        self.started.append(text)
        try:
            yield self.pcm
        finally:
            self.closed.append(text)

    async def aclose(self) -> None:
        return None


async def test_a_prepared_sentence_synthesises_before_it_is_played() -> None:
    synth = RecordingSynth(tone(1250))
    writer, events = GatedWriter(), list[PlaybackEvent]()
    engine = engine_for(synth, writer, events)

    prepared = await engine.prepare("Hello there.")
    assert prepared is not None
    await asyncio.sleep(0)
    assert synth.started == ["Hello there."]
    assert writer.writes == []

    await engine.speak_prepared(prepared)

    assert writer.writes
    assert b"".join(writer.writes) == tone(1250)
    assert kinds(events) == ["playback_started", "playback_finished"]
    assert prepared.sentence_index is None
    assert prepared.text == "Hello there."


async def test_a_prepared_sentence_that_is_dropped_closes_its_synthesis() -> None:
    synth = RecordingSynth(tone(1250))
    writer, events = GatedWriter(), list[PlaybackEvent]()
    engine = engine_for(synth, writer, events)

    prepared = await engine.prepare("Never spoken.")
    assert prepared is not None
    await asyncio.sleep(0)
    await prepared.aclose()

    assert synth.closed == ["Never spoken."]
    assert writer.writes == []
    assert kinds(events) == []


async def test_closing_a_running_synthesis_does_not_raise() -> None:
    synth = StallingSynth(tone(1250))
    writer, events = GatedWriter(), list[PlaybackEvent]()
    engine = engine_for(synth, writer, events)

    prepared = await engine.prepare("Never spoken.")
    assert prepared is not None
    await asyncio.sleep(0)

    await prepared.aclose()
    await engine.close()

    assert synth.closed == ["Never spoken."]


async def test_closing_the_engine_closes_the_prepared_syntheses() -> None:
    synth = RecordingSynth(tone(1250))
    writer, events = GatedWriter(), list[PlaybackEvent]()
    engine = engine_for(synth, writer, events)

    await engine.prepare("Never spoken.")
    await asyncio.sleep(0)
    await engine.close()

    assert synth.closed == ["Never spoken."]
