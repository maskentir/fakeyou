from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from functools import partial
from typing import Any, TypedDict, Unpack, cast, override

import httpx2

from fakeyou.audio.playback import PlaybackEvent
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.faults import ConsumerSink, FaultReporter, guard_consumer
from fakeyou.observability.journal import Journal
from fakeyou.transcription.contract import SpeechSinks, SttConnectionLost, TranscriptionError
from tests._prepared_support import PreparedSpeaker


class FakeAudio(PreparedSpeaker):
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

    @override
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        del sentence_index
        del transcript_id
        del language
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


class STTScript(TypedDict, total=False):
    faults: FaultReporter
    journal: Journal
    finalized: Sequence[bool]
    error: str
    exception: Exception
    startup: bool
    signal: bool


class FakeSTT:
    def __init__(self, text: str = "heard", **script: Unpack[STTScript]) -> None:
        self.text = text
        self.finalized = script.get("finalized", (False, True))
        self.error = script.get("error")
        self.exception = script.get("exception")
        self.startup = script.get("startup", False)
        self.faults = script.get("faults")
        self.journal = script.get("journal")
        self.levels: list[float] = []
        self.signal = asyncio.Event()
        self.measure_signal = script.get("signal", False)
        self.sinks: SpeechSinks | None = None
        self.started = 0
        self.closed = False
        self.received = 0
        self._deliveries: set[asyncio.Task[None]] = set()

    def __call__(self, sinks: SpeechSinks) -> FakeSTT:
        self.sinks = sinks
        return self

    async def start(self) -> None:
        self.started += 1
        if self.startup:
            self._script_transcripts()

    async def close(self) -> None:
        self.closed = True

    def send_audio(self, pcm: bytes) -> None:
        self.received += 1
        if self.measure_signal:
            self._measure(pcm)
        if self.error is not None:
            self._lose()
        self._script_transcripts()

    def _measure(self, pcm: bytes) -> None:
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        level = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
        self.levels.append(level)
        if level > 0.02:
            self.signal.set()

    async def _deliver(
        self, sink: Callable[[TranscriptUpdate], Awaitable[None]], update: TranscriptUpdate
    ) -> None:
        await guard_consumer(
            partial(sink, update),
            sink=ConsumerSink(self.faults, self.journal),
            source="stt",
            detail="transcript_consumer",
        )

    def _lose(self) -> None:
        assert self.sinks is not None
        if self.exception is not None:
            self.sinks.on_lost(cast(TranscriptionError, self.exception))
        else:
            self.sinks.on_lost(TranscriptionError(self.error or "provider failure"))

    def _script_transcripts(self) -> None:
        assert self.sinks is not None
        for finalized in self.finalized:
            sink = self.sinks.on_update if finalized else self.sinks.on_interim
            update = TranscriptUpdate(text=self.text, final=finalized, timestamp="")
            task = asyncio.get_running_loop().create_task(self._deliver(sink, update))
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)


class FakeReconnectableSTT(FakeSTT):
    def __init__(self) -> None:
        super().__init__(finalized=())
        self.calls = 0

    async def reconnect_when_idle(self) -> None:
        self.calls += 1


class ScriptedSTT:
    def __init__(self) -> None:
        self.sinks: SpeechSinks | None = None
        self.socket_dead = False
        self.reconnects = 0
        self.replay: list[TranscriptUpdate] = []

    def __call__(self, sinks: SpeechSinks) -> ScriptedSTT:
        self.sinks = sinks
        return self

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def send_audio(self, pcm: bytes) -> None:
        del pcm

    async def final(self, update: TranscriptUpdate) -> None:
        assert self.sinks is not None
        await self.sinks.on_update(update)

    def drop_socket(self) -> None:
        assert self.sinks is not None
        self.socket_dead = True
        self.sinks.on_lost(SttConnectionLost("socket gone"))

    async def reconnect_when_idle(self) -> None:
        self.reconnects += 1
        self.socket_dead = False
        for update in self.replay:
            await self.final(update)


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
        self.languages: list[str | None] = []
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
        language: str | None = None,
    ) -> None:
        self.spoken.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)
        self.languages.append(language)
        if self.on_playback_event is not None:
            self.on_playback_event(PlaybackEvent("playback_finished", text))

    @override
    async def stop_playback(self) -> None:
        self.stops += 1


class SilentSpeaker(PreparedSpeaker):
    @property
    def is_speaking(self) -> bool:
        return False

    @override
    async def speak(self, utterance: str, **_: Any) -> None:
        del utterance

    async def stop_playback(self) -> None:
        return None


async def wait_until(event: asyncio.Event) -> None:
    await event.wait()
