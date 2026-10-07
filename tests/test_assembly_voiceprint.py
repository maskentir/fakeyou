from __future__ import annotations

import struct
from typing import Any

import pytest

from fakeyou.assembly import VoiceprintLive, create_voiceprint_live
from fakeyou.audio import TurnAudioTap
from fakeyou.cli.errors import CliError
from fakeyou.settings import Settings
from tests.test_voiceprint_match import FakeEmbedder, make_record


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self._conn.statements.append((sql, params))
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._conn.results.pop(0)


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.results: list[list[tuple[Any, ...]]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def enable_voiceprint(settings: Settings) -> Settings:
    settings.identity.voiceprint_enabled = True
    return settings


def test_disabled_flag_returns_no_live_bundle(settings: Settings) -> None:
    assert settings.identity.voiceprint_enabled is False

    assert create_voiceprint_live(settings) is None


def test_enabled_flag_without_dsn_raises(settings: Settings) -> None:
    enable_voiceprint(settings)

    with pytest.raises(CliError, match="FAKEYOU_CHECKPOINT__URL"):
        create_voiceprint_live(settings)


def test_enabled_flag_without_model_path_raises(settings: Settings) -> None:
    enable_voiceprint(settings)

    with pytest.raises(CliError, match="VOICEPRINT_MODEL_PATH"):
        create_voiceprint_live(settings, connect=FakeConnection)


def test_enabled_flag_with_seams_returns_working_bundle(settings: Settings) -> None:
    enable_voiceprint(settings)
    conn = FakeConnection()
    embedder = FakeEmbedder((1.0, 0.0))

    live = create_voiceprint_live(settings, connect=lambda: conn, embedder=embedder)

    assert isinstance(live, VoiceprintLive)
    assert isinstance(live.tap, TurnAudioTap)
    assert live.min_seconds == settings.identity.voiceprint_min_seconds
    assert live.owners == {}
    assert any("CREATE TABLE" in sql for sql, _ in conn.statements)
    record = make_record("alice", (1.0, 0.0))
    conn.results.append(
        [
            (
                record.person_id,
                struct.pack("<2f", *record.embedding),
                record.dim,
                record.n_utterances,
                record.consent_ts,
                record.policy_version,
                record.is_negative,
                record.enrolled_at,
                record.last_used_at,
            )
        ]
    )
    scores = live.matcher.match((1.0, 0.0))
    assert scores.outcome == "verified"
    assert scores.person_id == "alice"
    live.close()


def test_live_bundle_serves_stable_taps_per_stream(settings: Settings) -> None:
    enable_voiceprint(settings)

    live = create_voiceprint_live(
        settings, connect=FakeConnection, embedder=FakeEmbedder((1.0, 0.0))
    )

    assert live is not None
    assert live.tap_for("") is live.tap
    first = live.tap_for("slot-0")
    assert isinstance(first, TurnAudioTap)
    assert live.tap_for("slot-0") is first
    assert live.tap_for("slot-1") is not first
    live.close()
