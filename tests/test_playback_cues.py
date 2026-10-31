import asyncio
import struct

from fakeyou.audio.playback import CUE_RENDER_ATTEMPTS, CuePlayback, PlaybackEvent
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._persistence_fakes import RecordingJournal

FRAME = PLAYBACK_SAMPLE_RATE // 50


def voiced(frames: int, level: int = 3000) -> bytes:
    return struct.pack("<h", level) * (FRAME * frames)


def silent(frames: int) -> bytes:
    return bytes(FRAME * frames * 2)


CUE_AUDIO = voiced(20)


async def test_prepared_cue_plays_from_cache_without_new_synthesis() -> None:
    synth = FakeSynth(audio=CUE_AUDIO)
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    await engine.prepare_cue("Hmm…")
    await engine.prepare_cue("Hmm…")
    played = await engine.play_cue("Hmm…")
    await engine.close()

    assert isinstance(engine, CuePlayback)
    assert played is True
    assert synth.texts == ["Hmm…"]
    assert writer.heard == CUE_AUDIO
    assert [(event.kind, event.text, event.cue) for event in events] == [
        ("playback_started", "Hmm…", True),
        ("playback_finished", "Hmm…", True),
    ]


async def test_cue_is_refused_until_it_is_prepared() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    played = await engine.play_cue("Hmm…")
    await engine.close()

    assert played is False
    assert writer.written == []
    assert events == []


async def test_cue_is_refused_while_the_twin_is_already_speaking() -> None:
    synth = FakeSynth(audio=CUE_AUDIO)
    writer = FakeWriter()
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)
    await engine.prepare_cue("Hmm…")
    synth.texts.clear()

    reply = asyncio.create_task(engine.speak("Friday."))
    await writer.started.wait()
    played = await engine.play_cue("Hmm…")
    writer.release.set()
    await reply
    await engine.close()

    assert played is False
    assert synth.texts == ["Friday."]
    assert all(event.cue is False for event in events)


async def test_reply_that_arrives_during_a_cue_plays_right_after_it() -> None:
    synth = FakeSynth(audio=CUE_AUDIO)
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)
    await engine.prepare_cue("Hmm…")
    cue_audio = synth.audio
    synth.audio = voiced(10, level=4000)

    cue = asyncio.create_task(engine.play_cue("Hmm…"))
    await asyncio.sleep(0)
    await engine.speak("Friday.")
    await cue
    await engine.close()

    assert writer.heard == cue_audio + synth.audio
    assert [(event.text, event.cue) for event in events if event.kind == "playback_finished"] == [
        ("Hmm…", True),
        ("Friday.", False),
    ]


async def test_silence_around_a_rendered_cue_is_trimmed_before_it_is_cached() -> None:
    synth = FakeSynth(audio=silent(40) + CUE_AUDIO + silent(200))
    writer = FakeWriter()
    engine = build_engine(synth, writer, [])

    await engine.prepare_cue("Hmm.")
    await engine.play_cue("Hmm.")
    await engine.close()

    assert writer.heard == silent(2) + CUE_AUDIO + silent(2)


async def test_a_cue_that_keeps_rendering_with_a_long_pause_is_rejected() -> None:
    journal = RecordingJournal()
    synth = FakeSynth(audio=voiced(10) + silent(50) + voiced(10))
    engine = build_engine(synth, FakeWriter(), [], journal=journal)

    await engine.prepare_cue("Let me see.")
    played = await engine.play_cue("Let me see.")
    await engine.close()

    assert played is False
    assert synth.texts == ["Let me see."] * CUE_RENDER_ATTEMPTS
    assert [row["attempts"] for row in journal.events("thinking_cue_rejected")] == [
        CUE_RENDER_ATTEMPTS
    ]
