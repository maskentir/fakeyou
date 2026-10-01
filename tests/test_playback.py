import asyncio
import os
import stat

import pytest

from fakeyou.playback import (
    PlaybackEngine,
    PlaybackError,
    PlaybackEvent,
    create_pactl_muter,
    create_paplay_stream_runner,
)


class _Synth:
    def __init__(self, audio: bytes = b"\x00" * 480, error: Exception | None = None) -> None:
        self.audio = audio
        self.error = error
        self.texts: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.closed = False

    async def stream(
        self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
    ):
        self.texts.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)
        if self.error is not None:
            raise self.error
        yield self.audio

    async def aclose(self) -> None:
        self.closed = True


class _Player:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.played: list[bytes] = []
        self.cancelled: list[bytes] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, chunks) -> None:
        data = b"".join([chunk async for chunk in chunks])
        self.played.append(data)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(data)
            raise
        if self.error is not None:
            raise self.error


class _SlowSynth:
    def __init__(self, audio: bytes = b"\x00" * 480) -> None:
        self.audio = audio
        self.texts: list[str] = []
        self.cancelled = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, text: str):
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


class _Muter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[bool] = []

    async def __call__(self, muted: bool) -> None:
        self.calls.append(muted)
        if self.error is not None:
            raise self.error


def _engine(synth, player, muter, events, **overrides):
    return PlaybackEngine(
        synth,
        stream_audio=overrides.pop("stream_audio", player),
        set_muted=muter,
        on_event=events.append,
        **overrides,
    )


async def test_speak_plays_in_order_and_mutes_around_playback() -> None:
    synth = _Synth()
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0.05)

    await engine.speak("one")
    await engine.speak("two")
    await engine.close()

    assert synth.texts == ["one", "two"]
    assert player.played == [synth.audio, synth.audio]
    assert muter.calls == [False, True]
    assert [event.kind for event in events] == [
        "playback_started",
        "playback_finished",
        "playback_started",
        "playback_finished",
    ]
    assert engine.is_speaking is False
    assert synth.closed


async def test_speak_threads_sentence_index_to_synthesis_in_order() -> None:
    synth = _Synth()
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    try:
        await engine.speak("First sentence.", sentence_index=0)
        await engine.speak("Second sentence.", sentence_index=1)
    finally:
        await engine.close()

    assert synth.texts == ["First sentence.", "Second sentence."]
    assert synth.indices == [0, 1]
    assert player.played == [synth.audio, synth.audio]
    assert [event.kind for event in events] == [
        "playback_started",
        "playback_finished",
        "playback_started",
        "playback_finished",
    ]


async def test_stop_between_sentences_drops_queued_sentence_without_trailing_audio() -> None:
    synth = _Synth()
    player = _Player()
    player.release.clear()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    try:
        first = asyncio.create_task(engine.speak("First sentence.", sentence_index=0))
        await asyncio.wait_for(player.started.wait(), timeout=2)
        second = asyncio.create_task(engine.speak("Second sentence.", sentence_index=1))
        await asyncio.sleep(0)
        await engine.stop()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        assert synth.texts == ["First sentence."]
        assert synth.indices == [0]
        assert player.played == [synth.audio]
        assert player.cancelled == [synth.audio]
        assert engine.is_speaking is False
        player.release.set()
        await engine.speak("Third sentence.", sentence_index=2)
        assert player.played == [synth.audio, synth.audio]
        assert synth.texts == ["First sentence.", "Third sentence."]
        assert synth.indices == [0, 2]
    finally:
        player.release.set()
        await engine.close()

    dropped = [event for event in events if event.text == "Second sentence."]
    assert [(event.kind, event.detail) for event in dropped] == [
        ("playback_dropped", "interrupted")
    ]
    assert [event.kind for event in events].count("playback_dropped") == 2
    assert [event.kind for event in events].count("playback_finished") == 1


async def test_is_speaking_covers_playback_until_tail_elapses() -> None:
    synth = _Synth()
    player = _Player()
    player.release.clear()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=30)

    try:
        assert engine.is_speaking is False
        speaking = asyncio.create_task(engine.speak("hello"))
        await asyncio.wait_for(player.started.wait(), timeout=2)
        assert engine.is_speaking is True
        player.release.set()
        await asyncio.wait_for(speaking, timeout=2)
        assert engine.is_speaking is True
        assert muter.calls == [False]
    finally:
        player.release.set()
        await engine.close()


async def test_stop_kills_in_flight_playback_and_keeps_engine_usable() -> None:
    synth = _Synth()
    player = _Player()
    player.release.clear()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    try:
        speaking = asyncio.create_task(engine.speak("long"))
        await asyncio.wait_for(player.started.wait(), timeout=2)
        queued = asyncio.create_task(engine.speak("next"))
        await asyncio.sleep(0)
        await engine.stop()
        await asyncio.wait_for(asyncio.gather(speaking, queued), timeout=2)
        assert player.cancelled == [synth.audio]
        assert engine.is_speaking is False
        player.release.set()
        await engine.speak("again")
        assert player.played == [synth.audio, synth.audio]
    finally:
        player.release.set()
        await engine.close()

    names = [event.kind for event in events]
    assert names.count("playback_dropped") == 2
    assert "playback_finished" in names


async def test_synthesis_failure_reports_event_and_keeps_queue_alive() -> None:
    synth = _Synth(error=TimeoutError("stream stalled"))
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    try:
        await engine.speak("fail")
        synth.error = None
        await engine.speak("recover")
        assert player.played == [synth.audio]
    finally:
        await engine.close()

    failed = [event for event in events if event.kind == "playback_failed"]
    assert len(failed) == 1
    assert failed[0].text == "fail"
    assert "tts timeout" in failed[0].detail
    assert "playback timed out" not in failed[0].detail


async def test_playback_failure_reports_event_and_drains_queue() -> None:
    synth = _Synth()
    player = _Player(error=PlaybackError("paplay exited with 1"))
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    await engine.speak("broken")
    await engine.close()

    assert [event.kind for event in events] == ["playback_started", "playback_failed"]
    assert muter.calls == [False, True]
    assert engine.is_speaking is False


async def test_mute_failure_reports_event_and_playback_continues() -> None:
    synth = _Synth()
    player = _Player()
    muter = _Muter(error=PlaybackError("pactl failed"))
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=30)

    await engine.speak("listen")
    assert player.played == [synth.audio]
    await engine.close()

    assert [event.kind for event in events].count("mute_failed") == 2


async def test_cancelled_speak_drops_from_queue() -> None:
    synth = _Synth()
    player = _Player()
    player.release.clear()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    try:
        first = asyncio.create_task(engine.speak("one"))
        await asyncio.wait_for(player.started.wait(), timeout=2)
        second = asyncio.create_task(engine.speak("two"))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        player.release.set()
        await asyncio.wait_for(first, timeout=2)
        assert player.played == [synth.audio]
    finally:
        player.release.set()
        await engine.close()


async def test_close_is_idempotent_and_blocks_late_speaks() -> None:
    synth = _Synth()
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    await engine.speak("one")
    await engine.close()
    await engine.close()
    await engine.speak("two")

    assert synth.texts == ["one"]
    assert synth.closed


async def _collect(chunks):
    for chunk in chunks:
        yield chunk


async def test_paplay_stream_runner_pipes_chunks_and_argv(tmp_path, monkeypatch) -> None:
    script = tmp_path / "paplay"
    script.write_text(
        '#!/bin/sh\necho "$@" > "$FAKE_PAPLAY_ARGS"\ncat > "$FAKE_PAPLAY_STDIN"\nexit 0\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    args_file = tmp_path / "args"
    stdin_file = tmp_path / "stdin"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PAPLAY_ARGS", str(args_file))
    monkeypatch.setenv("FAKE_PAPLAY_STDIN", str(stdin_file))

    await create_paplay_stream_runner(sink="agent_output", rate=24000)(
        _collect([b"\x01", b"\x02\x03", b"\x04"])
    )

    assert args_file.read_text().split() == [
        "--device=agent_output",
        "--format=s16le",
        "--rate=24000",
        "--channels=1",
        "--raw",
    ]
    assert stdin_file.read_bytes() == b"\x01\x02\x03\x04"


async def test_paplay_stream_runner_broken_pipe_is_fault(tmp_path, monkeypatch) -> None:
    script = tmp_path / "paplay"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    async def flood():
        for _ in range(300):
            yield b"\x00" * 4096

    with pytest.raises(PlaybackError, match="paplay stream failed"):
        await create_paplay_stream_runner(sink="agent_output", rate=24000)(flood())


async def test_paplay_stream_runner_broken_pipe_during_cancel_is_cleanup(
    tmp_path, monkeypatch
) -> None:
    script = tmp_path / "paplay"
    script.write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    processes = []
    create_subprocess = asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        proc = await create_subprocess(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)

    async def fail_while_cancelling():
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        raise BrokenPipeError(32, "Broken pipe")
        yield b""

    try:
        with pytest.raises(asyncio.CancelledError):
            await create_paplay_stream_runner(sink="agent_output", rate=24000)(
                fail_while_cancelling()
            )
        assert processes[0].returncode is not None
        assert processes[0].stdin.is_closing()
    finally:
        await processes[0].wait()


async def test_paplay_stream_runner_cleans_up_source_failure(tmp_path, monkeypatch) -> None:
    script = tmp_path / "paplay"
    script.write_text('#!/bin/sh\necho $$ > "$FAKE_PAPLAY_PID"\nexec sleep 30\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PAPLAY_PID", str(pid_file))

    async def fail():
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("paplay did not start")
        raise RuntimeError("source failed")
        yield b""

    with pytest.raises(RuntimeError, match="source failed"):
        await create_paplay_stream_runner(sink="agent_output", rate=24000)(fail())
    pid = int(pid_file.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("paplay process survived source failure")


async def test_pactl_muter_reports_missing_binary() -> None:
    apply = create_pactl_muter(sink="agent_output")

    with pytest.raises(PlaybackError, match="pactl"):
        await apply(True)


async def test_pactl_muter_toggles_sink_only(tmp_path, monkeypatch) -> None:
    script = tmp_path / "pactl"
    script.write_text('#!/bin/sh\necho "$@" >> "$FAKE_PACTL_CALLS"\nexit 0\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PACTL_CALLS", str(calls))

    apply = create_pactl_muter(sink="agent_output")
    await apply(True)
    await apply(False)

    assert calls.read_text().splitlines() == [
        "set-sink-mute agent_output 1",
        "set-sink-mute agent_output 0",
    ]


async def test_pactl_muter_reaps_child_on_cancel(tmp_path, monkeypatch) -> None:
    script = tmp_path / "pactl"
    script.write_text('#!/bin/sh\necho $$ > "$FAKE_PACTL_PID"\nexec sleep 30\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PACTL_PID", str(pid_file))

    applying = asyncio.create_task(create_pactl_muter(sink="agent_output")(True))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("pactl did not start")
    applying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await applying
    pid = int(pid_file.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("pactl process survived cancellation")


async def test_stop_during_synthesis_drops_without_playing() -> None:
    synth = _SlowSynth()
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    speaking = asyncio.create_task(engine.speak("delayed"))
    await synth.started.wait()
    await engine.stop()
    await asyncio.wait_for(speaking, timeout=5)
    await engine.close()

    assert synth.cancelled
    assert player.played == []
    assert [event.kind for event in events] == ["playback_started", "playback_dropped"]
    assert events[-1].detail == "interrupted"


async def test_speak_retry_is_journaled(
    journal,
) -> None:
    import json

    synth = _Synth(error=TimeoutError("slow"))
    player = _Player()
    muter = _Muter()
    lines = journal.lines
    engine = _engine(synth, player, muter, [], tail_seconds=0, journal=journal)
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
    synth = _Synth()
    player = _Player()
    player.release.clear()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)
    try:
        speaking = asyncio.create_task(engine.speak("cancel"))
        await player.started.wait()
        speaking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await speaking
        await asyncio.sleep(0)
        assert player.cancelled == [synth.audio]
    finally:
        player.release.set()
        await engine.close()


async def test_external_cancel_during_synthesis_propagates() -> None:
    synth = _SlowSynth()
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)

    speaking = asyncio.create_task(engine.speak("cancel"))
    await synth.started.wait()
    speaking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await speaking
    await engine.close()

    assert synth.cancelled
    assert player.played == []
    assert all(event.kind in {"playback_started", "playback_dropped"} for event in events)


async def test_typed_synth_fault_records_and_engine_recovers(
    journal,
) -> None:
    from fakeyou.faults import FaultReporter
    from fakeyou.provider_faults import ProviderFault

    reporter = FaultReporter(journal=journal)
    synth = _Synth(error=ProviderFault("tts", "timeout", detail="slow", retryable=True))
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth,
        stream_audio=player,
        set_muted=muter,
        on_event=events.append,
        tail_seconds=0,
        faults=reporter,
    )

    try:
        await engine.speak("fail")
        synth.error = None
        await engine.speak("recover")
        assert player.played == [synth.audio]
    finally:
        await engine.close()

    assert [event.kind for event in events if event.kind == "playback_failed"] != []
    report = reporter.report()
    assert report is not None
    assert report["faults"]["kinds"][0]["kind"] == "timeout"


async def test_play_failure_records_and_worker_survives(
    journal,
) -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(journal=journal)
    synth = _Synth()
    player = _Player(error=PlaybackError("paplay exited with 1"))
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth,
        stream_audio=player,
        set_muted=muter,
        on_event=events.append,
        tail_seconds=0,
        faults=reporter,
    )

    try:
        await engine.speak("broken")
        player.error = None
        await engine.speak("recover")
        assert player.played == [synth.audio, synth.audio]
    finally:
        await engine.close()

    names = [event.kind for event in events]
    assert names.count("playback_failed") == 1
    assert names.count("playback_finished") == 1
    assert reporter.total() >= 1


class _StreamSynth:
    def __init__(self, chunks=(b"\x00" * 100, b"\x01" * 100)) -> None:
        self.chunks = list(chunks)
        self.texts: list[str] = []
        self.closed = False

    async def stream(self, text: str):
        self.texts.append(text)
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _StreamPlayer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.chunks: list[bytes] = []

    async def __call__(self, chunks) -> None:
        async for chunk in chunks:
            self.chunks.append(chunk)
        if self.error is not None:
            raise self.error


async def test_stream_timeout_before_audio_retries_once() -> None:
    class _TimeoutStreamSynth(_StreamSynth):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def stream(self, text: str):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("stream stalled")
            for chunk in self.chunks:
                yield chunk

    synth = _TimeoutStreamSynth()
    streamer = _StreamPlayer()
    events: list[PlaybackEvent] = []
    engine = _engine(
        synth,
        _Player(),
        _Muter(),
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

        async def stream(self, text: str):
            self.calls += 1
            yield self.chunks[0]
            raise TimeoutError("stream stalled")

    synth = _PartialTimeoutSynth()
    streamer = _StreamPlayer()
    events: list[PlaybackEvent] = []
    engine = _engine(
        synth,
        _Player(),
        _Muter(),
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
    player = _Player()
    streamer = _StreamPlayer()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(
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
    journal,
) -> None:
    from fakeyou.faults import FaultReporter

    reporter = FaultReporter(journal=journal)
    synth = _StreamSynth()
    player = _Player()
    streamer = _StreamPlayer(error=PlaybackError("paplay stream failed"))
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(
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
async def test_engine_closes_retained_stream_after_first_chunk(outcome):
    finalized = asyncio.Event()
    started = asyncio.Event()
    retained = []

    class Synth(_Synth):
        async def stream(self, text):
            try:
                yield b"first"
                yield b"second"
            finally:
                finalized.set()

    async def consume(chunks):
        retained.append(chunks)
        assert await anext(chunks) == b"first"
        started.set()
        if outcome == "failure":
            raise PlaybackError("pipe failed")
        if outcome == "cancel":
            await asyncio.Event().wait()

    engine = _engine(Synth(), consume, _Muter(), [], tail_seconds=0)
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


@pytest.mark.parametrize("drop", ["stop", "cancel", "unmute_stop"])
async def test_engine_closes_dropped_pcm_iterators(drop):
    started = asyncio.Event()
    release = asyncio.Event()

    class Chunks:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    async def consume(chunks):
        started.set()
        await release.wait()

    async def mute(muted):
        if not muted and drop == "unmute_stop":
            await engine.stop()

    engine = _engine(_Synth(), consume, mute, [], tail_seconds=0)
    chunks = Chunks()
    try:
        if drop != "unmute_stop":
            active = asyncio.create_task(engine.speak("active"))
            await started.wait()
        queued = asyncio.create_task(engine._play_queued(chunks, "queued"))
        await asyncio.sleep(0)
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
        assert chunks.closed
    finally:
        release.set()
        await engine.close()


async def test_engine_limits_stream_finalizer_to_cleanup_budget():
    finalized = asyncio.Event()
    retained = []

    class Synth(_Synth):
        async def stream(self, text):
            try:
                yield b"first"
            finally:
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.set()

    async def consume(chunks):
        retained.append(chunks)
        await anext(chunks)
        raise PlaybackError("pipe failed")

    engine = _engine(Synth(), consume, _Muter(), [], tail_seconds=0, cleanup_timeout_seconds=0.01)
    try:
        await asyncio.wait_for(engine.speak("test"), 1)
        assert finalized.is_set()
    finally:
        await engine.close()
        for chunks in retained:
            await chunks.aclose()


async def test_engine_closes_dequeued_iterator_when_unmute_is_cancelled():
    entered = asyncio.Event()

    class Chunks:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    async def mute(muted):
        if not muted:
            entered.set()
            await asyncio.Event().wait()

    chunks = Chunks()
    engine = _engine(_Synth(), _Player(), mute, [], tail_seconds=0)
    queued = asyncio.create_task(engine._play_queued(chunks, "test"))
    await entered.wait()
    try:
        await engine.close()
        assert chunks.closed
        await asyncio.wait_for(queued, 1)
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)


async def test_engine_speak_forwards_transcript_id_to_synthesizer() -> None:
    synth = _Synth()
    player = _Player()
    muter = _Muter()
    events: list[PlaybackEvent] = []
    engine = _engine(synth, player, muter, events, tail_seconds=0)
    try:
        await engine.speak("Hello.", sentence_index=0, transcript_id="t9")
        assert synth.texts == ["Hello."]
        assert synth.indices == [0]
        assert synth.transcripts == ["t9"]
    finally:
        await engine.close()
