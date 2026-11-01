import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import override

import pytest

from fakeyou.audio.playback import FRAME_BYTES, PlaybackError, PlaybackEvent
from fakeyou.observability.faults import FaultReporter
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._persistence_fakes import RecordingJournal
from tests._prepared_support import SentenceSynth


class _StreamSynth(SentenceSynth):
    def __init__(self, chunks: Iterable[bytes] = (b"\x00" * 100, b"\x01" * 100)) -> None:
        self.chunks = list(chunks)
        self.texts: list[str] = []
        self.closed = False

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
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_stream_timeout_before_audio_retries_once() -> None:
    class _TimeoutStreamSynth(_StreamSynth):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        @override
        async def stream(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
            language: str | None = None,
        ) -> AsyncIterator[bytes]:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("stream stalled")
            for chunk in self.chunks:
                yield chunk

    synth = _TimeoutStreamSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)
    try:
        await engine.speak("retry")
    finally:
        await engine.close()

    assert synth.calls == 2
    assert writer.heard == b"".join(synth.chunks)
    assert [event.kind for event in events] == ["playback_started", "playback_finished"]


async def test_stream_timeout_after_audio_does_not_retry() -> None:
    class _PartialTimeoutSynth(_StreamSynth):
        def __init__(self) -> None:
            super().__init__(chunks=[b"\x00" * FRAME_BYTES])
            self.calls = 0

        @override
        async def stream(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
            language: str | None = None,
        ) -> AsyncIterator[bytes]:
            self.calls += 1
            yield self.chunks[0]
            raise TimeoutError("stream stalled")

    synth = _PartialTimeoutSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)
    try:
        await engine.speak("large")
    finally:
        await engine.close()

    assert synth.calls == 1
    assert writer.written == [synth.chunks[0]]
    assert [event.kind for event in events] == ["playback_started", "playback_failed"]


async def test_streaming_speak_writes_the_chunks_to_the_writer() -> None:
    synth = _StreamSynth()
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events)

    try:
        await engine.speak("hello")
    finally:
        await engine.close()

    assert synth.texts == ["hello"]
    assert writer.heard == b"".join(synth.chunks)
    assert [event.kind for event in events] == ["playback_started", "playback_finished"]


async def test_streaming_play_failure_reports_and_engine_recovers(
    journal: RecordingJournal,
) -> None:

    reporter = FaultReporter(journal=journal)
    synth = _StreamSynth()
    writer = FakeWriter(error=PlaybackError("pulse stream failed"))
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, writer, events, faults=reporter)

    try:
        await engine.speak("broken")
        writer.error = None
        await engine.speak("recover")
    finally:
        await engine.close()

    names = [event.kind for event in events]
    assert names.count("playback_failed") == 1
    assert names.count("playback_finished") == 1
    assert reporter.total() >= 1
    assert writer.written == [b"".join(synth.chunks)]


@pytest.mark.parametrize("outcome", ["failure", "return", "cancel"])
async def test_engine_closes_retained_stream_after_first_chunk(outcome: str) -> None:
    finalized = asyncio.Event()

    class Synth(FakeSynth):
        @override
        async def stream(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
            language: str | None = None,
        ) -> AsyncIterator[bytes]:
            try:
                for _ in range(3):
                    yield b"\x00" * FRAME_BYTES
            finally:
                finalized.set()

    writer = FakeWriter(error=PlaybackError("pipe failed") if outcome == "failure" else None)
    if outcome != "failure":
        writer.release.clear()
    engine = build_engine(Synth(), writer, [])
    speaking = asyncio.create_task(engine.speak("test"))
    try:
        await writer.started.wait()
        if outcome == "return":
            stopping = asyncio.create_task(engine.stop())
            await asyncio.sleep(0)
            writer.release.set()
            await stopping
        if outcome == "cancel":
            await engine.stop()
        await speaking
        assert finalized.is_set()
        assert len(writer.written) < 3
    finally:
        writer.release.set()
        await engine.close()


async def test_engine_limits_stream_finalizer_to_cleanup_budget() -> None:
    finalized = asyncio.Event()

    class Synth(FakeSynth):
        @override
        async def stream(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
            language: str | None = None,
        ) -> AsyncIterator[bytes]:
            try:
                yield b"\x00" * FRAME_BYTES
            finally:
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.set()

    engine = build_engine(
        Synth(),
        FakeWriter(error=PlaybackError("pipe failed")),
        [],
        cleanup_timeout_seconds=0.01,
    )
    try:
        await asyncio.wait_for(engine.speak("test"), 1)
        assert finalized.is_set()
    finally:
        await engine.close()


async def test_a_louder_playback_scales_the_audio() -> None:
    writer = FakeWriter(buffer_seconds=30)
    events: list[PlaybackEvent] = []
    engine = build_engine(_StreamSynth(chunks=[b"\x10\x00" * 10]), writer, events)

    await engine.speak("hello")
    plain = writer.written[-1]

    engine.louder()
    await engine.speak("hello")
    louder = writer.written[-1]

    assert louder != plain
    assert louder == b"\x13\x00" * 10
