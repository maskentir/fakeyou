from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import AsyncIterator, Callable, Sequence
from typing import TypedDict, Unpack, override

import httpx2
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.errors import ErrorCategory

from fakeyou.audio.playback import PlaybackEvent


class FakeAudio:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.stopped = False
        self.reconnect_requests: list[str] = []

    @property
    def is_speaking(self) -> bool:
        return False

    def request_stt_reconnect(self, reason: str) -> None:
        self.reconnect_requests.append(reason)

    def reconnect_state(self) -> str:
        return "queued" if self.reconnect_requests else "idle"

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        del sentence_index
        del transcript_id
        self.events.append(f"speak:{text}")

    async def stop_playback(self) -> None:
        self.events.append("playback_stopped")

    async def stop(self) -> None:
        if not self.stopped:
            self.events.append("audio_stopped")
            self.stopped = True

    def raise_if_failed(self) -> None:
        pass

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class STTScript(TypedDict, total=False):
    user_id: str
    finalized: Sequence[bool]
    error: str
    category: ErrorCategory
    exception: Exception
    startup: bool
    signal: bool


class FakeSTT(FrameProcessor):
    def __init__(self, text: str = "heard", **script: Unpack[STTScript]) -> None:
        super().__init__()
        self.text = text
        self.user_id = script.get("user_id", "guest")
        self.finalized = script.get("finalized", (False, True))
        self.error = script.get("error")
        self.category = script.get("category")
        self.exception = script.get("exception")
        self.startup = script.get("startup", False)
        self.levels: list[float] = []
        self.signal = asyncio.Event()
        self.measure_signal = script.get("signal", False)

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        triggered = (
            isinstance(frame, StartFrame) if self.startup else isinstance(frame, InputAudioRawFrame)
        )
        if triggered:
            if self.measure_signal and isinstance(frame, InputAudioRawFrame):
                assert frame.sample_rate == 16_000
                assert frame.num_channels == 1
                samples = struct.unpack(f"<{len(frame.audio) // 2}h", frame.audio)
                level = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
                self.levels.append(level)
                if level > 0.02:
                    self.signal.set()
            if self.error is not None:
                await self.push_frame(
                    ErrorFrame(self.error, category=self.category, exception=self.exception),
                    FrameDirection.UPSTREAM,
                )
            for finalized in self.finalized:
                await self.push_frame(
                    TranscriptionFrame(self.text, self.user_id, "", finalized=finalized), direction
                )
        await self.push_frame(frame, direction)


class PCMStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes], error: Exception | None = None) -> None:
        self.chunks = list(chunks)
        self.error = error

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


class FakeReconnectableSTT(FakeSTT):
    def __init__(self) -> None:
        super().__init__(finalized=())
        self.calls = 0

    async def reconnect_when_idle(self) -> None:
        self.calls += 1


class RawReconnectOnlySTT:
    async def _reconnect(self) -> None:
        pass


class NonReconnectableSTT:
    pass


class FakeReplyAudio(FakeAudio):
    def __init__(self) -> None:
        super().__init__([])
        self.spoken: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.stops = 0
        self.speaking = False
        self.on_playback_event: Callable[[PlaybackEvent], None] | None = None

    @property
    @override
    def is_speaking(self) -> bool:
        return self.speaking

    @override
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        self.spoken.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)
        if self.on_playback_event is not None:
            self.on_playback_event(PlaybackEvent("playback_finished", text))

    @override
    async def stop_playback(self) -> None:
        self.stops += 1
