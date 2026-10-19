import asyncio
from collections.abc import AsyncIterator
from typing import override

import pytest

from fakeyou.audio.playback import (
    PlaybackBackend,
    PlaybackEngine,
    PlaybackError,
    PlaybackEvent,
    PlaybackTiming,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._audio_support import FakeMuter, FakePlayer, FakeSynth, build_engine
from tests._persistence_fakes import RecordingJournal


class _SlowSynth:
    def __init__(self, audio: bytes = b"\x00" * 480) -> None:
        self.audio = audio
        self.texts: list[str] = []
        self.cancelled = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
    ) -> AsyncIterator[bytes]:
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


async def test_speak_plays_in_order_and_mutes_around_playback() -> None:
    synth = FakeSynth()
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0.05)

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
    synth = FakeSynth()
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

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
    synth = FakeSynth()
    player = FakePlayer()
    player.release.clear()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

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
    synth = FakeSynth()
    player = FakePlayer()
    player.release.clear()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=30)

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
    synth = FakeSynth()
    player = FakePlayer()
    player.release.clear()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

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
    synth = FakeSynth(error=TimeoutError("stream stalled"))
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

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
    synth = FakeSynth()
    player = FakePlayer(error=PlaybackError("paplay exited with 1"))
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

    await engine.speak("broken")
    await engine.close()

    assert [event.kind for event in events] == ["playback_started", "playback_failed"]
    assert muter.calls == [False, True]
    assert engine.is_speaking is False


async def test_mute_failure_reports_event_and_playback_continues() -> None:
    synth = FakeSynth()
    player = FakePlayer()
    muter = FakeMuter(error=PlaybackError("pactl failed"))
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=30)

    await engine.speak("listen")
    assert player.played == [synth.audio]
    await engine.close()

    assert [event.kind for event in events].count("mute_failed") == 2


async def test_cancelled_speak_drops_from_queue() -> None:
    synth = FakeSynth()
    player = FakePlayer()
    player.release.clear()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

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
    synth = FakeSynth()
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

    await engine.speak("one")
    await engine.close()
    await engine.close()
    await engine.speak("two")

    assert synth.texts == ["one"]
    assert synth.closed


async def test_stop_during_synthesis_drops_without_playing() -> None:
    synth = _SlowSynth()
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

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
    journal: RecordingJournal,
) -> None:
    import json

    synth = FakeSynth(error=TimeoutError("slow"))
    player = FakePlayer()
    muter = FakeMuter()
    lines = journal.lines
    engine = build_engine(synth, player, muter, [], tail_seconds=0, journal=journal)
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
    player = FakePlayer()
    player.release.clear()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)
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
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)

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
    journal: RecordingJournal,
) -> None:
    from fakeyou.observability.diagnostics import Diagnostics
    from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault

    reporter = FaultReporter(journal=journal)
    synth = FakeSynth(
        error=ProviderFault("tts", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth,
        PlaybackBackend(muter, player),
        on_event=events.append,
        timing=PlaybackTiming(tail_seconds=0),
        diagnostics=Diagnostics(faults=reporter),
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
    journal: RecordingJournal,
) -> None:

    reporter = FaultReporter(journal=journal)
    synth = FakeSynth()
    player = FakePlayer(error=PlaybackError("paplay exited with 1"))
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth,
        PlaybackBackend(muter, player),
        on_event=events.append,
        timing=PlaybackTiming(tail_seconds=0),
        diagnostics=Diagnostics(faults=reporter),
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


async def _drop_queued(
    engine: PlaybackEngine,
    queued: asyncio.Task[None],
    active: asyncio.Task[None] | None,
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
        assert active is not None
        await active
    if drop != "cancel":
        await queued
    if drop == "stop":
        assert active is not None
        await active


@pytest.mark.parametrize("drop", ["stop", "cancel", "unmute_stop"])
async def test_engine_never_synthesizes_dropped_speech(drop: str) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def consume(_chunks: AsyncIterator[bytes]) -> None:
        started.set()
        await release.wait()

    async def mute(*, muted: bool) -> None:
        if not muted and drop == "unmute_stop":
            await engine.stop()

    synth = FakeSynth()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, consume, mute, events, tail_seconds=0)
    active: asyncio.Task[None] | None = None
    try:
        if drop != "unmute_stop":
            active = asyncio.create_task(engine.speak("active"))
            await started.wait()
        queued = asyncio.create_task(engine.speak("queued"))
        await asyncio.sleep(0)
        await _drop_queued(engine, queued, active, drop=drop, release=release)
        assert "queued" not in synth.texts
        dropped = [(event.kind, event.text) for event in events]
        assert (("playback_dropped", "queued") in dropped) is (drop != "cancel")
    finally:
        release.set()
        await engine.close()


async def test_engine_releases_dequeued_speech_when_unmute_is_cancelled() -> None:
    entered = asyncio.Event()

    async def mute(*, muted: bool) -> None:
        if not muted:
            entered.set()
            await asyncio.Event().wait()

    synth = FakeSynth()
    engine = build_engine(synth, FakePlayer(), mute, [], tail_seconds=0)
    queued = asyncio.create_task(engine.speak("test"))
    await entered.wait()
    try:
        await engine.close()
        await asyncio.wait_for(queued, 1)
        assert synth.texts == []
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)


async def test_engine_speak_forwards_transcript_id_to_synthesizer() -> None:
    synth = FakeSynth()
    player = FakePlayer()
    muter = FakeMuter()
    events: list[PlaybackEvent] = []
    engine = build_engine(synth, player, muter, events, tail_seconds=0)
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
    engine = build_engine(
        synth, FakePlayer(), FakeMuter(), events, tail_seconds=30, faults=reporter
    )

    await engine.close()

    assert reporter.total() >= 1


async def test_a_slow_consumer_does_not_count_against_synthesis() -> None:
    class ChunkedSynth:
        async def stream(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
        ) -> AsyncIterator[bytes]:
            del text, sentence_index, transcript_id
            for _ in range(3):
                yield b"\x00" * 480

        async def aclose(self) -> None:
            return None

    class PacedPlayer:
        def __init__(self) -> None:
            self.played: list[bytes] = []

        async def __call__(self, chunks: AsyncIterator[bytes]) -> None:
            async for chunk in chunks:
                await asyncio.sleep(0.3)
                self.played.append(chunk)

    events: list[PlaybackEvent] = []
    player = PacedPlayer()
    engine = PlaybackEngine(
        ChunkedSynth(),
        PlaybackBackend(FakeMuter(), player),
        on_event=events.append,
        timing=PlaybackTiming(
            tail_seconds=0, cleanup_timeout_seconds=5.0, synth_timeout_seconds=0.1
        ),
    )

    try:
        await engine.speak("hello")
    finally:
        await engine.close()

    assert len(player.played) == 3
    assert [event for event in events if event.kind == "playback_failed"] == []
