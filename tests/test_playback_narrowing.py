import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import override

import pytest

from fakeyou.audio.playback import PlaybackEngine, PlaybackError, PlaybackEvent
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import ProviderFault
from tests._audio_support import FakeWriter
from tests._persistence_fakes import RecordingJournal
from tests._prepared_support import SentenceSynth


class _Synth(SentenceSynth):
    def __init__(self, error: Exception | None = None) -> None:
        self.audio = b"\x00" * 480
        self.error = error
        self.texts: list[str] = []

    @override
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


class _DrainFails(FakeWriter):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(buffer_seconds=30)
        self.drain_error = error

    @override
    async def drain(self) -> None:
        await super().drain()
        if self.drain_error is not None:
            raise self.drain_error


def _engine(
    writer: FakeWriter,
    events: list[PlaybackEvent],
    reporter: FaultReporter,
    synth: _Synth | None = None,
) -> PlaybackEngine:
    return PlaybackEngine(
        synth if synth is not None else _Synth(),
        writer,
        on_event=events.append,
        diagnostics=Diagnostics(faults=reporter),
    )


async def _await_event(events: list[PlaybackEvent], kind: str) -> PlaybackEvent:
    for _ in range(1000):
        matches = [event for event in events if event.kind == kind]
        if matches:
            return matches[0]
        await asyncio.sleep(0.001)
    pytest.fail(f"no {kind} event")


async def test_drain_unexpected_error_propagates(journal: RecordingJournal) -> None:
    events: list[PlaybackEvent] = []
    writer = _DrainFails()
    engine = _engine(writer, events, FaultReporter(journal=journal))
    try:
        await engine.speak("hi")
        writer.drain_error = KeyError("boom")
        with pytest.raises(KeyError):
            await engine.stop()
        writer.drain_error = None
    finally:
        await engine.close()


@pytest.mark.parametrize(
    "error",
    [PlaybackError("drain failed"), OSError("stream gone"), TimeoutError("drain timed out")],
    ids=["typed", "os", "timeout"],
)
async def test_drain_operational_errors_still_handled(
    journal: RecordingJournal, error: Exception
) -> None:
    events: list[PlaybackEvent] = []
    engine = _engine(_DrainFails(error), events, FaultReporter(journal=journal))
    try:
        await engine.speak("hi")
        await engine.stop()
        assert engine.is_speaking is False
    finally:
        await engine.close()
    assert "playback_finished" in [event.kind for event in events]
    assert len(journal.events("fault_recorded")) == 1


async def test_play_one_unexpected_error_propagates(journal: RecordingJournal) -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter(error=KeyError("boom"))
    engine = _engine(writer, events, FaultReporter(journal=journal))
    await engine.speak("hi")
    failed = await _await_event(events, "playback_failed")
    with pytest.raises(KeyError, match="boom"):
        await engine.close()
    assert writer.closed
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
    engine = _engine(FakeWriter(error=error), events, FaultReporter(journal=journal))
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
    engine = _engine(FakeWriter(buffer_seconds=30), events, reporter, synth)
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
    engine = _engine(FakeWriter(buffer_seconds=30), events, reporter, synth)
    try:
        await engine.speak("hi")
    finally:
        await engine.close()
    assert [event.kind for event in events if event.kind == "playback_failed"] != []
    assert synth.texts == ["hi"]


async def test_worker_bug_is_recorded_and_playback_resumes(journal: RecordingJournal) -> None:
    events: list[PlaybackEvent] = []
    synth = _Synth()
    writer = FakeWriter(error=KeyError("boom"), buffer_seconds=30)
    engine = _engine(writer, events, FaultReporter(journal=journal), synth)
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
        writer.error = None
        await engine.speak("second")
        assert synth.texts == ["first", "second"]
        assert writer.written == [synth.audio]
        assert [event.text for event in events if event.kind == "playback_finished"] == ["second"]
    finally:
        with suppress(asyncio.CancelledError):
            await engine.close()
