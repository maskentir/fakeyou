from __future__ import annotations

import hashlib
import math
import os
import random
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import IdentitySettings
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder, cosine, load_embedder


class StubExtractorConfig:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


class StubExtractor:
    def __init__(self, config: Any) -> None:
        self.config = config


def test_cosine_same_vector_is_one() -> None:
    assert cosine((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)) == pytest.approx(1.0, abs=1e-6)


def test_cosine_orthogonal_vectors_is_zero() -> None:
    assert cosine((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0, abs=1e-6)


def test_load_embedder_rejects_wrong_sha256(tmp_path: Path) -> None:
    model = tmp_path / "tiny.onnx"
    model.write_bytes(b"not a model")
    with pytest.raises(FakeYouError) as excinfo:
        load_embedder(str(model), "0" * 64)
    assert excinfo.value.code is ErrorCode.VOICEPRINT_MODEL_DIGEST_MISMATCH
    assert "sha256" in str(excinfo.value)
    assert str(model) not in str(excinfo.value)


def test_embed_rejects_empty_utterances() -> None:
    embedder = SpeakerEmbedder(object())
    with pytest.raises(ValueError, match="utterances must contain at least one utterance"):
        embedder.embed([])
    with pytest.raises(ValueError, match="utterance must contain at least one sample"):
        embedder.embed([[]])


def test_load_embedder_without_extra_raises_a_catalog_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "tiny.onnx"
    data = b"not a model"
    model.write_bytes(data)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)
    with pytest.raises(FakeYouError) as excinfo:
        load_embedder(str(model), hashlib.sha256(data).hexdigest())
    assert excinfo.value.code is ErrorCode.VOICEPRINT_EXTRA_MISSING
    message = str(excinfo.value)
    assert "voiceprint" in message
    assert "pip install" in message


def test_load_embedder_exposes_stable_model_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "tiny.onnx"
    data = b"not a model"
    model.write_bytes(data)
    stub = types.ModuleType("sherpa_onnx")
    stub_any = cast(Any, stub)
    stub_any.SpeakerEmbeddingExtractorConfig = StubExtractorConfig
    stub_any.SpeakerEmbeddingExtractor = StubExtractor
    monkeypatch.setitem(sys.modules, "sherpa_onnx", stub)
    digest = hashlib.sha256(data).hexdigest()

    first = load_embedder(str(model), digest)
    second = load_embedder(str(model), digest)

    assert first.model_id == f"tiny.onnx:{digest}"
    assert second.model_id == first.model_id


def _load_test_embedder() -> SpeakerEmbedder:
    pytest.importorskip("sherpa_onnx")
    default_model = "/tmp/f2-spike/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
    model_path = os.environ.get("VOICEPRINT_TEST_MODEL", default_model)
    if not os.path.exists(model_path):
        pytest.skip("voiceprint test model not available")
    return load_embedder(model_path, IdentitySettings().voiceprint_model_sha256)


def test_embed_two_synthetic_utterances_returns_normalized_192_vector() -> None:
    embedder = _load_test_embedder()
    first = [0.5 * math.sin(2.0 * math.pi * 440.0 * t / 16000.0) for t in range(16000 * 3)]
    second = [0.5 * math.sin(2.0 * math.pi * 880.0 * t / 16000.0) for t in range(16000 * 3)]
    embedding = embedder.embed([first, second])
    assert len(embedding) == 192
    assert math.sqrt(sum(x * x for x in embedding)) == pytest.approx(1.0, abs=1e-3)


def test_embed_truncated_utterance_stays_close_to_full() -> None:
    embedder = _load_test_embedder()
    rng = random.Random(11)
    full = [rng.uniform(-0.5, 0.5) for _ in range(16000 * 6)]
    half = full[: 16000 * 3]
    assert cosine(embedder.embed([full]), embedder.embed([half])) >= 0.95
