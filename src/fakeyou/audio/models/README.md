# Vendored models

- `silero_vad.onnx`: Silero VAD v5, used by `fakeyou.audio.vad`.
  - Source: the copy shipped in `pipecat-ai` 1.12.0 (`pipecat/audio/vad/data/silero_vad.onnx`), which is the upstream Silero file.
  - License: MIT (snakers4/silero-vad).
  - sha256: 597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004
  - The test `tests/test_vad.py::test_vendored_silero_model_matches_the_recorded_checksum` fails if the file changes.
