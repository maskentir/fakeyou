# Vendored models

- `smart-turn-v3.2-cpu.onnx`: Smart Turn v3.2 (CPU), used by `fakeyou.audio.smart_turn`.
  - Source: HuggingFace `pipecat-ai/smart-turn-v3`, file `smart-turn-v3.2-cpu.onnx`.
  - License: BSD-2-Clause (Daily).
  - sha256: 2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f
  - The log-mel features are extracted by this codebase (no transformers dependency),
    matching the upstream inference composition: keep the last 8 seconds, left-pad
    with zeros to 8 seconds (speech ends at the buffer end), normalize over the full
    buffer, then `WhisperFeatureExtractor(chunk_length=8)` features. The ONNX output
    (named `logits`) is already a sigmoid probability, as in upstream `inference.py`;
    `tests/test_smart_turn.py` pins golden values generated from that reference.
- `silero_vad.onnx`: Silero VAD v5, used by `fakeyou.audio.vad`.
  - Source: the copy shipped in `pipecat-ai` 1.12.0 (`pipecat/audio/vad/data/silero_vad.onnx`), which is the upstream Silero file.
  - License: MIT (snakers4/silero-vad).
  - sha256: 597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004
  - The test `tests/test_vad.py::test_vendored_silero_model_matches_the_recorded_checksum` fails if the file changes.
