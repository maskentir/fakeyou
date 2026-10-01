import re
from pathlib import Path

from fakeyou.invariants import (
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
