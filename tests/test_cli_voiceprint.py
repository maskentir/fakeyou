from __future__ import annotations

import json
import math
import random
import re
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.voiceprint import (
    PROVISIONAL_SNR_FLOOR_DB,
    VOICEPRINT_MIN_RMS,
    EnrollSubject,
    LivenessGate,
    VoiceprintDeps,
    _digit_words,
    _digits_only,
    voiceprint_challenge,
    voiceprint_delete,
    voiceprint_enroll,
    voiceprint_list,
    voiceprint_purge_expired,
    voiceprint_verify,
)
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintRecord, VoiceprintStore
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder
from tests.fakes import InMemoryVoiceprints, RecordingJournal

CONSENT_AT = datetime(2026, 10, 7, 10, 0, 0, tzinfo=UTC)
ENROLLED_AT = datetime(2026, 10, 7, 10, 0, 1, tzinfo=UTC)
LAST_USED_AT = datetime(2026, 10, 7, 10, 0, 2, tzinfo=UTC)


def saved(store: InMemoryVoiceprints, person_id: str) -> VoiceprintRecord:
    [record] = [row for row in store.load_all() if row.person_id == person_id]
    return record


class ScriptedEmbedder(SpeakerEmbedder):
    def __init__(self, vectors: list[tuple[float, ...]]) -> None:
        self.vectors = list(vectors)
        self.calls: list[list[tuple[float, ...]]] = []

    def embed(self, utterances: Any) -> tuple[float, ...]:
        self.calls.append([tuple(utterance) for utterance in utterances])
        return self.vectors.pop(0)


def write_tone_clip(
    path: Path,
    *,
    rate: int = 16000,
    seconds: float = 3.0,
    freq: float = 440.0,
    amplitude: float = 0.5,
) -> Path:
    frames = bytearray()
    total = int(rate * seconds)
    for index in range(total):
        sample = int(amplitude * 32767.0 * math.sin(2.0 * math.pi * freq * index / rate))
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
    return path


def stored_record(
    person_id: str,
    embedding: tuple[float, ...],
    *,
    is_negative: bool = False,
    last_used_at: datetime = LAST_USED_AT,
) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts=CONSENT_AT,
        policy_version="v1",
        is_negative=is_negative,
        enrolled_at=ENROLLED_AT,
        last_used_at=last_used_at,
    )


def test_enroll_three_valid_clips_stores_record_and_journals(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    voiceprint_enroll(
        EnrollSubject("alice", False),
        [str(path) for path in clips],
        settings=settings,
        deps=VoiceprintDeps(store, journal, embedder),
    )

    [utterances] = embedder.calls
    assert len(utterances) == 3
    assert all(len(utterance) == 16000 * 3 for utterance in utterances)
    record = saved(store, "alice")
    assert record.n_utterances == 3
    assert record.policy_version == "v1"
    assert record.dim == 2
    assert record.embedding == (1.0, 0.0)
    assert record.is_negative is False
    assert record.consent_ts.tzinfo is not None
    [row] = journal.events("voiceprint_recorded")
    assert row["person_id"] == "alice"
    assert row["n_utterances"] == 3
    assert row["is_negative"] is False
    assert "embedding" not in row
    assert json.loads(capsys.readouterr().out)["person_id"] == "alice"


def test_enroll_with_negative_flag_marks_record(settings: Settings, tmp_path: Path) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(0.0, 1.0)])

    voiceprint_enroll(
        EnrollSubject("twin-voice", True),
        [str(path) for path in clips],
        settings=settings,
        deps=VoiceprintDeps(store, journal, embedder),
    )

    assert saved(store, "twin-voice").is_negative is True
    [row] = journal.events("voiceprint_recorded")
    assert row["is_negative"] is True


def test_enroll_with_two_clips_exits_2_without_side_effects(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(2)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(FakeYouError) as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, journal, embedder),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_ENROLL_TOO_FEW_CLIPS
    assert caught.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(caught.value) == "enroll needs at least 3 clips, got 2"
    assert store.load_all() == []
    assert journal.events("voiceprint_recorded") == []
    assert embedder.calls == []


def test_enroll_with_silent_clip_exits_2(settings: Settings, tmp_path: Path) -> None:
    assert VOICEPRINT_MIN_RMS == 0.01
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", amplitude=0.0),
    ]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(FakeYouError) as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, journal, embedder),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_CLIP_SILENT
    assert caught.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(caught.value) == "clip is silent: clip-2.wav rms 0.0000 below 0.01"
    assert store.load_all() == []
    assert journal.events("voiceprint_recorded") == []
    assert embedder.calls == []


def test_enroll_with_short_clip_exits_2(settings: Settings, tmp_path: Path) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", seconds=1.0),
    ]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(FakeYouError) as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, journal, embedder),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_CLIP_TOO_SHORT
    assert caught.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(caught.value) == (
        "clip too short: clip-2.wav holds 1.00s of audio, need at least 3.0s"
    )
    assert store.load_all() == []


def test_enroll_with_8khz_clip_exits_2_mentioning_format(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", rate=8000),
    ]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(FakeYouError) as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(store, journal, embedder),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED
    assert caught.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(caught.value) == (
        "unsupported audio format in clip-2.wav: need 16 kHz mono 16-bit WAV"
    )
    assert store.load_all() == []


def test_enroll_with_non_wav_clip_exits_2_mentioning_format(
    settings: Settings, tmp_path: Path
) -> None:
    impostor = tmp_path / "clip-2.wav"
    impostor.write_bytes(b"this is not audio")
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        impostor,
    ]

    with pytest.raises(FakeYouError) as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(
                InMemoryVoiceprints(), RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])
            ),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED
    assert caught.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(caught.value) == (
        "unsupported audio format in clip-2.wav: need 16 kHz mono 16-bit WAV"
    )


def test_enroll_with_truncated_clip_exits_2_mentioning_format(
    settings: Settings, tmp_path: Path
) -> None:
    broken = write_tone_clip(tmp_path / "clip-2.wav")
    raw = broken.read_bytes()
    broken.write_bytes(raw[: len(raw) // 2])
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        broken,
    ]

    with pytest.raises(FakeYouError) as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(
                InMemoryVoiceprints(), RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])
            ),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED
    assert caught.value.exit_code is ExitCode.INPUT_REJECTED
    assert str(caught.value) == (
        "unsupported audio format in clip-2.wav: need 16 kHz mono 16-bit WAV"
    )


def test_verify_high_score_journals_verified_and_touches_print(
    settings: Settings, tmp_path: Path
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    before = datetime.now(UTC)
    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["person_id"] == "alice"
    assert row["outcome"] == "verified"
    assert row["score"] == pytest.approx(1.0, abs=1e-6)
    assert saved(store, "alice").last_used_at >= before


def test_verify_low_score_journals_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(0.0, 1.0)])),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert saved(store, "alice").last_used_at == LAST_USED_AT


def test_verify_close_runners_up_journals_conflict(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("bob", (0.95, 0.3122)))
    journal = RecordingJournal()

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "conflict"
    assert saved(store, "alice").last_used_at == LAST_USED_AT


def test_verify_negative_winner_journals_unknown_without_fault(
    settings: Settings, tmp_path: Path
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("twin-voice", (0.9, 0.4359), is_negative=True))
    journal = RecordingJournal()

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(0.95, 0.3122)])),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert journal.events() == journal.events("voiceprint_decided")
    assert [record.person_id for record in store.load_all()] == ["alice", "twin-voice"]
    assert saved(store, "alice").last_used_at == LAST_USED_AT


def test_verify_named_negative_print_journals_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("twin-voice", (0.9, 0.4359), is_negative=True))
    journal = RecordingJournal()

    voiceprint_verify(
        "twin-voice",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(0.9, 0.4359)])),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert saved(store, "twin-voice").last_used_at == LAST_USED_AT


def test_verify_missing_name_journals_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    voiceprint_verify(
        "ghost",
        str(clip),
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["person_id"] == "ghost"
    assert row["outcome"] == "unknown"


def test_delete_present_print_clears_and_journals(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    voiceprint_delete("alice", settings=settings, store=store, journal=journal)

    assert store.load_all() == []
    [row] = journal.events("voiceprint_cleared")
    assert row["person_id"] == "alice"
    assert json.loads(capsys.readouterr().out)["person_id"] == "alice"


def test_delete_missing_print_raises_without_journal(settings: Settings) -> None:
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match='no voiceprint enrolled for "ghost"') as caught:
        voiceprint_delete("ghost", settings=settings, store=InMemoryVoiceprints(), journal=journal)

    assert caught.value.code is ErrorCode.VOICEPRINT_NOT_ENROLLED
    assert journal.events("voiceprint_cleared") == []


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
    assert rows[0]["enrolled_at"] == ENROLLED_AT.isoformat()
    assert rows[0]["last_used_at"] == LAST_USED_AT.isoformat()
    assert all("embedding" not in row for row in rows)


def test_purge_expired_journals_one_event_per_id(settings: Settings) -> None:
    store = InMemoryVoiceprints()
    store.save(stored_record("alice", (1.0, 0.0), last_used_at=datetime(2026, 1, 1, tzinfo=UTC)))
    store.save(stored_record("bob", (0.0, 1.0), last_used_at=datetime.now(UTC)))
    journal = RecordingJournal()

    voiceprint_purge_expired(settings=settings, store=store, journal=journal)

    assert [row["person_id"] for row in journal.events("voiceprint_expired")] == ["alice"]
    assert [record.person_id for record in store.load_all()] == ["bob"]


def test_list_without_store_and_without_database_url_raises(settings: Settings) -> None:
    settings.database.url = None

    with pytest.raises(FakeYouError) as caught:
        voiceprint_list(settings=settings, store=None)

    assert caught.value.code is ErrorCode.DATABASE_URL_MISSING


def test_list_against_unmigrated_database_raises(
    settings: Settings, blank_database_url: str
) -> None:
    settings.database.url = SecretStr(blank_database_url)

    with pytest.raises(FakeYouError) as caught:
        voiceprint_list(settings=settings, store=None)

    assert caught.value.code is ErrorCode.DATABASE_SCHEMA_EMPTY


def test_list_reads_prints_from_migrated_database(
    migrated_settings: Settings,
    migrated_engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    VoiceprintStore(migrated_engine).save(stored_record("alice", (1.0, 0.0)))

    voiceprint_list(settings=migrated_settings, store=None)

    [row] = json.loads(capsys.readouterr().out)
    assert row["person_id"] == "alice"
    assert datetime.fromisoformat(row["last_used_at"]) == LAST_USED_AT


def test_enroll_with_missing_clip_raises_without_journal(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        tmp_path / "clip-2.wav",
    ]
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match="cannot be read") as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            [str(path) for path in clips],
            settings=settings,
            deps=VoiceprintDeps(InMemoryVoiceprints(), journal, ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_CLIP_UNREADABLE
    assert journal.events("voiceprint_recorded") == []


def test_voiceprint_parsers_map_to_handlers() -> None:
    parser = cli_main.build_parser()

    options = parser.parse_args(
        [
            "voiceprint",
            "enroll",
            "--name",
            "alice",
            "--clip",
            "a.wav",
            "--clip",
            "b.wav",
            "--clip",
            "c.wav",
        ]
    )
    assert options.handler is voiceprint_enroll
    assert options.name == "alice"
    assert options.clip == ["a.wav", "b.wav", "c.wav"]
    assert options.negative is False

    options = parser.parse_args(
        ["voiceprint", "enroll", "--name", "twin-voice", "--clip", "a.wav", "--negative"]
    )
    assert options.clip == ["a.wav"]
    assert options.negative is True

    options = parser.parse_args(["voiceprint", "verify", "--name", "alice", "--clip", "t.wav"])
    assert options.handler is voiceprint_verify
    assert options.clip == "t.wav"

    options = parser.parse_args(["voiceprint", "delete", "--name", "alice"])
    assert options.handler is voiceprint_delete

    options = parser.parse_args(["voiceprint", "list"])
    assert options.handler is voiceprint_list

    options = parser.parse_args(["voiceprint", "purge-expired"])
    assert options.handler is voiceprint_purge_expired


def test_voiceprint_commands_dispatch_without_leaking_parser_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_list(**kwargs: Any) -> None:
        calls.append(kwargs)

    import sys

    monkeypatch.setattr(cli_main, "voiceprint_list", fake_list)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "voiceprint", "list"])
    cli_main.main()

    [call] = calls
    assert "worker_command" not in call
    assert "voiceprint_command" not in call
    assert "command" not in call


def test_voiceprint_enroll_dispatch_bundles_subject_and_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_enroll(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, **kwargs})

    import sys

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
    cli_main.main()

    [call] = calls
    assert call["subject"] == EnrollSubject("alice", True)
    assert call["gate"] == LivenessGate("cid-1", "12345678", "12345678", "Maya", ("s.wav",))
    assert call["clip"] == ["a.wav"]
    for key in ("name", "negative", "challenge_id", "challenge", "transcript", "owner_name"):
        assert key not in call


CHALLENGE_ID = "11111111-2222-3333-4444-555555555555"
CHALLENGE_DIGITS = "12345678"


def write_speechlike_clip(
    path: Path,
    *,
    seconds: float = 10.0,
    freq: float = 440.0,
    amplitude: float = 0.5,
    noise: float = 0.0,
    seed: int = 7,
) -> Path:
    rng = random.Random(seed)
    frames = bytearray()
    total = int(16000 * seconds)
    for index in range(total):
        voiced = (index % 12000) < 8000
        tone = amplitude * math.sin(2.0 * math.pi * freq * index / 16000) if voiced else 0.0
        sample = int(32767.0 * max(-1.0, min(1.0, tone + rng.uniform(-noise, noise))))
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(frames))
    return path


def gated_enroll(
    name: str,
    clips: list[str],
    spontaneous: list[str],
    *,
    settings: Settings,
    deps: VoiceprintDeps,
    challenge: str = CHALLENGE_DIGITS,
    transcript: str = CHALLENGE_DIGITS,
    owner_name: str = "Maya",
    challenge_id: str = CHALLENGE_ID,
) -> None:
    voiceprint_enroll(
        EnrollSubject(name, False),
        clips,
        settings=settings,
        deps=deps,
        gate=LivenessGate(challenge_id, challenge, transcript, owner_name, tuple(spontaneous)),
    )


def test_challenge_issues_unique_eight_digit_codes(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    voiceprint_challenge(settings=settings)
    first = json.loads(capsys.readouterr().out)
    voiceprint_challenge(settings=settings)
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


def test_enroll_matching_challenge_enrolls_with_owner_and_accept_journal(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, embedder),
    )

    record = saved(store, "alice")
    assert record.owner_name == "Maya"
    assert record.n_utterances == 3
    [row] = journal.events("enrollment_accepted")
    assert row["person_id"] == "alice"
    assert row["challenge_id"] == CHALLENGE_ID
    assert row["snr_db"] >= PROVISIONAL_SNR_FLOOR_DB
    assert row["seconds"] >= 30.0
    assert row["segments"] == {"read": 2, "spontaneous": 1}
    assert "transcript" not in row
    assert CHALLENGE_DIGITS not in json.dumps(row)
    assert len(journal.events("voiceprint_recorded")) == 1
    assert journal.events("enrollment_rejected") == []
    body = json.loads(capsys.readouterr().out)
    assert body["person_id"] == "alice"
    assert body["challenge_id"] == CHALLENGE_ID


def test_enroll_transcript_two_edits_away_rejects_without_store(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [
        str(write_speechlike_clip(tmp_path / f"read-{index}.wav", seconds=3.0))
        for index in range(2)
    ]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", seconds=3.0))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            spontaneous,
            settings=settings,
            deps=VoiceprintDeps(store, journal, embedder),
            transcript="12345699",
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "reason" in body
    assert store.load_all() == []
    assert embedder.calls == []
    [row] = journal.events("enrollment_rejected")
    assert row["challenge_id"] == CHALLENGE_ID
    assert journal.events("enrollment_accepted") == []
    assert journal.events("voiceprint_recorded") == []


def test_enroll_short_total_duration_rejects(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [
        str(write_speechlike_clip(tmp_path / f"read-{index}.wav", seconds=3.0))
        for index in range(2)
    ]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", seconds=3.0))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            spontaneous,
            settings=settings,
            deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "30" in body["reason"]
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["seconds"] < 30.0
    assert row["segments"] == {"read": 2, "spontaneous": 1}


def test_enroll_low_snr_rejects(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PROVISIONAL_SNR_FLOOR_DB == 10.0
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", noise=0.45))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            spontaneous,
            settings=settings,
            deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "snr" in body["reason"].lower()
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["snr_db"] < PROVISIONAL_SNR_FLOOR_DB


def test_enroll_without_spontaneous_rejects(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            [],
            settings=settings,
            deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "spontaneous" in body["reason"].lower()
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["segments"] == {"read": 3, "spontaneous": 0}


def test_enroll_single_edit_accepts(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        transcript="12345679",
    )

    assert saved(store, "alice").owner_name == "Maya"
    assert len(journal.events("enrollment_accepted")) == 1
    capsys.readouterr()


def test_enroll_empty_normalized_challenge_rejects_without_store(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            spontaneous,
            settings=settings,
            deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
            challenge="no digits here",
            transcript="nothing spoken",
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "digit" in body["reason"].lower()
    assert store.load_all() == []
    [row] = journal.events("enrollment_rejected")
    assert row["challenge_id"] == CHALLENGE_ID
    assert journal.events("enrollment_accepted") == []


def test_enroll_partial_liveness_args_raises_without_journal(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match="gated enrollment needs") as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            clips,
            settings=settings,
            deps=VoiceprintDeps(InMemoryVoiceprints(), journal, ScriptedEmbedder([(1.0, 0.0)])),
            gate=LivenessGate(challenge_id=CHALLENGE_ID),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE
    assert journal.events() == []


def test_enroll_owner_name_without_challenge_flags_raises(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    with pytest.raises(FakeYouError, match="gated enrollment needs") as caught:
        voiceprint_enroll(
            EnrollSubject("alice", False),
            clips,
            settings=settings,
            deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
            gate=LivenessGate(owner_name="Maya"),
        )

    assert caught.value.code is ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE
    assert store.load_all() == []
    assert journal.events() == []


def test_enroll_legacy_stores_empty_owner_name(settings: Settings, tmp_path: Path) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    store = InMemoryVoiceprints()

    voiceprint_enroll(
        EnrollSubject("alice", False),
        clips,
        settings=settings,
        deps=VoiceprintDeps(store, RecordingJournal(), ScriptedEmbedder([(1.0, 0.0)])),
    )

    assert saved(store, "alice").owner_name == ""


def test_challenge_and_gated_enroll_parsers_map_to_handlers() -> None:
    parser = cli_main.build_parser()

    options = parser.parse_args(["voiceprint", "challenge"])
    assert options.handler is voiceprint_challenge
    assert not hasattr(options, "meeting")

    options = parser.parse_args(
        [
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
        ]
    )
    assert options.handler is voiceprint_enroll
    assert options.challenge_id == CHALLENGE_ID
    assert options.challenge == CHALLENGE_DIGITS
    assert options.transcript == CHALLENGE_DIGITS
    assert options.owner_name == "Maya"
    assert options.spontaneous_clip == ["s.wav"]


ENGLISH_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)


def spoken_word(digit: str) -> str:
    table = _digit_words()
    return next(
        word
        for word, mapped in table.items()
        if mapped == digit and word not in ENGLISH_NUMBER_WORDS
    )


def spoken_phrase(digits: str) -> str:
    return " ".join(spoken_word(digit) for digit in digits)


def test_digits_only_maps_spelled_out_words_to_digits() -> None:
    spoken = spoken_phrase("216")

    assert not any(char.isdigit() for char in spoken)
    assert _digits_only(spoken) == "216"


def test_digits_only_mixes_digits_and_words() -> None:
    assert _digits_only(f"1 {spoken_word('2')}, three!") == "123"


def test_digits_only_drops_unknown_words() -> None:
    assert _digits_only("abc def 7 ghi") == "7"
    assert _digits_only("please call two now") == "2"


def test_digits_only_empty_holds_no_digits() -> None:
    assert _digits_only("") == ""
    assert _digits_only("... !!!") == ""
    assert _digits_only("nothing here") == ""


def test_digit_words_asset_maps_english_digits() -> None:
    table = _digit_words()

    assert len(table) == 20
    for digit, word in enumerate(ENGLISH_NUMBER_WORDS):
        assert table[word] == str(digit)


def test_digit_words_asset_maps_each_digit_in_second_locale() -> None:
    table = _digit_words()

    for digit in "0123456789":
        others = [
            word
            for word, mapped in table.items()
            if mapped == digit and word not in ENGLISH_NUMBER_WORDS
        ]
        assert len(others) == 1
        assert others[0].isascii()
        assert others[0].isalpha()


def test_digit_words_missing_file_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"

    assert _digit_words(path=missing) == {}
    assert _digits_only("two three", path=missing) == ""
    assert _digits_only("42", path=missing) == "42"


def test_digit_words_broken_file_fails_closed(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    listed = tmp_path / "listed.json"
    listed.write_text('["zero"]')

    assert _digit_words(path=broken) == {}
    assert _digit_words(path=listed) == {}


def test_enroll_worded_transcript_accepts(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = InMemoryVoiceprints()
    journal = RecordingJournal()

    gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        deps=VoiceprintDeps(store, journal, ScriptedEmbedder([(1.0, 0.0)])),
        transcript=spoken_phrase(CHALLENGE_DIGITS),
    )

    assert saved(store, "alice").owner_name == "Maya"
    assert len(journal.events("enrollment_accepted")) == 1
    capsys.readouterr()


def test_verify_empty_clip_rejected_as_too_short(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "empty.wav", seconds=0.0)

    with pytest.raises(FakeYouError) as error:
        voiceprint_verify("alice", str(clip), settings=settings)

    assert error.value.code is ErrorCode.VOICEPRINT_CLIP_TOO_SHORT


def test_enroll_empty_clip_rejected_even_with_zero_minimum(
    settings: Settings, tmp_path: Path
) -> None:
    settings.identity.voiceprint_min_seconds = 0.0
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav", seconds=0.0) for index in range(3)]

    with pytest.raises(FakeYouError) as error:
        voiceprint_enroll(
            EnrollSubject("alice", False), [str(path) for path in clips], settings=settings
        )

    assert error.value.code is ErrorCode.VOICEPRINT_CLIP_TOO_SHORT
