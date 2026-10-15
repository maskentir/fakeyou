from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

import fakeyou.orchestration.assembly as assembly
from fakeyou.audio.audio import TurnAudioTap
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintStore
from fakeyou.identity.verify import CalibrationArtifact
from fakeyou.orchestration.assembly import VoiceprintLive, build_voiceprint_live, open_voiceprint
from tests._error_support import message_pattern
from tests._voiceprint_fakes import FakeEmbedder, InMemoryVoiceprints, make_record


def enable_voiceprint(settings: Settings) -> Settings:
    settings.identity.voiceprint_enabled = True
    return settings


def stub_embedder(monkeypatch: pytest.MonkeyPatch, embedder: FakeEmbedder) -> None:
    monkeypatch.setattr(assembly, "load_voiceprint_embedder", lambda _settings: embedder)


def test_disabled_flag_yields_no_live_bundle(settings: Settings) -> None:
    assert settings.identity.voiceprint_enabled is False

    with open_voiceprint(settings) as live:
        assert live is None


@pytest.mark.parametrize("url", [None, SecretStr("")])
def test_enabled_flag_without_database_url_raises(
    settings: Settings, url: SecretStr | None
) -> None:
    enable_voiceprint(settings)
    settings.database.url = url

    with (
        pytest.raises(
            FakeYouError,
            match=message_pattern(ErrorCode.DATABASE_URL_MISSING),
        ) as caught,
        open_voiceprint(settings),
    ):
        pass

    assert caught.value.code is ErrorCode.DATABASE_URL_MISSING


def test_enabled_flag_with_unmigrated_database_raises(
    settings: Settings, blank_database_url: str
) -> None:
    enable_voiceprint(settings)
    settings.database.url = SecretStr(blank_database_url)

    with (
        pytest.raises(
            FakeYouError, match=message_pattern(ErrorCode.DATABASE_SCHEMA_EMPTY)
        ) as caught,
        open_voiceprint(settings),
    ):
        pass

    assert caught.value.code is ErrorCode.DATABASE_SCHEMA_EMPTY


def test_enabled_flag_without_model_path_raises(migrated_settings: Settings) -> None:
    enable_voiceprint(migrated_settings)

    with (
        pytest.raises(
            FakeYouError,
            match=message_pattern(ErrorCode.VOICEPRINT_MODEL_PATH_MISSING),
        ) as caught,
        open_voiceprint(migrated_settings),
    ):
        pass

    assert caught.value.code is ErrorCode.VOICEPRINT_MODEL_PATH_MISSING


def test_enabled_flag_with_broken_calibration_path_raises(
    migrated_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_voiceprint(migrated_settings)
    stub_embedder(monkeypatch, FakeEmbedder((1.0, 0.0)))
    migrated_settings.identity.voiceprint_calibration_path = str(tmp_path / "missing.json")

    with (
        pytest.raises(
            FakeYouError,
            match=message_pattern(ErrorCode.VOICEPRINT_CALIBRATION_INVALID),
        ) as caught,
        open_voiceprint(migrated_settings),
    ):
        pass

    assert caught.value.code is ErrorCode.VOICEPRINT_CALIBRATION_INVALID


def test_enabled_flag_with_migrated_database_yields_live_bundle(
    migrated_settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_voiceprint(migrated_settings)
    embedder = FakeEmbedder((1.0, 0.0))
    stub_embedder(monkeypatch, embedder)
    VoiceprintStore(migrated_engine).save(make_record("alice", (1.0, 0.0)))

    with open_voiceprint(migrated_settings) as live:
        assert isinstance(live, VoiceprintLive)
        assert isinstance(live.tap, TurnAudioTap)
        assert live.min_seconds == migrated_settings.identity.voiceprint_min_seconds
        assert live.owners == {}
        assert live.embedder is embedder
        assert live.artifact is None
        scores = live.matcher.match((1.0, 0.0))

    assert scores.outcome == "verified"
    assert scores.person_id == "alice"


def test_enabled_flag_loads_calibration_artifact(
    migrated_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_voiceprint(migrated_settings)
    stub_embedder(monkeypatch, FakeEmbedder((1.0, 0.0)))
    artifact_path = tmp_path / "dev-lock.json"
    artifact_path.write_text(
        json.dumps(
            {
                "theta_llr": 1.5,
                "margin_floor": 0.3,
                "snorm_cohort": [0.1, 0.2],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "v1",
                "report_id": "test",
            }
        )
    )
    migrated_settings.identity.voiceprint_calibration_path = str(artifact_path)

    with open_voiceprint(migrated_settings) as live:
        assert live is not None
        assert isinstance(live.artifact, CalibrationArtifact)
        assert live.artifact.theta_llr == 1.5


def test_build_voiceprint_live_serves_stable_taps_per_stream(settings: Settings) -> None:
    live = build_voiceprint_live(
        settings, store=InMemoryVoiceprints(), embedder=FakeEmbedder((1.0, 0.0)), artifact=None
    )

    assert live.tap_for("") is live.tap
    first = live.tap_for("slot-0")
    assert isinstance(first, TurnAudioTap)
    assert live.tap_for("slot-0") is first
    assert live.tap_for("slot-1") is not first


def test_build_voiceprint_live_matches_against_injected_store(settings: Settings) -> None:
    live = build_voiceprint_live(
        settings,
        store=InMemoryVoiceprints([make_record("alice", (1.0, 0.0))]),
        embedder=FakeEmbedder((1.0, 0.0)),
        artifact=None,
    )

    assert isinstance(live, VoiceprintLive)
    assert isinstance(live.tap, TurnAudioTap)
    assert live.min_seconds == settings.identity.voiceprint_min_seconds
    assert live.owners == {}
    assert live.artifact is None
    scores = live.matcher.match((1.0, 0.0))
    assert scores.outcome == "verified"
    assert scores.person_id == "alice"
