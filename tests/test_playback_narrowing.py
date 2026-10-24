import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
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
from fakeyou.observability.provider_faults import ProviderFault
from tests._persistence_fakes import RecordingJournal


class _Synth:
    def __init__(self, error: Exception | None = None) -> None:
        self.audio = b"\x00" * 480
        self.error = error
        self.texts: list[str] = []

    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del sentence_index
        del transcript_id
        del language
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        yield self.audio

    async def aclose(self) -> None:
        pass


class _Player:
    async def __call__(self, chunks: AsyncIterator[bytes]) -> None:
        async for _ in chunks:
            pass


class _Muter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def __call__(self, *, muted: bool) -> None:
        del muted
        if self.error is not None:
            raise self.error


class _FailingPlayer:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __call__(self, chunks: AsyncIterator[bytes]) -> None:
        del chunks
        raise self.error


def _engine(
    muter: _Muter,
    events: list[PlaybackEvent],
    reporter: FaultReporter,
    player: _Player | _FailingPlayer | None = None,
) -> PlaybackEngine:
    return PlaybackEngine(
        _Synth(),
        PlaybackBackend(muter, player if player is not None else _Player()),
        on_event=events.append,
        timing=PlaybackTiming(tail_seconds=30),
        diagnostics=Diagnostics(faults=reporter),
    )


async def _await_event(events: list[PlaybackEvent], kind: str) -> PlaybackEvent:
    for _ in range(1000):
        matches = [event for event in events if event.kind == kind]
        if matches:
            return matches[0]
        await asyncio.sleep(0.001)
    pytest.fail(f"no {kind} event")


class _UnmuteFails(_Muter):
    def __init__(self) -> None:
        super().__init__()
        self.failing = True

    @override
    async def __call__(self, *, muted: bool) -> None:
        if not muted and self.failing:
            raise KeyError("boom")


async def test_unmute_unexpected_error_is_recorded_and_playback_resumes(
    journal: RecordingJournal,
) -> None:
    events: list[PlaybackEvent] = []
    muter = _UnmuteFails()
    engine = _engine(muter, events, FaultReporter(journal=journal))
    try:
        await engine.speak("first")
        for _ in range(1000):
            if any(event.kind == "playback_failed" for event in events):
                break
            await asyncio.sleep(0.001)
        (failed,) = [event for event in events if event.kind == "playback_failed"]
        assert "KeyError" in failed.detail
        assert len(journal.events("fault_recorded")) == 1
        muter.failing = False
        finished_before = [event.kind for event in events].count("playback_finished")
        await engine.speak("second")
        assert [event.kind for event in events].count("playback_finished") > finished_before
    finally:
        await engine.close()


async def test_mute_unexpected_error_propagates(journal: RecordingJournal) -> None:
    events: list[PlaybackEvent] = []
    muter = _Muter()
    engine = _engine(muter, events, FaultReporter(journal=journal))
    try:
        await engine.speak("hi")
        muter.error = KeyError("boom")
        with pytest.raises(KeyError):
            await engine.stop()
        muter.error = None
    finally:
        await engine.close()


@pytest.mark.parametrize(
    "error",
    [PlaybackError("pactl failed"), OSError("spawn failed"), TimeoutError("mute timed out")],
    ids=["typed", "os", "timeout"],
)
async def test_mute_operational_errors_still_handled(
    journal: RecordingJournal, error: Exception
) -> None:
    events: list[PlaybackEvent] = []
    engine = _engine(_Muter(error=error), events, FaultReporter(journal=journal))
    try:
        await engine.speak("hi")
    finally:
        await engine.close()
    kinds = [event.kind for event in events]
    assert kinds.count("mute_failed") == 2
    assert "playback_finished" in kinds


async def test_play_one_unexpected_error_propagates(journal: RecordingJournal) -> None:
    events: list[PlaybackEvent] = []
    engine = _engine(
        _Muter(), events, FaultReporter(journal=journal), _FailingPlayer(KeyError("boom"))
    )
    await engine.speak("hi")
    failed = await _await_event(events, "playback_failed")
    with pytest.raises(KeyError, match="boom"):
        await engine.close()
    assert failed.text == ""
    assert failed.detail.startswith("KeyError")
    assert len(journal.events("fault_recorded")) == 1


@pytest.mark.parametrize(
    "error",
    [
        PlaybackError("pipe failed"),
        OSError("pipe broken"),
        ProviderFault("tts", "unknown"),
    ],
    ids=["typed", "os", "provider"],
)
async def test_play_one_operational_errors_handled(
    journal: RecordingJournal, error: Exception
) -> None:
    events: list[PlaybackEvent] = []
    engine = _engine(_Muter(), events, FaultReporter(journal=journal), _FailingPlayer(error))
    try:
        await engine.speak("hi")
    finally:
        await engine.close()
    assert [event.kind for event in events] == ["playback_started", "playback_failed"]
    assert events[1].text == "hi"


async def test_synth_bug_translates_to_provider_fault(journal: RecordingJournal) -> None:
    reporter = FaultReporter(journal=journal)
    synth = _Synth(error=KeyError("bad voice"))
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth,
        PlaybackBackend(_Muter(), _Player()),
        on_event=events.append,
        timing=PlaybackTiming(tail_seconds=30),
        diagnostics=Diagnostics(faults=reporter),
    )
    try:
        await engine.speak("hi")
    finally:
        await engine.close()
    assert [event.kind for event in events if event.kind == "playback_failed"] != []
    assert synth.texts == ["hi"]
    assert reporter.total() >= 1


async def test_synth_provider_fault_passes_through_without_retry(
    journal: RecordingJournal,
) -> None:
    reporter = FaultReporter(journal=journal)
    synth = _Synth(error=ProviderFault("tts", "unknown"))
    events: list[PlaybackEvent] = []
    engine = PlaybackEngine(
        synth,
        PlaybackBackend(_Muter(), _Player()),
        on_event=events.append,
        timing=PlaybackTiming(tail_seconds=30),
        diagnostics=Diagnostics(faults=reporter),
    )
    try:
        await engine.speak("hi")
    finally:
        await engine.close()
    assert [event.kind for event in events if event.kind == "playback_failed"] != []
    assert synth.texts == ["hi"]


async def test_worker_bug_is_recorded_and_playback_resumes(journal: RecordingJournal) -> None:
    events: list[PlaybackEvent] = []
    synth = _Synth()
    muter = _Muter(error=KeyError("boom"))
    engine = PlaybackEngine(
        synth,
        PlaybackBackend(muter, _Player()),
        on_event=events.append,
        timing=PlaybackTiming(tail_seconds=30),
        diagnostics=Diagnostics(faults=FaultReporter(journal=journal)),
    )
    try:
        await engine.speak("first")
        for _ in range(1000):
            if any(event.kind == "playback_failed" for event in events):
                break
            await asyncio.sleep(0.001)
        (failed,) = [event for event in events if event.kind == "playback_failed"]
        assert failed.text == ""
        assert "KeyError" in failed.detail
        assert len(journal.events("fault_recorded")) == 1
        muter.error = None
        await engine.speak("second")
        assert synth.texts == ["second"]
    finally:
        with suppress(asyncio.CancelledError):
            await engine.close()
