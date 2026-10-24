import asyncio
import base64
from typing import override

import pytest

from fakeyou.audio.stream_input import StreamAudioInput
from fakeyou.config.identifiers import SessionId
from tests._capture_support import recording_registry
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import meet_page


def _drain_entry(slot: int, pcm_b64: str = "AAP//w==") -> dict[str, object]:
    return {
        "slot": slot,
        "seq": 0,
        "csrcs": [111],
        "level": 0.4,
        "page_ms": 1000.0,
        "pcm_b64": pcm_b64,
    }


async def test_capture_drain_loop_polls_and_feeds_registry() -> None:
    from fakeyou.audio.stream_capture import CAPTURE_DRAIN_JS, CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload, None])
    registry, made = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry)
    assert await loop.poll() is True
    assert await loop.poll() is False
    assert page.evaluate_calls == [CAPTURE_DRAIN_JS, CAPTURE_DRAIN_JS]
    assert set(registry.sessions) == {0}
    assert registry.sessions[0].csrcs == (111,)
    assert made[0].fed == [b"\x00\x03\xff\xff"]


async def test_drain_poll_feeds_good_chunks_despite_a_bad_entry() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {"streams": ["nope", _drain_entry(2)], "roster": [], "dropped": 0}
    page = meet_page([], evaluates=[payload])
    registry, made = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry)

    assert await loop.poll() is True

    assert set(registry.sessions) == {2}
    assert made[0].fed == [b"\x00\x03\xff\xff"]


async def test_drain_poll_survives_a_feed_failure_mid_batch() -> None:

    from fakeyou.audio.stream_capture import (
        CaptureDrainLoop,
        RegistryTiming,
        StreamRegistry,
    )

    class _FlakyInput(StreamAudioInput):
        @override
        async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
            if audio == b"\x01\x02":
                raise RuntimeError("boom")
            await super().feed(audio, sample_rate)

    def _factory() -> StreamAudioInput:
        return _FlakyInput()

    bad_pcm = base64.b64encode(b"\x01\x02").decode("ascii")
    payload = {
        "streams": [_drain_entry(0, bad_pcm), _drain_entry(1)],
        "roster": [],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload])
    registry = StreamRegistry(timing=RegistryTiming(FakeClock().monotonic), input_factory=_factory)
    loop = CaptureDrainLoop(page, registry)

    assert await loop.poll() is True

    assert set(registry.sessions) == {0, 1}


async def test_drain_loop_survives_an_unexpected_poll_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from fakeyou.audio.stream_capture import CaptureDrainLoop, DrainPolling
    from fakeyou.observability.faults import ConsumerSink
    from tests._persistence_fakes import RecordingJournal

    payload = {"streams": [_drain_entry(0)], "roster": [], "dropped": 0}
    page = meet_page([], evaluates=[payload] * 10)
    registry, made = recording_registry(FakeClock().monotonic)
    journal = RecordingJournal()
    loop = CaptureDrainLoop(
        page,
        registry,
        polling=DrainPolling(poll_interval_seconds=0.01),
        sinks=ConsumerSink(journal=journal),
    )
    real_prune = registry.prune
    calls: list[str] = []

    def flaky_prune() -> None:
        calls.append("prune")
        if len(calls) == 1:
            raise RuntimeError("registry hiccup")
        real_prune()

    monkeypatch.setattr(registry, "prune", flaky_prune)
    await loop.start()
    try:
        for _ in range(200):
            if made and made[0].fed:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("drain loop died on an unexpected poll error")
    finally:
        await loop.close()

    failed = journal.events("drain_poll_failed")
    assert [event["stage"] for event in failed] == ["poll"]


async def test_drain_poll_journals_first_and_every_nth_failure_then_escalates() -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import CaptureDrainLoop, DrainPolling
    from fakeyou.observability.faults import ConsumerSink, FaultReporter
    from tests._persistence_fakes import RecordingJournal

    payload = {"streams": ["nope"], "roster": [], "dropped": 0}
    page = meet_page([], evaluates=[payload] * 4)
    registry, _made = recording_registry(FakeClock().monotonic)
    journal = RecordingJournal()
    loop = CaptureDrainLoop(
        page,
        registry,
        polling=DrainPolling(
            poll_interval_seconds=0.25,
            heartbeat_polls=40,
            max_consecutive_failures=3,
            failure_journal_every=2,
        ),
        sinks=ConsumerSink(journal=journal, faults=FaultReporter(journal=journal)),
    )

    assert await loop.poll() is True
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert await loop.poll() is False

    failed = journal.events("drain_poll_failed")
    assert [event["consecutive"] for event in failed] == [1, 2]
    assert failed[0]["error_type"] == "MeetingAudioError"
    assert failed[0]["stage"] == "parse"
    assert failed[0]["skipped"] == 1
    [exhausted] = journal.events("drain_poll_exhausted")
    assert exhausted["consecutive"] == 3
    assert journal.events("fault_recorded") != []
    with pytest.raises(MeetingAudioError):
        loop.raise_if_failed()


async def test_drain_poll_bounds_a_hung_capture_evaluate() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop, DrainPolling
    from fakeyou.observability.faults import ConsumerSink
    from tests._meet_browser_fakes import HangingPage
    from tests._persistence_fakes import RecordingJournal

    page = HangingPage()
    registry, _made = recording_registry(FakeClock().monotonic)
    journal = RecordingJournal()
    loop = CaptureDrainLoop(
        page,
        registry,
        polling=DrainPolling(poll_interval_seconds=0.01),
        sinks=ConsumerSink(journal=journal),
    )

    assert await loop.poll() is False

    assert page.cancelled is True
    [failed] = journal.events("drain_poll_failed")
    assert failed["error_type"] == "TimeoutError"
    assert failed["stage"] == "evaluate"


async def test_start_capture_drain_respects_enabled_flag() -> None:
    import asyncio

    from fakeyou.conversation.session import SessionConfig
    from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
    from fakeyou.orchestration.runtime import MeetingWorker
    from tests._fakes_common import LINK

    clock = FakeClock()
    page = meet_page([])
    worker = MeetingWorker(
        SessionConfig(
            owner_id=SessionId("owner-1"),
            twin_id=SessionId("twin-1"),
            meeting_id=SessionId("meeting-1"),
            link=LINK,
            twin_name="Twin",
        ),
        page,
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )
    await worker.start_capture_drain(enabled=False)
    assert worker.capture_drain is None
    await worker.start_capture_drain(enabled=True)
    assert worker.capture_drain is not None
    await worker.close_capture_drain()
    assert worker.capture_drain is None


async def test_drain_poll_propagates_unexpected_evaluate_error() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    page = meet_page([], evaluates=[ValueError("bug")])
    registry, _made = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry)

    with pytest.raises(ValueError, match="bug"):
        await loop.poll()
