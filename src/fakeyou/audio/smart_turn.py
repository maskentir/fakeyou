import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import cache, partial
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime

from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import JournalSink

RATE = CAPTURE_SAMPLE_RATE
N_SAMPLES = 8 * RATE
N_FFT = 400
HOP_LENGTH = 160
N_MELS = 80
MODEL_PATH = Path(__file__).parent / "models" / "smart-turn-v3.2-cpu.onnx"
MODEL_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"


def _hertz_to_mel(freq: np.ndarray) -> np.ndarray:
    mels = 3.0 * freq / 200.0
    log_region = freq >= 1000.0
    mels[log_region] = 15.0 + np.log(freq[log_region] / 1000.0) * (27.0 / np.log(6.4))
    return mels


def _mel_to_hertz(mels: np.ndarray) -> np.ndarray:
    freq = 200.0 * mels / 3.0
    log_region = mels >= 15.0
    freq[log_region] = 1000.0 * np.exp((np.log(6.4) / 27.0) * (mels[log_region] - 15.0))
    return freq


@cache
def _mel_filters() -> np.ndarray:
    num_frequency_bins = 1 + N_FFT // 2
    mel_freqs = np.linspace(
        _hertz_to_mel(np.array([0.0]))[0], _hertz_to_mel(np.array([8000.0]))[0], N_MELS + 2
    )
    filter_freqs = _mel_to_hertz(mel_freqs)
    fft_freqs = np.linspace(0, RATE // 2, num_frequency_bins)
    filter_diff = np.diff(filter_freqs)
    slopes = np.expand_dims(filter_freqs, 0) - np.expand_dims(fft_freqs, 1)
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    filters = np.maximum(np.zeros(1), np.minimum(down_slopes, up_slopes))
    enorm = 2.0 / (filter_freqs[2 : N_MELS + 2] - filter_freqs[:N_MELS])
    return np.asarray(filters * np.expand_dims(enorm, 0), dtype=np.float64)


@cache
def _window() -> np.ndarray:
    return np.hanning(N_FFT + 1)[:-1]


def _checked(audio: np.ndarray) -> np.ndarray:
    waveform = np.asarray(audio)
    if waveform.ndim != 1:
        raise ValueError("audio must be mono 16 kHz samples in one dimension")
    segment = waveform[-N_SAMPLES:].astype(np.float64)
    if not np.isfinite(segment).all():
        raise ValueError("audio must be finite 16 kHz samples")
    return segment


def log_mel_spectrogram(audio: np.ndarray) -> np.ndarray:
    segment = _checked(audio)
    padded = np.zeros(N_SAMPLES, dtype=np.float64)
    if segment.shape[0]:
        padded[-segment.shape[0] :] = segment
    padded = (padded - padded.mean()) / np.sqrt(padded.var() + 1e-7)
    padded = np.pad(padded, [(N_FFT // 2, N_FFT // 2)], mode="reflect")
    num_frames = 1 + (padded.size - N_FFT) // HOP_LENGTH
    strides = (padded.strides[0] * HOP_LENGTH, padded.strides[0])
    frames = np.lib.stride_tricks.as_strided(padded, shape=(num_frames, N_FFT), strides=strides)
    spectrum = np.fft.rfft(frames * _window(), n=N_FFT, axis=1)
    power = (np.abs(spectrum) ** 2).T
    mel = np.maximum(1e-10, _mel_filters().T @ power)
    log_spec = np.asarray(np.log10(mel), dtype=np.float64)[:, :-1]
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    result: np.ndarray = ((log_spec + 4.0) / 4.0).astype(np.float32)
    return result


class SmartTurnModel:
    def __init__(self, path: Path = MODEL_PATH) -> None:
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )

    def predict(self, audio: np.ndarray) -> float:
        features = log_mel_spectrogram(audio)[np.newaxis, :, :]
        output = self._session.run(None, {"input_features": features})
        return float(np.asarray(output[0]).reshape(-1)[0])


class TurnScorer(Protocol):
    def predict(self, audio: np.ndarray) -> float: ...


OnTurnEnd = Callable[[float], Awaitable[None]]


class SmartTurnTap:
    def __init__(
        self, scorer: TurnScorer, on_turn_end: OnTurnEnd, *, journal: JournalSink | None = None
    ) -> None:
        self._scorer = scorer
        self._on_turn_end = on_turn_end
        self._journal = journal
        self._buffer = bytearray()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def feed(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        self._buffer += pcm
        excess = len(self._buffer) - N_SAMPLES * 2
        if excess > 0:
            del self._buffer[:excess]

    def speech_started(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def speech_stopped(self) -> None:
        if self._closed or not self._buffer:
            return
        audio = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._score(audio))

    async def close(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _score(self, audio: np.ndarray) -> None:
        probability, error = await capture_error(
            partial(asyncio.to_thread, self._scorer.predict, audio)
        )
        if error is not None or probability is None or not 0.0 <= probability <= 1.0:
            if self._journal is not None:
                self._journal.record("speech_turn_scoring_failed", source="audio")
            return
        await capture_error(partial(self._deliver, probability))

    async def _deliver(self, probability: float) -> None:
        if self._journal is not None:
            self._journal.record(
                "speech_turn_scored",
                source="audio",
                turn_end_probability=round(probability, 4),
            )
        await self._on_turn_end(probability)
