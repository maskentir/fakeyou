import asyncio
import hashlib
import time
from pathlib import Path

import numpy as np
import pytest

from fakeyou.audio.smart_turn import (
    MODEL_PATH,
    MODEL_SHA256,
    SmartTurnModel,
    SmartTurnTap,
    log_mel_spectrogram,
)
from tests._persistence_fakes import RecordingJournal


def sine(seconds: int = 2) -> np.ndarray:
    t = np.arange(seconds * 16000) / 16000
    return (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def noise() -> np.ndarray:
    return (np.random.default_rng(7).standard_normal(3 * 16000) * 0.1).astype(np.float32)


def test_vendored_smart_turn_model_matches_the_recorded_checksum() -> None:
    digest = hashlib.sha256(Path(MODEL_PATH).read_bytes()).hexdigest()

    assert digest == MODEL_SHA256


def test_silence_extracts_to_the_floor_everywhere() -> None:
    features = log_mel_spectrogram(np.zeros(8 * 16000, dtype=np.float32))

    assert features.shape == (80, 800)
    assert np.all(features == pytest.approx(-1.5))


def test_features_match_the_reference_extractor_for_a_short_sine() -> None:
    features = log_mel_spectrogram(sine())

    assert features.shape == (80, 800)
    assert features.mean() == pytest.approx(-0.159605, abs=1e-4)
    assert features.std() == pytest.approx(0.212980, abs=1e-4)
    assert features.min() == pytest.approx(-0.185509, abs=1e-4)
    assert features.max() == pytest.approx(1.814491, abs=1e-4)
    samples = {(0, 599): 0.697043, (10, 782): 1.725025, (13, 599): 0.712286, (79, 600): 0.144081}
    for (row, col), expected in samples.items():
        assert float(features[row, col]) == pytest.approx(expected, abs=1e-4)


def test_features_match_the_reference_extractor_for_a_full_sine() -> None:
    features = log_mel_spectrogram(sine(8))

    assert features.mean() == pytest.approx(-0.240087, abs=1e-4)
    assert features.std() == pytest.approx(0.410778, abs=1e-4)
    assert float(features[11, 415]) == pytest.approx(1.663976, abs=1e-4)
    assert float(features[79, 0]) == pytest.approx(0.144081, abs=1e-4)


def test_features_match_the_reference_extractor_for_noise() -> None:
    features = log_mel_spectrogram(noise())

    assert features.mean() == pytest.approx(0.146999, abs=1e-4)
    assert features.std() == pytest.approx(0.838226, abs=1e-4)
    assert float(features[0, 0]) == pytest.approx(0.372887, abs=1e-4)
    assert float(features[30, 501]) == pytest.approx(1.327437, abs=1e-4)
    assert float(features[79, 799]) == pytest.approx(1.225104, abs=1e-4)


def test_short_audio_is_left_padded_so_speech_ends_at_the_last_frame() -> None:
    features = log_mel_spectrogram(sine())

    assert float(features[0, 0]) == pytest.approx(float(features.min()), abs=1e-6)
    assert float(features[79, 600]) > float(features.min()) + 0.1


def test_non_finite_audio_is_rejected() -> None:
    audio = np.zeros(16000, dtype=np.float32)
    audio[10] = np.nan

    with pytest.raises(ValueError, match="finite"):
        log_mel_spectrogram(audio)
    with pytest.raises(ValueError, match="finite"):
        SmartTurnModel().predict(audio)


def test_multichannel_audio_is_rejected() -> None:
    with pytest.raises(ValueError, match="mono"):
        log_mel_spectrogram(np.zeros((16000, 2), dtype=np.float32))


def test_the_model_scores_silence_as_complete_and_stays_a_probability() -> None:
    model = SmartTurnModel()

    assert model.predict(np.zeros(8 * 16000, dtype=np.float32)) > 0.98
    assert model.predict(sine()) < 0.5
    assert model.predict(sine(8)) > 0.5
    assert 0.0 < model.predict(noise()) < 1.0


def pcm16(audio: np.ndarray) -> bytes:
    return (audio * 32768).astype(np.int16).tobytes()


class Scores:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.delivered = asyncio.Event()

    async def __call__(self, probability: float) -> None:
        self.values.append(probability)
        self.delivered.set()


class SlowScorer:
    def predict(self, _audio: np.ndarray) -> float:
        time.sleep(0.25)
        return 0.9


async def test_the_tap_scores_the_buffer_when_speech_stops() -> None:
    scores = Scores()
    tap = SmartTurnTap(SmartTurnModel(), scores)
    tap.feed(pcm16(np.zeros(8 * 16000, dtype=np.float32)))

    tap.speech_stopped()
    await asyncio.wait_for(scores.delivered.wait(), 5)

    assert scores.values[0] > 0.98
    await tap.close()


class FailingScorer:
    def predict(self, _audio: np.ndarray) -> float:
        raise RuntimeError("model unavailable")


async def test_a_failing_scorer_is_journaled_and_delivers_nothing() -> None:
    journal = RecordingJournal("meeting-9")
    scores = Scores()
    tap = SmartTurnTap(FailingScorer(), scores, journal=journal)
    tap.feed(pcm16(np.zeros(16000, dtype=np.float32)))

    tap.speech_stopped()
    await asyncio.sleep(0.2)

    assert scores.values == []
    assert journal.events("speech_turn_scoring_failed")
    await tap.close()


async def test_close_waits_for_the_pending_score_to_stop() -> None:
    scores = Scores()
    tap = SmartTurnTap(SlowScorer(), scores)
    tap.feed(pcm16(np.zeros(16000, dtype=np.float32)))
    tap.speech_stopped()

    await asyncio.wait_for(tap.close(), 1)

    assert scores.values == []


async def test_a_stop_without_audio_scores_nothing() -> None:
    scores = Scores()
    tap = SmartTurnTap(SmartTurnModel(), scores)

    tap.speech_stopped()
    await asyncio.sleep(0.1)

    assert scores.values == []
    await tap.close()


async def test_a_resumed_voice_cancels_the_pending_score() -> None:
    scores = Scores()
    tap = SmartTurnTap(SlowScorer(), scores)
    tap.feed(pcm16(np.zeros(16000, dtype=np.float32)))

    tap.speech_stopped()
    tap.speech_started()
    await asyncio.sleep(0.4)

    assert scores.values == []
    await tap.close()


async def test_the_tap_keeps_only_the_last_eight_seconds() -> None:
    scores = Scores()
    tap = SmartTurnTap(SmartTurnModel(), scores)
    tap.feed(pcm16(sine(8)))
    tap.feed(pcm16(np.zeros(8 * 16000, dtype=np.float32)))

    tap.speech_stopped()
    await asyncio.wait_for(scores.delivered.wait(), 5)

    assert scores.values[0] > 0.98
    await tap.close()


def test_short_and_long_audio_are_padded_and_truncated_to_eight_seconds() -> None:
    model = SmartTurnModel()

    assert model.predict(np.zeros(16000, dtype=np.float32)) > 0.98
    assert model.predict(np.zeros(20 * 16000, dtype=np.float32)) > 0.98
