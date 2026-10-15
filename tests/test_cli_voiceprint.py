from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine

import fakeyou.cli.main as cli_main
from fakeyou.cli.voiceprint import (
    voiceprint_challenge,
    voiceprint_delete,
    voiceprint_enroll,
    voiceprint_list,
    voiceprint_purge_expired,
    voiceprint_verify,
)
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintStore
from fakeyou.identity.enrollment import EnrollSubject, LivenessGate, VoiceprintDeps
from tests._persistence_fakes import RecordingJournal
from tests._voiceprint_fakes import InMemoryVoiceprints
from tests._voiceprint_support import (
    CHALLENGE_DIGITS,
    CHALLENGE_ID,
    ScriptedEmbedder,
    stored_record,
    write_speechlike_clip,
    write_tone_clip,
)


def test_enroll_prints_accepted_summary(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]

    voiceprint_enroll(
        EnrollSubject("alice", negative=False),
        clips,
        settings=settings,
        deps=VoiceprintDeps(
            InMemoryVoiceprints(), RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])
        ),
    )

    assert json.loads(capsys.readouterr().out) == {
        "person_id": "alice",
        "n_utterances": 3,
        "dim": 2,
        "is_negative": False,
    }


def test_enroll_gated_acceptance_prints_challenge_and_owner(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))
    gate = LivenessGate(CHALLENGE_ID, CHALLENGE_DIGITS, CHALLENGE_DIGITS, "Maya", (spontaneous,))

    voiceprint_enroll(
        EnrollSubject("alice", negative=False),
        clips,
        gate=gate,
        settings=settings,
        deps=VoiceprintDeps(
            InMemoryVoiceprints(), RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])
        ),
    )

    body = json.loads(capsys.readouterr().out)
    assert body["person_id"] == "alice"
    assert body["challenge_id"] == CHALLENGE_ID
    assert body["owner_name"] == "Maya"


def test_enroll_gated_rejection_prints_reason_and_exits_2(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [
        str(write_speechlike_clip(tmp_path / f"read-{index}.wav", seconds=3.0))
        for index in range(2)
    ]
    spontaneous = str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", seconds=3.0))
    gate = LivenessGate(CHALLENGE_ID, CHALLENGE_DIGITS, "12345699", "Maya", (spontaneous,))
    store = InMemoryVoiceprints()

    with pytest.raises(SystemExit) as exited:
        voiceprint_enroll(
            EnrollSubject("alice", negative=False),
            clips,
            gate=gate,
            settings=settings,
            deps=VoiceprintDeps(store, RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert exited.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert body["challenge_id"] == CHALLENGE_ID
    assert body["reason"]
    assert store.load_all() == []


def test_challenge_prints_unique_eight_digit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    voiceprint_challenge()
    first = json.loads(capsys.readouterr().out)
    voiceprint_challenge()
    second = json.loads(capsys.readouterr().out)

    assert set(first) == {"challenge", "challenge_id"}
    assert re.fullmatch(r"[0-9]{8}", first["challenge"]) is not None
    assert re.fullmatch(r"[0-9]{8}", second["challenge"]) is not None
    UUID(first["challenge_id"])
    UUID(second["challenge_id"])
    assert (first["challenge"], first["challenge_id"]) != (
        second["challenge"],
        second["challenge_id"],
    )


def test_verify_prints_the_verdict(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])),
    )

    body = json.loads(capsys.readouterr().out)
    assert set(body) == {"person_id", "outcome", "score", "margin"}
    assert body["person_id"] == "alice"
    assert body["outcome"] == "verified"


def test_delete_prints_removed_flag(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))

    voiceprint_delete("alice", settings=settings, store=store, journal=RecordingJournal())

    assert json.loads(capsys.readouterr().out) == {"person_id": "alice", "removed": True}


def test_list_prints_records_without_embeddings(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("twin-voice", (0.0, 1.0), is_negative=True))

    voiceprint_list(settings=settings, store=store)

    rows = json.loads(capsys.readouterr().out)
    assert [row["person_id"] for row in rows] == ["alice", "twin-voice"]
    assert rows[1]["is_negative"] is True
    assert all("embedding" not in row for row in rows)


def test_list_against_migrated_database_prints_rows(
    migrated_settings: Settings,
    migrated_engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    VoiceprintStore(migrated_engine).save(stored_record("alice", (1.0, 0.0)))

    voiceprint_list(settings=migrated_settings, store=None)

    [row] = json.loads(capsys.readouterr().out)
    assert row["person_id"] == "alice"


def test_purge_expired_prints_removed_ids(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0), last_used_at=datetime(2026, 1, 1, tzinfo=UTC)))

    voiceprint_purge_expired(settings=settings, store=store, journal=RecordingJournal())

    assert json.loads(capsys.readouterr().out) == {"removed": ["alice"]}


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (
            ["voiceprint", "enroll", "--name", "alice", "--clip", "a.wav", "--clip", "b.wav"],
            "voiceprint_enroll",
            {"subject": EnrollSubject("alice", negative=False), "clip": ["a.wav", "b.wav"]},
        ),
        (
            ["voiceprint", "enroll", "--name", "twin-voice", "--clip", "a.wav", "--negative"],
            "voiceprint_enroll",
            {"subject": EnrollSubject("twin-voice", negative=True), "clip": ["a.wav"]},
        ),
        (
            ["voiceprint", "verify", "--name", "alice", "--clip", "t.wav"],
            "voiceprint_verify",
            {"name": "alice", "clip": "t.wav"},
        ),
        (
            ["voiceprint", "delete", "--name", "alice"],
            "voiceprint_delete",
            {"name": "alice"},
        ),
        (["voiceprint", "list"], "voiceprint_list", {}),
        (["voiceprint", "purge-expired"], "voiceprint_purge_expired", {}),
        (["voiceprint", "challenge"], "voiceprint_challenge", {}),
    ],
)
def test_voiceprint_commands_dispatch_to_handlers(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, expected: dict[str, Any]
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_command(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, target, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert {key: call[key] for key in expected} == expected


def test_voiceprint_gated_enroll_dispatch_bundles_liveness_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_enroll(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "voiceprint_enroll", fake_enroll)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "voiceprint",
            "enroll",
            "--name",
            "alice",
            "--clip",
            "a.wav",
            "--spontaneous-clip",
            "s.wav",
            "--challenge-id",
            CHALLENGE_ID,
            "--challenge",
            CHALLENGE_DIGITS,
            "--transcript",
            CHALLENGE_DIGITS,
            "--owner-name",
            "Maya",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["gate"] == LivenessGate(
        CHALLENGE_ID, CHALLENGE_DIGITS, CHALLENGE_DIGITS, "Maya", ("s.wav",)
    )
    assert set(call) == {"subject", "clip", "gate", "settings"}


def test_voiceprint_commands_dispatch_without_leaking_parser_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_list(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "voiceprint_list", fake_list)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "voiceprint", "list"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert set(call) == {"settings"}


def test_voiceprint_enroll_dispatch_bundles_subject_and_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_enroll(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, **kwargs})

    monkeypatch.setattr(cli_main, "voiceprint_enroll", fake_enroll)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "voiceprint",
            "enroll",
            "--name",
            "alice",
            "--clip",
            "a.wav",
            "--negative",
            "--challenge-id",
            "cid-1",
            "--challenge",
            "12345678",
            "--transcript",
            "12345678",
            "--owner-name",
            "Maya",
            "--spontaneous-clip",
            "s.wav",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["subject"] == EnrollSubject("alice", negative=True)
    assert call["gate"] == LivenessGate("cid-1", "12345678", "12345678", "Maya", ("s.wav",))
    assert call["clip"] == ["a.wav"]
    for key in ("name", "negative", "challenge_id", "challenge", "transcript", "owner_name"):
        assert key not in call
