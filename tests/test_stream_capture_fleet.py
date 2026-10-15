import asyncio
from typing import Any, cast

import pytest

from fakeyou.audio.stream_capture import (
    StreamChunk,
    StreamRegistry,
)
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptUpdate
from tests._capture_support import echo_stt
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import meet_page


async def test_stream_fleet_tags_transcripts_per_slot() -> None:
    from fakeyou.audio.audio import StreamCallbacks
    from fakeyou.audio.stream_capture import (
        FleetTopology,
        SlotFactories,
        open_stream_fleet,
    )

    class _Playback:
        @property
        def is_speaking(self) -> bool:
            return False

        async def speak(self, text: str, **extra: object) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def close(self) -> None:
            pass

    seen: list[tuple[str, str]] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        seen.append((frame.stream_id or "", frame.text))
        if len(seen) == 2:
            received.set()

    async with open_stream_fleet(
        topology=FleetTopology(SlotBinder(), 2),
        callbacks=StreamCallbacks(on_transcript),
        factories=SlotFactories(
            stt=lambda slot: echo_stt(f"speech-{slot}"),
            playback=lambda _slot: _Playback(),
        ),
    ) as fleet:
        assert set(fleet.capture_registry.sessions) == {0, 1}
        assert fleet.capture_registry.sessions[0].pinned is True
        await fleet.capture_registry.feed(
            StreamChunk(
                slot=0, seq=0, csrcs=(111,), level=0.4, page_ms=1000.0, pcm=b"\x01\x02" * 160
            )
        )
        await fleet.capture_registry.feed(
            StreamChunk(
                slot=1, seq=0, csrcs=(222,), level=0.5, page_ms=1000.0, pcm=b"\x03\x04" * 160
            )
        )
        await asyncio.wait_for(received.wait(), timeout=10)
    assert sorted(seen) == [("slot-0", "speech-0"), ("slot-1", "speech-1")]


class _Playback:
    @property
    def is_speaking(self) -> bool:
        return False

    async def speak(self, text: str, **extra: object) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        pass


async def test_stream_fleet_covers_fourth_slot() -> None:
    from fakeyou.audio.audio import StreamCallbacks
    from fakeyou.audio.stream_capture import (
        STREAM_FLEET_SLOTS,
        FleetTopology,
        SlotFactories,
        open_stream_fleet,
    )

    assert STREAM_FLEET_SLOTS == 4
    seen: list[tuple[str, str]] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        seen.append((frame.stream_id or "", frame.text))
        if len(seen) == 4:
            received.set()

    async with open_stream_fleet(
        topology=FleetTopology(SlotBinder(), STREAM_FLEET_SLOTS),
        callbacks=StreamCallbacks(on_transcript),
        factories=SlotFactories(
            stt=lambda slot: echo_stt(f"speech-{slot}"),
            playback=lambda _slot: _Playback(),
        ),
    ) as fleet:
        assert set(fleet.capture_registry.sessions) == {0, 1, 2, 3}
        for slot in range(4):
            await fleet.capture_registry.feed(
                StreamChunk(
                    slot=slot,
                    seq=0,
                    csrcs=(100 + slot,),
                    level=0.4,
                    page_ms=1000.0,
                    pcm=b"\x01\x02" * 160,
                )
            )
        await asyncio.wait_for(received.wait(), timeout=10)
    assert sorted(seen) == [(f"slot-{slot}", f"speech-{slot}") for slot in range(4)]


async def test_start_capture_drain_uses_fleet_registry() -> None:
    import asyncio
    from types import SimpleNamespace

    from langgraph.checkpoint.memory import InMemorySaver

    from fakeyou.conversation.session import SessionConfig
    from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
    from fakeyou.orchestration.lifecycle import MeetingLifecycle
    from fakeyou.orchestration.runtime import MeetingWorker
    from tests._fakes_common import LINK

    clock = FakeClock()
    page = meet_page([])
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    worker = MeetingWorker(
        SessionConfig(
            owner_id=SessionId("owner-1"),
            twin_id=SessionId("twin-1"),
            meeting_id=SessionId("meeting-1"),
            link=LINK,
            twin_name="Twin",
        ),
        lifecycle,
        page,
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )
    registry = StreamRegistry()
    worker.audio = SimpleNamespace(capture_registry=registry)
    await worker.start_capture_drain(enabled=True)
    assert worker.capture_registry is registry
    await worker.close_capture_drain()
    assert worker.capture_registry is None


async def test_fleet_alone_requires_every_slot_alone() -> None:
    from fakeyou.audio.stream_capture import StreamFleetSession

    class _Slot:
        def __init__(self, verdict: str) -> None:
            self._verdict = verdict

        def raise_if_failed(self) -> None:
            pass

        async def maintenance_tick(self, **_extra: object) -> str:
            return self._verdict

    mixed = StreamFleetSession(cast(Any, [_Slot("alone"), _Slot("hearing")]), StreamRegistry())
    assert await mixed.maintenance_tick(is_busy=False) == "hearing"
    unanimous = StreamFleetSession(cast(Any, [_Slot("alone"), _Slot("alone")]), StreamRegistry())
    assert await unanimous.maintenance_tick(is_busy=False) == "alone"


async def test_fleet_ticks_slots_concurrently() -> None:
    from fakeyou.audio.stream_capture import StreamFleetSession

    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    class _Slot:
        def __init__(self, index: int, verdict: str) -> None:
            self._index = index
            self._verdict = verdict

        def raise_if_failed(self) -> None:
            pass

        async def maintenance_tick(self, **_extra: object) -> str:
            started[self._index].set()
            await release.wait()
            return self._verdict

    fleet = StreamFleetSession(
        cast(Any, [_Slot(0, "hearing"), _Slot(1, "alone")]), StreamRegistry()
    )
    ticking = asyncio.create_task(fleet.maintenance_tick(is_busy=False))
    try:
        await asyncio.wait_for(started[0].wait(), 1.0)
        await asyncio.wait_for(started[1].wait(), 1.0)
        release.set()
        assert await asyncio.wait_for(ticking, 1.0) == "hearing"
    finally:
        release.set()
        await ticking


async def test_fleet_tick_skips_failed_slots() -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import StreamFleetSession

    class _Slot:
        def __init__(self, verdict: str | None) -> None:
            self._verdict = verdict

        def raise_if_failed(self) -> None:
            pass

        async def maintenance_tick(self, **_extra: object) -> str:
            if self._verdict is None:
                raise MeetingAudioError("slot gone")
            return self._verdict

    mixed = StreamFleetSession(cast(Any, [_Slot(None), _Slot("hearing")]), StreamRegistry())
    assert await mixed.maintenance_tick(is_busy=False) == "hearing"


async def test_fleet_speak_raises_when_every_slot_dead() -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import StreamFleetSession

    class _DeadSlot:
        spoken: list[str] = []

        def raise_if_failed(self) -> None:
            raise MeetingAudioError("slot is dead")

        async def speak(self, text: str, **_extra: object) -> None:
            self.spoken.append(text)

    fleet = StreamFleetSession(cast(Any, [_DeadSlot(), _DeadSlot()]), StreamRegistry())
    with pytest.raises(MeetingAudioError):
        await fleet.speak("hello")
    assert _DeadSlot.spoken == []


@pytest.mark.parametrize("method", ["stop", "stop_playback"])
async def test_fleet_stop_aggregates_slot_failures(method: str) -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import StreamFleetSession

    class _FailingSlot:
        def __init__(self, index: int) -> None:
            self._index = index

        async def stop(self) -> None:
            raise MeetingAudioError(f"slot-{self._index} down")

        async def stop_playback(self) -> None:
            raise MeetingAudioError(f"slot-{self._index} down")

    fleet = StreamFleetSession(cast(Any, [_FailingSlot(0), _FailingSlot(1)]), StreamRegistry())
    with pytest.raises(MeetingAudioError, match=f"fleet {method} failed") as excinfo:
        await getattr(fleet, method)()
    assert isinstance(excinfo.value.__cause__, MeetingAudioError)
    assert str(excinfo.value.__cause__) in {"slot-0 down", "slot-1 down"}


@pytest.mark.parametrize("method", ["stop", "stop_playback"])
async def test_fleet_stop_attempts_every_slot_despite_one_failure(method: str) -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import StreamFleetSession

    finished: list[str] = []
    failed = asyncio.Event()
    release = asyncio.Event()

    class _FailingSlot:
        async def stop(self) -> None:
            failed.set()
            raise MeetingAudioError("slot-0 down")

        async def stop_playback(self) -> None:
            failed.set()
            raise MeetingAudioError("slot-0 down")

    class _SlowSlot:
        async def stop(self) -> None:
            await release.wait()
            finished.append("slow")

        async def stop_playback(self) -> None:
            await release.wait()
            finished.append("slow")

    fleet = StreamFleetSession(cast(Any, [_FailingSlot(), _SlowSlot()]), StreamRegistry())
    task = asyncio.create_task(getattr(fleet, method)())
    await failed.wait()
    for _ in range(10):
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(MeetingAudioError):
        await task
    assert finished == ["slow"]


@pytest.mark.parametrize("method", ["stop", "stop_playback"])
async def test_fleet_stop_with_a_single_failure_reraises_the_slot_error(method: str) -> None:
    from fakeyou.audio.playback import PlaybackError
    from fakeyou.audio.stream_capture import StreamFleetSession

    finished: list[str] = []

    class _FailingSlot:
        async def stop(self) -> None:
            raise PlaybackError("speaker busy")

        async def stop_playback(self) -> None:
            raise PlaybackError("speaker busy")

    class _HealthySlot:
        async def stop(self) -> None:
            finished.append("healthy")

        async def stop_playback(self) -> None:
            finished.append("healthy")

    fleet = StreamFleetSession(cast(Any, [_FailingSlot(), _HealthySlot()]), StreamRegistry())
    with pytest.raises(PlaybackError, match="speaker busy"):
        await getattr(fleet, method)()
    assert finished == ["healthy"]


@pytest.mark.parametrize("method", ["stop", "stop_playback"])
async def test_fleet_stop_with_mixed_failures_still_aggregates(method: str) -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.playback import PlaybackError
    from fakeyou.audio.stream_capture import StreamFleetSession

    class _PlaybackSlot:
        async def stop(self) -> None:
            raise PlaybackError("speaker busy")

        async def stop_playback(self) -> None:
            raise PlaybackError("speaker busy")

    class _AudioSlot:
        async def stop(self) -> None:
            raise MeetingAudioError("slot gone")

        async def stop_playback(self) -> None:
            raise MeetingAudioError("slot gone")

    fleet = StreamFleetSession(cast(Any, [_PlaybackSlot(), _AudioSlot()]), StreamRegistry())
    with pytest.raises(MeetingAudioError, match=f"fleet {method} failed"):
        await getattr(fleet, method)()


@pytest.mark.parametrize("method", ["stop", "stop_playback"])
async def test_fleet_stop_completes_slot_cleanup_when_cancelled(method: str) -> None:
    from fakeyou.audio.stream_capture import StreamFleetSession

    cleaned: list[int] = []
    barrier = asyncio.Barrier(3)

    class _HangingSlot:
        def __init__(self, index: int) -> None:
            self._index = index

        async def stop(self) -> None:
            await barrier.wait()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.append(self._index)

        async def stop_playback(self) -> None:
            await barrier.wait()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.append(self._index)

    fleet = StreamFleetSession(cast(Any, [_HangingSlot(0), _HangingSlot(1)]), StreamRegistry())
    task = asyncio.create_task(getattr(fleet, method)())
    await barrier.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cleaned) == [0, 1]


async def test_fleet_calls_propagates_unexpected_error_as_group() -> None:
    from fakeyou.audio.stream_capture import run_fleet_calls

    async def boom() -> None:
        raise ValueError("bug")

    with pytest.raises(BaseExceptionGroup) as caught:
        await run_fleet_calls([boom], "stop")

    (error,) = caught.value.exceptions
    assert isinstance(error, ValueError)


async def test_fleet_calls_collects_os_error() -> None:
    from fakeyou.audio.stream_capture import run_fleet_calls

    async def boom() -> None:
        raise OSError("device gone")

    with pytest.raises(OSError, match="device gone"):
        await run_fleet_calls([boom], "stop")


async def test_tick_slot_returns_meeting_audio_error_as_value() -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import tick_slot

    class _DeadSession:
        async def maintenance_tick(self, **_extra: object) -> str:
            raise MeetingAudioError("slot gone")

    result = await tick_slot(cast(Any, _DeadSession()), is_busy=False, remote_participants=None)

    assert isinstance(result, MeetingAudioError)


async def test_tick_slot_propagates_unexpected_error() -> None:
    from fakeyou.audio.stream_capture import tick_slot

    class _BuggySession:
        async def maintenance_tick(self, **_extra: object) -> str:
            raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        await tick_slot(cast(Any, _BuggySession()), is_busy=False, remote_participants=None)


async def test_tick_slot_returns_os_error_as_result() -> None:
    from fakeyou.audio.stream_capture import tick_slot

    class _BrokenDeviceSession:
        async def maintenance_tick(self, **_extra: object) -> str:
            raise OSError("device gone")

    result = await tick_slot(
        cast(Any, _BrokenDeviceSession()), is_busy=False, remote_participants=None
    )

    assert isinstance(result, OSError)
