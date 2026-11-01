import asyncio
from collections.abc import AsyncIterator
from typing import override

import numpy as np
import pytest

from fakeyou.audio.playback import (
    FRAME_BYTES,
    PlaybackEngine,
    PlaybackError,
    PlaybackEvent,
    PlaybackTiming,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._persistence_fakes import RecordingJournal
from tests._prepared_support import SentenceSynth


class _DrainingWriter(FakeWriter):
    def __init__(self, *, buffer_seconds: float = 0.0) -> None:
        super().__init__(buffer_seconds=buffer_seconds)
        self.draining = asyncio.Event()
        self.drain_release = asyncio.Event()

    @override
    async def drain(self) -> None:
        self.drains += 1
        self.draining.set()
        await self.drain_release.wait()


class _SlowSynth(SentenceSynth):
    def __init__(self, audio: bytes = b"\x00" * 480) -> None:
        self.audio = audio
        self.texts: list[str] = []
        self.cancelled = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del language
        del sentence_index
        del transcript_id
        self.texts.append(text)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield self.audio

    async def aclose(self) -> None:
        pass


async def test_speak_plays_in_order_as_one_voice() -> None:
    synth = FakeSynth()
    writer = FakeWriter(buffer_seconds=30)
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    await engine.speak("one")
    await engine.speak("two")
    await engine.close()

    assert synth.texts == ["one", "two"]
    assert writer.written == [synth.audio, synth.audio]
    assert writer.drains == 1
    assert writer.closed
    assert [event.kind for event in events] == [
        "playback_started",
        "playback_finished",
        "playback_started",
        "playback_finished",
    ]
    assert engine.is_speaking is False
    assert synth.closed


async def test_speak_threads_sentence_index_to_synthesis_in_order() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        await engine.speak("First sentence.", sentence_index=0)
        await engine.speak("Second sentence.", sentence_index=1)
    finally:
        await engine.close()

    assert synth.texts == ["First sentence.", "Second sentence."]
    assert synth.indices == [0, 1]
    assert writer.written == [synth.audio, synth.audio]
    assert [event.kind for event in events] == [
        "playback_started",
        "playback_finished",
        "playback_started",
        "playback_finished",
    ]


async def test_stop_between_sentences_drops_queued_sentence_without_trailing_audio() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        first = asyncio.create_task(engine.speak("First sentence.", sentence_index=0))
        await asyncio.wait_for(writer.started.wait(), timeout=2)
        second = asyncio.create_task(engine.speak("Second sentence.", sentence_index=1))
        await asyncio.sleep(0)
        await engine.stop()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        assert synth.texts == ["First sentence."]
        assert synth.indices == [0]
        assert writer.written == []
        assert writer.cancelled == [synth.audio]
        assert engine.is_speaking is False
        writer.release.set()
        await engine.speak("Third sentence.", sentence_index=2)
        assert writer.written == [synth.audio]
        assert synth.texts == ["First sentence.", "Third sentence."]
        assert synth.indices == [0, 2]
    finally:
        writer.release.set()
        await engine.close()

    dropped = [event for event in events if event.text == "Second sentence."]
    assert [(event.kind, event.detail) for event in dropped] == [
        ("playback_dropped", "interrupted")
    ]
    assert [event.kind for event in events].count("playback_dropped") == 2
    assert [event.kind for event in events].count("playback_finished") == 1


async def test_is_speaking_covers_playback_until_the_buffer_plays_out() -> None:
    synth = FakeSynth()
    writer = FakeWriter(buffer_seconds=30)
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        assert engine.is_speaking is False
        speaking = asyncio.create_task(engine.speak("hello"))
        await asyncio.wait_for(writer.started.wait(), timeout=2)
        assert engine.is_speaking is True
        writer.release.set()
        await asyncio.wait_for(speaking, timeout=2)
        assert engine.is_speaking is True
        assert writer.drains == 0
    finally:
        writer.release.set()
        await engine.close()


async def test_stop_kills_in_flight_playback_and_keeps_engine_usable() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        speaking = asyncio.create_task(engine.speak("long"))
        await asyncio.wait_for(writer.started.wait(), timeout=2)
        queued = asyncio.create_task(engine.speak("next"))
        await asyncio.sleep(0)
        await engine.stop()
        await asyncio.wait_for(asyncio.gather(speaking, queued), timeout=2)
        assert writer.cancelled == [synth.audio]
        assert engine.is_speaking is False
        writer.release.set()
        await engine.speak("again")
        assert writer.written == [synth.audio]
    finally:
        writer.release.set()
        await engine.close()

    names = [event.kind for event in events]
    assert names.count("playback_dropped") == 2
    assert "playback_finished" in names


async def test_synthesis_failure_reports_event_and_keeps_queue_alive() -> None:
    synth = FakeSynth(error=TimeoutError("stream stalled"))
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        await engine.speak("fail")
        synth.error = None
        await engine.speak("recover")
        assert writer.written == [synth.audio]
    finally:
        await engine.close()

    failed = [event for event in events if event.kind == "playback_failed"]
    assert len(failed) == 1
    assert failed[0].text == "fail"
    assert "tts timeout" in failed[0].detail
    assert "playback timed out" not in failed[0].detail


async def test_playback_failure_reports_event_and_drains_queue() -> None:
    synth = FakeSynth()
    writer = FakeWriter(error=PlaybackError("pulse write failed"))
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    await engine.speak("broken")
    await engine.close()

    assert [event.kind for event in events] == ["playback_started", "playback_failed"]
    assert writer.drains == 1
    assert engine.is_speaking is False


async def test_cancelled_speak_drops_from_queue() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        first = asyncio.create_task(engine.speak("one"))
        await asyncio.wait_for(writer.started.wait(), timeout=2)
        second = asyncio.create_task(engine.speak("two"))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        writer.release.set()
        await asyncio.wait_for(first, timeout=2)
        assert writer.written == [synth.audio]
    finally:
        writer.release.set()
        await engine.close()


async def test_close_is_idempotent_and_blocks_late_speaks() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    await engine.speak("one")
    await engine.close()
    await engine.close()
    await engine.speak("two")

    assert synth.texts == ["one"]
    assert synth.closed


async def test_stop_during_synthesis_drops_without_playing() -> None:
    synth = _SlowSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    speaking = asyncio.create_task(engine.speak("delayed"))
    await synth.started.wait()
    await engine.stop()
    await asyncio.wait_for(speaking, timeout=5)
    await engine.close()

    assert synth.cancelled
    assert writer.written == []
    assert [event.kind for event in events] == ["playback_started", "playback_dropped"]
    assert events[-1].detail == "interrupted"


async def test_speak_retry_is_journaled(
    journal: RecordingJournal,
) -> None:
    import json

    synth = FakeSynth(error=TimeoutError("slow"))
    lines = journal.lines
    engine = build_engine(synth, FakeWriter(), [], journal=journal)
    try:
        await engine.speak("retry")
    finally:
        await engine.close()

    retries = [json.loads(line) for line in lines if '"event":"synthesis_retried"' in line]
    assert retries == [
        {
            "event": "synthesis_retried",
            "meeting_id": "meeting-1",
            "at": retries[0]["at"],
            "source": "playback",
            "attempt": 2,
            "streaming": True,
            "reason": "timeout",
        }
    ]


async def test_cancelling_speak_cancels_active_playback() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)
    try:
        speaking = asyncio.create_task(engine.speak("cancel"))
        await writer.started.wait()
        speaking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await speaking
        await asyncio.sleep(0)
        assert writer.cancelled == [synth.audio]
    finally:
        writer.release.set()
        await engine.close()


async def test_external_cancel_during_synthesis_propagates() -> None:
    synth = _SlowSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    speaking = asyncio.create_task(engine.speak("cancel"))
    await synth.started.wait()
    speaking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await speaking
    await engine.close()

    assert synth.cancelled
    assert writer.written == []
    assert all(event.kind in {"playback_started", "playback_dropped"} for event in events)


async def test_typed_synth_fault_records_and_engine_recovers(
    journal: RecordingJournal,
) -> None:
    from fakeyou.observability.diagnostics import Diagnostics
    from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault

    reporter = FaultReporter(journal=journal)
    synth = FakeSynth(
        error=ProviderFault("tts", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth, writer, on_event=events.append, diagnostics=Diagnostics(faults=reporter)
    )

    try:
        await engine.speak("fail")
        synth.error = None
        await engine.speak("recover")
        assert writer.written == [synth.audio]
    finally:
        await engine.close()

    assert [event.kind for event in events if event.kind == "playback_failed"] != []
    report = reporter.report()
    assert report is not None
    assert report["faults"]["kinds"][0]["kind"] == "timeout"


async def test_play_failure_records_and_worker_survives(
    journal: RecordingJournal,
) -> None:

    reporter = FaultReporter(journal=journal)
    synth = FakeSynth()
    writer = FakeWriter(error=PlaybackError("pulse write failed"))
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth, writer, on_event=events.append, diagnostics=Diagnostics(faults=reporter)
    )

    try:
        await engine.speak("broken")
        writer.error = None
        await engine.speak("recover")
        assert writer.written == [synth.audio]
    finally:
        await engine.close()

    names = [event.kind for event in events]
    assert names.count("playback_failed") == 1
    assert names.count("playback_finished") == 1
    assert reporter.total() >= 1


async def _drop_queued(
    engine: PlaybackEngine,
    queued: asyncio.Task[None],
    active: asyncio.Task[None],
    *,
    drop: str,
    release: asyncio.Event,
) -> None:
    if drop == "stop":
        await engine.stop()
    elif drop == "cancel":
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await active
    if drop != "cancel":
        await queued
    if drop == "stop":
        await active


@pytest.mark.parametrize("drop", ["stop", "cancel"])
async def test_engine_never_synthesizes_dropped_speech(drop: str) -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)
    try:
        active = asyncio.create_task(engine.speak("active"))
        await writer.started.wait()
        queued = asyncio.create_task(engine.speak("queued"))
        await asyncio.sleep(0)
        await _drop_queued(engine, queued, active, drop=drop, release=writer.release)
        assert "queued" not in synth.texts
        dropped = [(event.kind, event.text) for event in events]
        assert (("playback_dropped", "queued") in dropped) is (drop != "cancel")
    finally:
        writer.release.set()
        await engine.close()


async def test_engine_speak_forwards_transcript_id_to_synthesizer() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)
    try:
        await engine.speak("Hello.", sentence_index=0, transcript_id="t9")
        assert synth.texts == ["Hello."]
        assert synth.indices == [0]
        assert synth.transcripts == ["t9"]
    finally:
        await engine.close()


class _BrokenCloseSynth(FakeSynth):
    @override
    async def aclose(self) -> None:
        raise OSError("close failed")


async def test_close_synth_failure_is_recorded(journal: RecordingJournal) -> None:

    reporter = FaultReporter(journal=journal)
    synth = _BrokenCloseSynth()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, FakeWriter(buffer_seconds=30), events, faults=reporter)

    await engine.close()

    assert reporter.total() >= 1


async def test_a_slow_consumer_does_not_count_against_synthesis() -> None:
    class ChunkedSynth(SentenceSynth):
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
            for _ in range(3):
                yield b"\x00" * FRAME_BYTES

        async def aclose(self) -> None:
            return None

    class SlowWriter(FakeWriter):
        @override
        async def write(self, pcm: bytes) -> None:
            await asyncio.sleep(0.3)
            await super().write(pcm)

    events: list[PlaybackEvent] = []
    writer = SlowWriter()
    engine = PlaybackEngine(
        ChunkedSynth(),
        writer,
        on_event=events.append,
        timing=PlaybackTiming(cleanup_timeout_seconds=5.0, synth_timeout_seconds=0.1),
    )

    try:
        await engine.speak("hello")
    finally:
        await engine.close()

    assert writer.heard == b"\x00" * FRAME_BYTES * 3
    assert [event for event in events if event.kind == "playback_failed"] == []


async def test_text_with_nothing_to_say_never_reaches_the_synthesizer() -> None:
    synth = FakeSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        await engine.speak(".", sentence_index=0)
        await engine.speak("—", sentence_index=1)
        await engine.speak("Yes.", sentence_index=2)
    finally:
        await engine.close()

    assert synth.texts == ["Yes."]
    assert [(event.kind, event.text, event.detail) for event in events[:2]] == [
        ("playback_dropped", ".", "nothing_to_say"),
        ("playback_dropped", "—", "nothing_to_say"),
    ]


async def test_louder_scales_the_written_samples_and_resets_after_idle() -> None:
    synth = FakeSynth(audio=b"\xe8\x03" * 240)
    writer = FakeWriter(buffer_seconds=0.01)
    engine = build_engine(synth, writer, [])

    try:
        engine.louder()
        await engine.speak("one")
        await asyncio.sleep(0.05)
        await engine.speak("two")
    finally:
        await engine.close()

    first = np.frombuffer(writer.written[0], dtype="<i2")
    second = np.frombuffer(writer.written[1], dtype="<i2")
    assert first.tolist() == [1200] * 240
    assert second.tolist() == [1000] * 240


async def test_louder_applies_to_the_next_reply_only() -> None:
    synth = FakeSynth(audio=b"\xe8\x03" * 240)
    writer = FakeWriter(buffer_seconds=0.01)
    engine = build_engine(synth, writer, [])

    try:
        await engine.speak("one")
        await asyncio.sleep(0.05)
        engine.louder()
        await engine.speak("two")
        await asyncio.sleep(0.05)
        await engine.speak("three")
    finally:
        await engine.close()

    values = [int(np.frombuffer(chunk, dtype="<i2")[0]) for chunk in writer.written]
    assert values == [1000, 1200, 1000]


async def test_a_cancelled_speak_fades_the_audio_out() -> None:
    synth = FakeSynth(audio=b"\xe8\x03" * 240)
    writer = FakeWriter()
    writer.release.clear()
    engine = build_engine(synth, writer, [])

    try:
        speaking = asyncio.create_task(engine.speak("cancel"))
        await asyncio.wait_for(writer.started.wait(), timeout=2)
        speaking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await speaking
        writer.release.set()
        await asyncio.sleep(0.05)
        assert writer.cancelled == [synth.audio]
        assert len(writer.written) == 1
        fade = np.frombuffer(writer.written[0], dtype="<i2")
        assert len(fade) == 240
        assert fade[0] == 1000
        assert fade[-1] == 0
        assert (np.diff(fade.astype(np.int32)) <= 0).all()
    finally:
        writer.release.set()
        await engine.close()


async def test_stop_drops_a_sentence_queued_while_it_is_halting() -> None:
    synth = FakeSynth()
    writer = FakeWriter(buffer_seconds=30)
    writer.release.clear()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        first = asyncio.create_task(engine.speak("one"))
        await asyncio.wait_for(writer.started.wait(), timeout=2)
        stopping = asyncio.create_task(engine.stop())
        await asyncio.sleep(0)
        late = asyncio.create_task(engine.speak("two"))
        await asyncio.sleep(0)
        await asyncio.wait_for(stopping, timeout=2)
        writer.release.set()
        await asyncio.wait_for(asyncio.gather(first, late), timeout=2)
        assert writer.written == []
        assert engine.is_speaking is False
    finally:
        writer.release.set()
        await engine.close()

    assert "two" not in synth.texts
    assert [(event.kind, event.text) for event in events if event.text == "two"] == [
        ("playback_dropped", "two")
    ]


async def test_stop_silences_a_sentence_queued_while_it_is_draining() -> None:
    synth = FakeSynth()
    writer = _DrainingWriter(buffer_seconds=30)
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        await engine.speak("one")
        assert writer.written == [synth.audio]
        stopping = asyncio.create_task(engine.stop())
        await asyncio.wait_for(writer.draining.wait(), timeout=2)
        late = asyncio.create_task(engine.speak("two"))
        await asyncio.sleep(0)
        writer.drain_release.set()
        await asyncio.wait_for(stopping, timeout=2)
        await asyncio.wait_for(late, timeout=2)
        assert writer.written == [synth.audio]
        assert engine.is_speaking is False
    finally:
        writer.drain_release.set()
        await engine.close()

    assert [(event.kind, event.text) for event in events if event.text == "two"] == [
        ("playback_dropped", "two")
    ]
