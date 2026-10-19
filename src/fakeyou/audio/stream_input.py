from fakeyou.audio.graph import PcmQueue
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES

EARLY_AUDIO_BYTES = CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES * 5


class StreamAudioInput:
    def __init__(self) -> None:
        self._queue = PcmQueue()
        self._early: list[bytes] = []
        self._early_bytes = 0
        self._started = False
        self._capture: tuple[int, float] | None = None

    @property
    def queue(self) -> PcmQueue:
        return self._queue

    def note_capture(self, seq: int, capture_ms: float) -> None:
        self._capture = (seq, capture_ms)

    @property
    def last_capture(self) -> tuple[int, float] | None:
        return self._capture

    async def start(self) -> None:
        self._started = True
        early, self._early = self._early, []
        self._early_bytes = 0
        for chunk in early:
            self._queue.put(chunk)

    def terminate(self) -> None:
        self._started = False

    async def feed(self, audio: bytes, sample_rate: int = CAPTURE_SAMPLE_RATE) -> None:
        if sample_rate != CAPTURE_SAMPLE_RATE:
            raise ValueError(f"stream audio must be {CAPTURE_SAMPLE_RATE} Hz, got {sample_rate}")
        if not self._started:
            self._early.append(audio)
            self._early_bytes += len(audio)
            while self._early and self._early_bytes > EARLY_AUDIO_BYTES:
                dropped = self._early.pop(0)
                self._early_bytes -= len(dropped)
            return
        self._queue.put(audio)
