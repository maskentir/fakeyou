import hashlib
from pathlib import Path

import numpy as np
import pytest

from fakeyou.audio.smart_turn import MODEL_PATH, MODEL_SHA256, SmartTurnModel, log_mel_spectrogram

SAMPLES = ((0, 0), (40, 100), (79, 799), (10, 400), (40, 799))


def sine() -> np.ndarray:
    t = np.arange(2 * 16000) / 16000
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


def test_features_match_the_reference_extractor_for_a_sine() -> None:
    features = log_mel_spectrogram(sine())

    assert features.shape == (80, 800)
    assert features.mean() == pytest.approx(-0.308822, abs=1e-4)
    assert features.std() == pytest.approx(0.216492, abs=1e-4)
    assert features.min() == pytest.approx(-0.336024, abs=1e-4)
    assert features.max() == pytest.approx(1.663976, abs=1e-4)
    assert [float(features[i, j]) for i, j in SAMPLES] == pytest.approx(
        [1.209051, -0.336024, -0.336024, -0.336024, -0.336024], abs=1e-4
    )


def test_features_match_the_reference_extractor_for_noise() -> None:
    features = log_mel_spectrogram(noise())

    assert features.mean() == pytest.approx(0.030805, abs=1e-4)
    assert features.std() == pytest.approx(0.842504, abs=1e-4)
    assert [float(features[i, j]) for i, j in SAMPLES] == pytest.approx(
        [1.288462, 1.156801, -0.623318, -0.623318, -0.623318], abs=1e-4
    )


def test_the_model_scores_silence_as_complete_and_stays_a_probability() -> None:
    model = SmartTurnModel()

    silence = model.predict(np.zeros(8 * 16000, dtype=np.float32))
    tone = model.predict(sine())

    assert silence == pytest.approx(0.987037, abs=1e-4)
    assert tone == pytest.approx(0.338590, abs=1e-4)


def test_short_and_long_audio_are_padded_and_truncated_to_eight_seconds() -> None:
    model = SmartTurnModel()

    short = model.predict(np.zeros(16000, dtype=np.float32))
    long = model.predict(np.zeros(20 * 16000, dtype=np.float32))

    assert short == pytest.approx(0.987037, abs=1e-4)
    assert long == pytest.approx(0.987037, abs=1e-4)
