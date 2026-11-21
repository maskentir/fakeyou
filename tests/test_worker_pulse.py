import hashlib
import re
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import tiktoken

from fakeyou.config.settings import EmbedderSettings
from fakeyou.foundation.invariants import (
    AGENT_MIC_SOURCE_NAME,
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    MEETING_SINK_NAME,
    PLAYBACK_SAMPLE_RATE,
)

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / "docker/audio" / name).read_text()


def test_null_sink_names_and_rates_match_audio_module() -> None:
    sinks = {
        match.group("name"): int(match.group("rate"))
        for match in re.finditer(
            r"module-null-sink sink_name=(?P<name>\S+) rate=(?P<rate>\d+)",
            _read("default.pa"),
        )
    }
    assert sinks[MEETING_SINK_NAME] == CAPTURE_SAMPLE_RATE
    assert sinks[AGENT_SINK_NAME] == PLAYBACK_SAMPLE_RATE


def test_remap_source_and_defaults_match_audio_module() -> None:
    config = _read("default.pa")
    assert f"source_name={AGENT_MIC_SOURCE_NAME} master={AGENT_SINK_NAME}.monitor" in config
    assert f"set-default-sink {MEETING_SINK_NAME}" in config
    assert f"set-default-source {AGENT_MIC_SOURCE_NAME}" in config


def test_daemon_pins_resampler_and_sample_format() -> None:
    config = _read("daemon.conf")
    assert "resample-method = speex-float-5" in config
    assert "default-sample-format = s16le" in config
    assert "default-sample-channels = 1" in config
    assert f"default-sample-rate = {CAPTURE_SAMPLE_RATE}" in config
    assert f"alternate-sample-rate = {PLAYBACK_SAMPLE_RATE}" in config


def test_dockerfile_installs_pulse_configs() -> None:
    dockerfile = _read("Dockerfile")
    assert "COPY docker/audio/default.pa /etc/fakeyou/default.pa" in dockerfile
    assert "COPY docker/audio/daemon.conf /etc/pulse/daemon.conf" in dockerfile


def _cl100k_source() -> tuple[str, str]:
    asked: dict[str, str] = {}

    def capture(blobpath: str, expected_hash: str) -> dict[bytes, int]:
        asked.update(blobpath=blobpath, expected_hash=expected_hash)
        return {}

    with patch("tiktoken_ext.openai_public.load_tiktoken_bpe", capture):
        import_module("tiktoken_ext.openai_public").cl100k_base()
    return asked["blobpath"], asked["expected_hash"]


def test_the_image_bakes_the_encoding_the_embedder_counts_tokens_with() -> None:
    dockerfile = _read("Dockerfile")
    runtime = dockerfile[dockerfile.index("FROM base AS runtime") :]
    blobpath, expected_hash = _cl100k_source()
    cached = hashlib.sha1(blobpath.encode()).hexdigest()
    encoding = tiktoken.encoding_name_for_model(EmbedderSettings().model)

    assert encoding == "cl100k_base"
    assert f"{expected_hash}  /workspace/tiktoken/{cached}" in dockerfile
    assert "COPY --from=builder /workspace/tiktoken /opt/tiktoken" in runtime
    assert "TIKTOKEN_CACHE_DIR=/opt/tiktoken" in runtime


def test_every_image_stage_keeps_the_browser_it_was_built_with() -> None:
    dockerfile = _read("Dockerfile")
    runtime = dockerfile.index("FROM base AS runtime")
    test = dockerfile.index("FROM runtime AS test")
    assert "CLOAKBROWSER_AUTO_UPDATE=false" in dockerfile[runtime:test]
