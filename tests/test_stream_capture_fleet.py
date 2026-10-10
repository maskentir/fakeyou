import asyncio

import pytest

from fakeyou.audio.stream_capture import (
    StreamChunk,
    StreamRegistry,
)
from fakeyou.audio.voice_activity import SlotBinder
from tests.fakes import FakeClock, meet_page


async def test_stream_fleet_tags_transcripts_per_slot() -> None:
    from pipecat.frames.frames import AudioRawFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    from fakeyou.audio.audio import StreamCallbacks
    from fakeyou.audio.stream_capture import (
        FleetTopology,
        SlotFactories,
        open_stream_fleet,
    )

    class _EchoSTT(FrameProcessor):
        def __init__(self, text: str) -> None:
            super().__init__()
            self._text = text

        async def process_frame(self, frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, AudioRawFrame):
                await self.push_frame(
                    TranscriptionFrame(self._text, "guest", "", finalized=True), direction
                )
            await self.push_frame(frame, direction)

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

    async def on_transcript(frame: TranscriptionFrame) -> None:
        seen.append((frame.transport_source or "", frame.text))
        if len(seen) == 2:
            received.set()

    async with open_stream_fleet(
        topology=FleetTopology(SlotBinder(), 2),
        callbacks=StreamCallbacks(on_transcript),
        factories=SlotFactories(
            stt=lambda slot: _EchoSTT(f"speech-{slot}"),
            playback=lambda slot: _Playback(),
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


async def test_stream_fleet_covers_fourth_slot() -> None:
    from pipecat.frames.frames import AudioRawFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    from fakeyou.audio.audio import StreamCallbacks
    from fakeyou.audio.stream_capture import (
        STREAM_FLEET_SLOTS,
        FleetTopology,
        SlotFactories,
        open_stream_fleet,
    )

    class _EchoSTT(FrameProcessor):
        def __init__(self, text: str) -> None:
            super().__init__()
            self._text = text

        async def process_frame(self, frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, AudioRawFrame):
                await self.push_frame(
                    TranscriptionFrame(self._text, "guest", "", finalized=True), direction
                )
            await self.push_frame(frame, direction)

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

    assert STREAM_FLEET_SLOTS == 4
    seen: list[tuple[str, str]] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        seen.append((frame.transport_source or "", frame.text))
        if len(seen) == 4:
            received.set()

    async with open_stream_fleet(
        topology=FleetTopology(SlotBinder(), STREAM_FLEET_SLOTS),
        callbacks=StreamCallbacks(on_transcript),
        factories=SlotFactories(
            stt=lambda slot: _EchoSTT(f"speech-{slot}"),
            playback=lambda slot: _Playback(),
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
    from fakeyou.orchestration.runtime import _MeetingWorker
    from tests.fakes import LINK

    clock = FakeClock()
    page = meet_page([])
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    worker = _MeetingWorker(
        SessionConfig(
            owner_id="owner-1",
            twin_id="twin-1",
            meeting_id="meeting-1",
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

        async def maintenance_tick(self, **extra: object) -> str:
            return self._verdict

    mixed = StreamFleetSession([_Slot("alone"), _Slot("hearing")], StreamRegistry())
    assert await mixed.maintenance_tick(is_busy=False) == "hearing"
    unanimous = StreamFleetSession([_Slot("alone"), _Slot("alone")], StreamRegistry())
    assert await unanimous.maintenance_tick(is_busy=False) == "alone"


async def test_fleet_speak_raises_when_every_slot_dead() -> None:
    from fakeyou.audio.audio import MeetingAudioError
    from fakeyou.audio.stream_capture import StreamFleetSession

    class _DeadSlot:
        spoken: list[str] = []

        def raise_if_failed(self) -> None:
            raise MeetingAudioError("slot is dead")

        async def speak(self, text: str, **extra: object) -> None:
            self.spoken.append(text)

    fleet = StreamFleetSession([_DeadSlot(), _DeadSlot()], StreamRegistry())
    with pytest.raises(MeetingAudioError):
        await fleet.speak("hello")
    assert _DeadSlot.spoken == []
