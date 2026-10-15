import asyncio
import math
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from fakeyou.audio.deaf import CaptureActivity, audio_duration_ms, rms_level
from fakeyou.audio.vad import VadController, VadEvent, VadEventKind
from fakeyou.audio.voice_activity import VoiceActivityTracker
from fakeyou.foundation.attribution_probe import AttributionProbe
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE, PCM_CHANNELS
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES
from fakeyou.identity.voiceprint_match import CLIP_GRID_MS, CLIP_PAD_MS, cut_turn_clip
from fakeyou.observability.faults import ConsumerSink, guard_consumer
from fakeyou.observability.journal import Journal

TURN_TAP_MAX_SECONDS = 30.0
IDLE_CHECK_SECONDS = 0.2


class OnVad(Protocol):
    async def __call__(self, *, active: bool) -> None: ...


class SpeechToText(Protocol):
    def send_audio(self, pcm: bytes) -> None: ...


class PcmQueue:
    def __init__(self) -> None:
        self._items: asyncio.Queue[bytes] = asyncio.Queue()

    def put(self, pcm: bytes) -> None:
        if pcm:
            self._items.put_nowait(pcm)

    async def get(self, wait_seconds: float) -> bytes | None:
        try:
            return await asyncio.wait_for(self._items.get(), wait_seconds)
        except TimeoutError:
            return None


class TurnAudioTap:
    def __init__(self) -> None:
        self._speaking = False
        self._clock_ms = 0.0
        self._buffer = bytearray()
        self._segments: list[list[float]] = []
        self._limit_bytes = max(
            int(CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES * TURN_TAP_MAX_SECONDS), 0
        )

    def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._clock_ms += len(pcm) / (CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES / 1000.0)
        self._buffer += pcm
        excess = len(self._buffer) - self._limit_bytes
        if excess > 0:
            del self._buffer[:excess]

    def speech_started(self) -> None:
        self._speaking = True
        if self._segments and len(self._segments[-1]) == 1:
            self._segments[-1][0] = self._clock_ms
        else:
            self._segments.append([self._clock_ms])

    def speech_stopped(self) -> None:
        if self._speaking:
            self._speaking = False
            self._segments[-1].append(self._clock_ms)

    def take_clip(self) -> bytes:
        closed = [segment for segment in self._segments if len(segment) == 2]
        if not closed:
            return b""
        base = self._buffer_start_ms()
        start_ms = min(segment[0] for segment in closed) - base
        stop_ms = max(segment[1] for segment in closed) - base
        clip = cut_turn_clip(bytes(self._buffer), start_ms, stop_ms)
        keep = [segment for segment in self._segments if len(segment) == 1]
        if keep:
            edge_ms = max(keep[0][0] - CLIP_PAD_MS - CLIP_GRID_MS, 0.0)
            aligned_ms = math.floor(edge_ms / CLIP_GRID_MS) * CLIP_GRID_MS
            drop_ms = max(aligned_ms - base, 0.0)
            drop = min(
                round(drop_ms * CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES / 1000.0)
                // PCM16_SAMPLE_BYTES
                * PCM16_SAMPLE_BYTES,
                len(self._buffer),
            )
            del self._buffer[:drop]
        else:
            self._buffer.clear()
        self._segments = keep
        return clip

    def _buffer_start_ms(self) -> float:
        return self._clock_ms - len(self._buffer) / (
            CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES / 1000.0
        )


@dataclass(frozen=True, slots=True)
class GraphParts:
    activity: CaptureActivity
    vad: VadController
    stt: SpeechToText
    turn_tap: TurnAudioTap | None = None
    probe: AttributionProbe | None = None
    voice_activity: VoiceActivityTracker | None = None
    on_vad: OnVad | None = None
    journal: Journal | None = None


class AudioGraph:
    def __init__(self, parts: GraphParts) -> None:
        self._activity = parts.activity
        self._vad = parts.vad
        self._stt = parts.stt
        self._turn_tap = parts.turn_tap
        self._probe = parts.probe
        self._voice_activity = parts.voice_activity
        self._on_vad = parts.on_vad
        self._journal = parts.journal
        self._vad_probe_failed = False

    async def run(self, source: PcmQueue) -> None:
        while True:
            pcm = await source.get(IDLE_CHECK_SECONDS)
            if pcm is None:
                await self._dispatch(self._vad.check_idle())
            else:
                await self.ingest(pcm)

    async def ingest(self, pcm: bytes) -> None:
        samples = len(pcm) // PCM16_SAMPLE_BYTES // PCM_CHANNELS
        self._activity.observe_audio(
            energy=rms_level(pcm, channels=PCM_CHANNELS),
            duration_ms=audio_duration_ms(len(pcm), CAPTURE_SAMPLE_RATE, PCM_CHANNELS),
        )
        if self._probe is not None:
            self._probe.observe_pcm(samples)
        if self._turn_tap is not None:
            self._turn_tap.feed(pcm)
        self._stt.send_audio(pcm)
        await self._dispatch(self._vad.feed(pcm))

    async def _dispatch(self, events: list[VadEvent]) -> None:
        for event in events:
            await self._handle(event)

    async def _handle(self, event: VadEvent) -> None:
        if event.kind is VadEventKind.ACTIVITY:
            self._activity.observe_speech(active=True)
            return
        active = event.kind is VadEventKind.STARTED
        self._activity.observe_speech(active=active)
        self._record("speech_started" if active else "speech_stopped")
        if self._turn_tap is not None:
            if active:
                self._turn_tap.speech_started()
            else:
                self._turn_tap.speech_stopped()
        self._observe_vad(active=active)
        if self._on_vad is not None:
            await guard_consumer(
                partial(self._on_vad, active=active),
                sink=ConsumerSink(journal=self._journal),
                source="audio",
                detail="vad_consumer",
            )

    def _record(self, event: str) -> None:
        if self._journal is not None:
            self._journal.record(event, source="audio")

    def _observe_vad(self, *, active: bool) -> None:
        if self._voice_activity is not None:
            self._voice_activity.observe_vad(active=active)
        if self._probe is None:
            return
        try:
            self._probe.observe_vad(active=active)
        except OSError as error:
            if not self._vad_probe_failed:
                self._vad_probe_failed = True
                if self._journal is not None:
                    self._journal.record(
                        "vad_observe_failed",
                        source="audio",
                        error_type=type(error).__name__,
                    )
