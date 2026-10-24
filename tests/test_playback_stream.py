import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import Any, override

import pytest

from fakeyou.audio import playback
from fakeyou.audio.playback import PlaybackError, PlaybackEvent
from fakeyou.observability.faults import FaultReporter
from tests._audio_support import FakeMuter, FakePlayer, FakeSynth, build_engine
from tests._persistence_fakes import RecordingJournal


class _StreamSynth:
    def __init__(self, chunks: Iterable[bytes] = (b"\x00" * 100, b"\x01" * 100)) -> None:
        self.chunks = list(chunks)
        self.texts: list[str] = []
        self.closed = False

    async def stream(
        self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
    ) -> AsyncIterator[bytes]:
        del sentence_index
        del transcript_id
        self.texts.append(text)
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _StreamPlayer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.chunks: list[bytes] = []

    async def __call__(self, chunks: AsyncIterator[bytes]) -> None:
        async for chunk in chunks:
            self.chunks.append(chunk)
        if self.error is not None:
            raise self.error


async def test_stream_timeout_before_audio_retries_once() -> None:
    class _TimeoutStreamSynth(_StreamSynth):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        @override
        async def stream(
            self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
        ) -> AsyncIterator[bytes]:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("stream stalled")
            for chunk in self.chunks:
                yield chunk

    synth = _TimeoutStreamSynth()
    streamer = _StreamPlayer()
    events: list[PlaybackEvent] = []
    engine = build_engine(
        synth,
        FakePlayer(),
        FakeMuter(),
        events,
        tail_seconds=0,
        stream_audio=streamer,
    )
    try:
        await engine.speak("retry")
    finally:
        await engine.close()

    assert synth.calls == 2
    assert streamer.chunks == synth.chunks
    assert [event.kind for event in events] == ["playback_started", "playback_finished"]


async def test_stream_timeout_after_audio_does_not_retry() -> None:
    class _PartialTimeoutSynth(_StreamSynth):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        @override
        async def stream(
            self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
        ) -> AsyncIterator[bytes]:
            self.calls += 1
            yield self.chunks[0]
            raise TimeoutError("stream stalled")

    synth = _PartialTimeoutSynth()
    streamer = _StreamPlayer()
    events: list[PlaybackEvent] = []
    engine = build_engine(
        synth,
        FakePlayer(),
        FakeMuter(),
        events,
        tail_seconds=0,
        stream_audio=streamer,
    )
    try:
        await engine.speak("large")
    finally:
        await engine.close()

    assert synth.calls == 1
    assert streamer.chunks == [synth.chunks[0]]
    assert [event.kind for event in events] == ["playback_started", "playback_failed"]


async def test_streaming_speak_pipes_chunks_to_stream_player() -> None:
    synth = _StreamSynth()
    player = FakePlayer()
    streamer = _StreamPlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(
        synth,
        player,
        muter,
        events,
        tail_seconds=0,
        stream_audio=streamer,
    )

    try:
        await engine.speak("hello")
    finally:
        await engine.close()

    assert synth.texts == ["hello"]
    assert streamer.chunks == synth.chunks
    assert player.played == []
    assert [event.kind for event in events] == ["playback_started", "playback_finished"]


async def test_streaming_play_failure_reports_and_engine_recovers(
    journal: RecordingJournal,
) -> None:

    reporter = FaultReporter(journal=journal)
    synth = _StreamSynth()
    player = FakePlayer()
    streamer = _StreamPlayer(error=PlaybackError("paplay stream failed"))
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(
        synth,
        player,
        muter,
        events,
        tail_seconds=0,
        stream_audio=streamer,
        faults=reporter,
    )

    try:
        await engine.speak("broken")
        streamer.error = None
        await engine.speak("recover")
    finally:
        await engine.close()

    names = [event.kind for event in events]
    assert names.count("playback_failed") == 1
    assert names.count("playback_finished") == 1
    assert reporter.total() >= 1
    assert streamer.chunks == synth.chunks + synth.chunks


@pytest.mark.parametrize("outcome", ["failure", "return", "cancel"])
async def test_engine_closes_retained_stream_after_first_chunk(outcome: str) -> None:
    finalized = asyncio.Event()
    started = asyncio.Event()
    retained: list[Any] = []

    class Synth(FakeSynth):
        @override
        async def stream(
            self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
        ) -> AsyncIterator[bytes]:
            try:
                yield b"first"
                yield b"second"
            finally:
                finalized.set()

    async def consume(chunks: AsyncIterator[bytes]) -> None:
        retained.append(chunks)
        assert await anext(chunks) == b"first"
        started.set()
        if outcome == "failure":
            raise PlaybackError("pipe failed")
        if outcome == "cancel":
            await asyncio.Event().wait()

    engine = build_engine(Synth(), consume, FakeMuter(), [], tail_seconds=0)
    speaking = asyncio.create_task(engine.speak("test"))
    try:
        await started.wait()
        if outcome == "cancel":
            await engine.stop()
        await speaking
        assert finalized.is_set()
    finally:
        await engine.close()
        for chunks in retained:
            await chunks.aclose()


async def test_engine_limits_stream_finalizer_to_cleanup_budget() -> None:
    finalized = asyncio.Event()
    retained: list[Any] = []

    class Synth(FakeSynth):
        @override
        async def stream(
            self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
        ) -> AsyncIterator[bytes]:
            try:
                yield b"first"
            finally:
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.set()

    async def consume(chunks: AsyncIterator[bytes]) -> None:
        retained.append(chunks)
        await anext(chunks)
        raise PlaybackError("pipe failed")

    engine = build_engine(
        Synth(), consume, FakeMuter(), [], tail_seconds=0, cleanup_timeout_seconds=0.01
    )
    try:
        await asyncio.wait_for(engine.speak("test"), 1)
        assert finalized.is_set()
    finally:
        await engine.close()
        for chunks in retained:
            await chunks.aclose()


async def test_an_interrupted_paplay_drops_buffered_audio_and_exits_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[asyncio.subprocess.Process] = []
    loop_errors: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: loop_errors.append(context)
    )
    launch = asyncio.create_subprocess_exec

    async def stalled_player(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await launch("sleep", "30", **kwargs)
        spawned.append(proc)
        return proc

    async def endless_audio() -> AsyncIterator[bytes]:
        while True:
            yield bytes(65536)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stalled_player)
    play = asyncio.create_task(
        playback.create_paplay_stream_runner(sink="agent_output", rate=24000)(endless_audio())
    )
    await asyncio.sleep(0.2)
    play.cancel()

    with pytest.raises(asyncio.CancelledError):
        await play
    await asyncio.sleep(0.1)

    [proc] = spawned
    assert proc.returncode is not None
    assert proc.stdin is not None
    assert proc.stdin.transport.is_closing()
    assert loop_errors == []


async def test_stopping_paplay_after_its_input_closed_is_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[asyncio.subprocess.Process] = []
    launch = asyncio.create_subprocess_exec

    async def lingering_player(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await launch("sleep", "30", **kwargs)
        spawned.append(proc)
        return proc

    async def one_chunk() -> AsyncIterator[bytes]:
        yield bytes(1024)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lingering_player)
    play = asyncio.create_task(
        playback.create_paplay_stream_runner(sink="agent_output", rate=24000)(one_chunk())
    )
    await asyncio.sleep(0.2)
    play.cancel()

    with pytest.raises(asyncio.CancelledError):
        await play
    [proc] = spawned
    assert proc.returncode is not None


async def test_a_louder_playback_scales_the_audio() -> None:
    player = _StreamPlayer()
    events: list[PlaybackEvent] = []
    engine = build_engine(_StreamSynth(chunks=[b"\x10\x00" * 10]), player, FakeMuter(), events)

    await engine.speak("hello")
    plain = player.chunks[0]

    engine.louder()
    player.chunks.clear()
    await engine.speak("hello")
    louder = player.chunks[0]

    assert louder != plain
    assert louder == b"\x13\x00" * 10
