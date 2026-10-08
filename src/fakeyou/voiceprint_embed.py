from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


class SpeakerEmbedder:
    model_id: str | None = None

    def __init__(self, extractor: Any, *, model_id: str | None = None) -> None:
        self._extractor = extractor
        self.model_id = model_id

    def embed(self, utterances: Sequence[Sequence[float]]) -> tuple[float, ...]:
        if len(utterances) == 0:
            raise ValueError("utterances must contain at least one utterance")
        for utterance in utterances:
            if len(utterance) == 0:
                raise ValueError("utterance must contain at least one sample")
        vectors = [self._embed_one(utterance) for utterance in utterances]
        dim = len(vectors[0])
        totals = [0.0] * dim
        for vector in vectors:
            for index, value in enumerate(vector):
                totals[index] += value
        count = float(len(vectors))
        mean = [total / count for total in totals]
        norm = math.sqrt(sum(value * value for value in mean))
        return tuple(value / norm for value in mean)

    def _embed_one(self, utterance: Sequence[float]) -> list[float]:
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=list(utterance))
        stream.input_finished()
        return [float(value) for value in self._extractor.compute(stream)]


def load_embedder(model_path: str, expected_sha256: str) -> SpeakerEmbedder:
    digest = hashlib.sha256(Path(model_path).read_bytes()).hexdigest()
    name = Path(model_path).name
    if digest != expected_sha256:
        raise ValueError(
            f"sha256 mismatch for model file {name}: expected {expected_sha256} got {digest}"
        )
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError(
            'voiceprint support needs the voiceprint extra: pip install "fakeyou[voiceprint]"'
        ) from exc
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=model_path, num_threads=1, debug=False, provider="cpu"
    )
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    return SpeakerEmbedder(extractor, model_id=f"{name}:{digest}")
