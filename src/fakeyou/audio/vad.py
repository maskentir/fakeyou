import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

import loudness
import numpy as np
import onnxruntime

RATE = 16000
WINDOW_SAMPLES = 512
WINDOW_BYTES = WINDOW_SAMPLES * 2
CONTEXT_SAMPLES = 64
VOLUME_WINDOW_SECONDS = 0.4
VOLUME_SMOOTHING = 0.2
ACTIVITY_PERIOD_SECONDS = 0.2
AUDIO_IDLE_TIMEOUT_SECONDS = 1.0
DEFAULT_MODEL_RESET_SAMPLES = 5 * RATE
MODEL_PATH = Path(__file__).parent / "models" / "silero_vad.onnx"


class VadState(Enum):
    QUIET = 1
    STARTING = 2
    SPEAKING = 3
    STOPPING = 4


@dataclass(frozen=True, slots=True)
class VadParams:
    confidence: float = 0.7
    start_secs: float = 0.2
    stop_secs: float = 0.2
    min_volume: float = 0.6


class SpeechModel(Protocol):
    def predict(self, window: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class SileroModel:
    def __init__(self, path: Path = MODEL_PATH) -> None:
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._rate = np.array(RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def predict(self, window: np.ndarray) -> float:
        audio = np.concatenate((self._context, window[np.newaxis, :]), axis=1)
        output, state = self._session.run(
            None, {"input": audio, "state": self._state, "sr": self._rate}
        )
        self._state = state
        self._context = audio[..., -CONTEXT_SAMPLES:]
        return float(np.asarray(output).reshape(-1)[0])


class _VolumeMeter:
    def __init__(self) -> None:
        self._window_bytes = math.ceil(VOLUME_WINDOW_SECONDS * RATE) * 2
        self._buffer = bytearray()
        self._smoothed = 0.0

    def measure(self, pcm: bytes) -> float:
        self._buffer += pcm
        excess = len(self._buffer) - self._window_bytes
        if excess > 0:
            del self._buffer[:excess]
        raw = self._loudness() if len(self._buffer) == self._window_bytes else 0.0
        self._smoothed += VOLUME_SMOOTHING * (raw - self._smoothed)
        return self._smoothed

    def _loudness(self) -> float:
        samples = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        level: float = loudness.integrated_loudness(samples, RATE)
        return max(0.0, min(1.0, (level + 110.0) / 100.0))


class _StateMachine:
    def __init__(self, params: VadParams) -> None:
        window_seconds = WINDOW_SAMPLES / RATE
        self._start_windows = round(params.start_secs / window_seconds)
        self._stop_windows = round(params.stop_secs / window_seconds)
        self._params = params
        self.reset()

    def reset(self) -> None:
        self.state = VadState.QUIET
        self._starting = 0
        self._stopping = 0

    def step(self, *, speaking: bool) -> VadState:
        if speaking:
            if self.state is VadState.QUIET:
                self.state = VadState.STARTING
                self._starting = 1
            elif self.state is VadState.STARTING:
                self._starting += 1
            elif self.state is VadState.STOPPING:
                self.state = VadState.SPEAKING
                self._stopping = 0
        else:
            if self.state is VadState.STARTING:
                self.state = VadState.QUIET
                self._starting = 0
            elif self.state is VadState.SPEAKING:
                self.state = VadState.STOPPING
                self._stopping = 1
            elif self.state is VadState.STOPPING:
                self._stopping += 1
        if self.state is VadState.STARTING and self._starting >= self._start_windows:
            self.state = VadState.SPEAKING
            self._starting = 0
        if self.state is VadState.STOPPING and self._stopping >= self._stop_windows:
            self.state = VadState.QUIET
            self._stopping = 0
        return self.state


@dataclass(frozen=True, slots=True)
class VadWindow:
    end_sample: int
    confidence: float
    volume: float
    state: VadState


class VadAnalyzer:
    def __init__(
        self,
        params: VadParams,
        *,
        model: SpeechModel | None = None,
        model_reset_samples: int | None = DEFAULT_MODEL_RESET_SAMPLES,
    ) -> None:
        self._model: SpeechModel = model if model is not None else SileroModel()
        self._reset_every = model_reset_samples
        self._since_reset = 0
        self._pending = bytearray()
        self._position = 0
        self._volume = _VolumeMeter()
        self._machine = _StateMachine(params)
        self._params = params

    @property
    def state(self) -> VadState:
        return self._machine.state

    @property
    def position(self) -> int:
        return self._position

    def feed(self, pcm: bytes) -> list[VadWindow]:
        self._pending += pcm
        windows: list[VadWindow] = []
        while len(self._pending) >= WINDOW_BYTES:
            frame = bytes(self._pending[:WINDOW_BYTES])
            del self._pending[:WINDOW_BYTES]
            windows.append(self._analyse(frame))
        return windows

    def reset_to_quiet(self) -> None:
        self._machine.reset()

    def _analyse(self, frame: bytes) -> VadWindow:
        if self._reset_every is not None and self._since_reset >= self._reset_every:
            self._model.reset()
            self._since_reset = 0
        window = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        confidence = self._model.predict(window)
        volume = self._volume.measure(frame)
        speaking = confidence >= self._params.confidence and volume >= self._params.min_volume
        state = self._machine.step(speaking=speaking)
        self._since_reset += WINDOW_SAMPLES
        self._position += WINDOW_SAMPLES
        return VadWindow(self._position, confidence, volume, state)


class VadEventKind(Enum):
    STARTED = "started"
    STOPPED = "stopped"
    ACTIVITY = "activity"


@dataclass(frozen=True, slots=True)
class VadEvent:
    kind: VadEventKind
    sample: int


class VadController:
    def __init__(
        self,
        analyzer: VadAnalyzer,
        *,
        activity_period_seconds: float = ACTIVITY_PERIOD_SECONDS,
        audio_idle_timeout_seconds: float = AUDIO_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._analyzer = analyzer
        self._activity_period = round(activity_period_seconds * RATE)
        self._idle_timeout = audio_idle_timeout_seconds
        self._clock = clock
        self._last_audio_at = clock()
        self._last_activity: int | None = None
        self._speaking = False

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, pcm: bytes) -> list[VadEvent]:
        self._last_audio_at = self._clock()
        events: list[VadEvent] = []
        for window in self._analyzer.feed(pcm):
            if window.state is VadState.SPEAKING and not self._speaking:
                self._speaking = True
                events.append(VadEvent(VadEventKind.STARTED, window.end_sample))
            elif window.state is VadState.QUIET and self._speaking:
                self._speaking = False
                events.append(VadEvent(VadEventKind.STOPPED, window.end_sample))
        if self._speaking and self._activity_due():
            self._last_activity = self._analyzer.position
            events.append(VadEvent(VadEventKind.ACTIVITY, self._analyzer.position))
        return events

    def check_idle(self) -> list[VadEvent]:
        if not self._speaking or self._idle_timeout <= 0:
            return []
        if self._clock() - self._last_audio_at < self._idle_timeout:
            return []
        self._speaking = False
        self._analyzer.reset_to_quiet()
        return [VadEvent(VadEventKind.STOPPED, self._analyzer.position)]

    def _activity_due(self) -> bool:
        if self._last_activity is None:
            return True
        return self._analyzer.position - self._last_activity >= self._activity_period
