from __future__ import annotations

import json
import math
import random
import re
import struct
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import fakeyou.cli.main as cli_main
from fakeyou.cli.errors import CliError
from fakeyou.cli.voiceprint import (
    PROVISIONAL_SNR_FLOOR_DB,
    VOICEPRINT_MIN_RMS,
    voiceprint_challenge,
    voiceprint_delete,
    voiceprint_enroll,
    voiceprint_list,
    voiceprint_purge_expired,
    voiceprint_verify,
)
from fakeyou.settings import Settings
from fakeyou.voiceprint_embed import SpeakerEmbedder
from fakeyou.voiceprint_store import VoiceprintRecord, VoiceprintStore
from tests.fakes import RecordingJournal


class FakeStore(VoiceprintStore):
    def __init__(self) -> None:
        self.records: dict[str, VoiceprintRecord] = {}
        self.touched: list[str] = []

    def save(self, record: VoiceprintRecord) -> None:
        self.records[record.person_id] = record

    def load_all(self) -> list[VoiceprintRecord]:
        return [self.records[key] for key in sorted(self.records)]

    def delete(self, person_id: str) -> bool:
        return self.records.pop(person_id, None) is not None

    def purge_expired(self, now: datetime, retention_days: int) -> list[str]:
        cutoff = now - timedelta(days=retention_days)
        removed = [
            person_id
            for person_id, record in self.records.items()
            if datetime.fromisoformat(record.last_used_at) < cutoff
        ]
        for person_id in removed:
            del self.records[person_id]
        return removed

    def touch_used(self, person_id: str, now: datetime) -> None:
        self.touched.append(person_id)
        record = self.records[person_id]
        self.records[person_id] = VoiceprintRecord(
            person_id=record.person_id,
            embedding=record.embedding,
            dim=record.dim,
            n_utterances=record.n_utterances,
            consent_ts=record.consent_ts,
            policy_version=record.policy_version,
            is_negative=record.is_negative,
            enrolled_at=record.enrolled_at,
            last_used_at=now.isoformat(),
            owner_name=record.owner_name,
        )


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
    last_used_at: str = "2026-10-07T10:00:02+00:00",
) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts="2026-10-07T10:00:00+00:00",
        policy_version="v1",
        is_negative=is_negative,
        enrolled_at="2026-10-07T10:00:01+00:00",
        last_used_at=last_used_at,
    )


def test_enroll_three_valid_clips_stores_record_and_journals(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(3)]
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    voiceprint_enroll(
        "alice",
        [str(path) for path in clips],
        False,
        settings=settings,
        store=store,
        journal=journal,
        embedder=embedder,
    )

    [utterances] = embedder.calls
    assert len(utterances) == 3
    assert all(len(utterance) == 16000 * 3 for utterance in utterances)
    record = store.records["alice"]
    assert record.n_utterances == 3
    assert record.policy_version == "v1"
    assert record.dim == 2
    assert record.embedding == (1.0, 0.0)
    assert record.is_negative is False
    assert datetime.fromisoformat(record.consent_ts).tzinfo is not None
    [row] = journal.events("voiceprint_recorded")
    assert row["person_id"] == "alice"
    assert row["n_utterances"] == 3
    assert row["is_negative"] is False
    assert "embedding" not in row
    assert json.loads(capsys.readouterr().out)["person_id"] == "alice"


def test_enroll_with_negative_flag_marks_record(settings: Settings, tmp_path: Path) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(3)]
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(0.0, 1.0)])

    voiceprint_enroll(
        "twin-voice",
        [str(path) for path in clips],
        True,
        settings=settings,
        store=store,
        journal=journal,
        embedder=embedder,
    )

    assert store.records["twin-voice"].is_negative is True
    [row] = journal.events("voiceprint_recorded")
    assert row["is_negative"] is True


def test_enroll_with_two_clips_exits_2_without_side_effects(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [write_tone_clip(tmp_path / f"clip-{index}.wav") for index in range(2)]
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(SystemExit) as caught:
        voiceprint_enroll(
            "alice",
            [str(path) for path in clips],
            False,
            settings=settings,
            store=store,
            journal=journal,
            embedder=embedder,
        )

    assert caught.value.code == 2
    assert "clip" in capsys.readouterr().err.lower()
    assert store.records == {}
    assert journal.events("voiceprint_recorded") == []
    assert embedder.calls == []


def test_enroll_with_silent_clip_exits_2(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert VOICEPRINT_MIN_RMS == 0.01
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", amplitude=0.0),
    ]
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(SystemExit) as caught:
        voiceprint_enroll(
            "alice",
            [str(path) for path in clips],
            False,
            settings=settings,
            store=store,
            journal=journal,
            embedder=embedder,
        )

    assert caught.value.code == 2
    assert "silent" in capsys.readouterr().err.lower()
    assert store.records == {}
    assert journal.events("voiceprint_recorded") == []
    assert embedder.calls == []


def test_enroll_with_short_clip_exits_2(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", seconds=1.0),
    ]
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(SystemExit) as caught:
        voiceprint_enroll(
            "alice",
            [str(path) for path in clips],
            False,
            settings=settings,
            store=store,
            journal=journal,
            embedder=embedder,
        )

    assert caught.value.code == 2
    assert "short" in capsys.readouterr().err.lower()
    assert store.records == {}


def test_enroll_with_8khz_clip_exits_2_mentioning_format(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        write_tone_clip(tmp_path / "clip-2.wav", rate=8000),
    ]
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(SystemExit) as caught:
        voiceprint_enroll(
            "alice",
            [str(path) for path in clips],
            False,
            settings=settings,
            store=store,
            journal=journal,
            embedder=embedder,
        )

    assert caught.value.code == 2
    assert "format" in capsys.readouterr().err.lower()
    assert store.records == {}


def test_enroll_with_non_wav_clip_exits_2_mentioning_format(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    impostor = tmp_path / "clip-2.wav"
    impostor.write_bytes(b"this is not audio")
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        impostor,
    ]

    with pytest.raises(SystemExit) as caught:
        voiceprint_enroll(
            "alice",
            [str(path) for path in clips],
            False,
            settings=settings,
            store=FakeStore(),
            journal=RecordingJournal(),
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
        )

    assert caught.value.code == 2
    assert "format" in capsys.readouterr().err.lower()


def test_enroll_with_truncated_clip_exits_2_mentioning_format(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = write_tone_clip(tmp_path / "clip-2.wav")
    raw = broken.read_bytes()
    broken.write_bytes(raw[: len(raw) // 2])
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        broken,
    ]

    with pytest.raises(SystemExit) as caught:
        voiceprint_enroll(
            "alice",
            [str(path) for path in clips],
            False,
            settings=settings,
            store=FakeStore(),
            journal=RecordingJournal(),
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
        )

    assert caught.value.code == 2
    assert "format" in capsys.readouterr().err.lower()


def test_verify_high_score_journals_verified_and_touches_print(
    settings: Settings, tmp_path: Path
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        store=store,
        journal=journal,
        embedder=ScriptedEmbedder([(1.0, 0.0)]),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["person_id"] == "alice"
    assert row["outcome"] == "verified"
    assert row["score"] == pytest.approx(1.0, abs=1e-6)
    assert store.touched == ["alice"]


def test_verify_low_score_journals_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        store=store,
        journal=journal,
        embedder=ScriptedEmbedder([(0.0, 1.0)]),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert store.touched == []


def test_verify_close_runners_up_journals_conflict(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("bob", (0.95, 0.3122)))
    journal = RecordingJournal()

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        store=store,
        journal=journal,
        embedder=ScriptedEmbedder([(1.0, 0.0)]),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "conflict"
    assert store.touched == []


def test_verify_negative_winner_journals_unknown_without_fault(
    settings: Settings, tmp_path: Path
) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("twin-voice", (0.9, 0.4359), is_negative=True))
    journal = RecordingJournal()

    voiceprint_verify(
        "alice",
        str(clip),
        settings=settings,
        store=store,
        journal=journal,
        embedder=ScriptedEmbedder([(0.95, 0.3122)]),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert journal.events() == journal.events("voiceprint_decided")
    assert sorted(store.records) == ["alice", "twin-voice"]
    assert store.touched == []


def test_verify_named_negative_print_journals_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = FakeStore()
    store.save(stored_record("twin-voice", (0.9, 0.4359), is_negative=True))
    journal = RecordingJournal()

    voiceprint_verify(
        "twin-voice",
        str(clip),
        settings=settings,
        store=store,
        journal=journal,
        embedder=ScriptedEmbedder([(0.9, 0.4359)]),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["outcome"] == "unknown"
    assert store.touched == []


def test_verify_missing_name_journals_unknown(settings: Settings, tmp_path: Path) -> None:
    clip = write_tone_clip(tmp_path / "probe.wav")
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    voiceprint_verify(
        "ghost",
        str(clip),
        settings=settings,
        store=store,
        journal=journal,
        embedder=ScriptedEmbedder([(1.0, 0.0)]),
    )

    [row] = journal.events("voiceprint_decided")
    assert row["person_id"] == "ghost"
    assert row["outcome"] == "unknown"


def test_delete_present_print_clears_and_journals(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0)))
    journal = RecordingJournal()

    voiceprint_delete("alice", settings=settings, store=store, journal=journal)

    assert store.records == {}
    [row] = journal.events("voiceprint_cleared")
    assert row["person_id"] == "alice"
    assert json.loads(capsys.readouterr().out)["person_id"] == "alice"


def test_delete_missing_print_raises_without_journal(settings: Settings) -> None:
    journal = RecordingJournal()

    with pytest.raises(CliError):
        voiceprint_delete("ghost", settings=settings, store=FakeStore(), journal=journal)

    assert journal.events("voiceprint_cleared") == []


def test_list_prints_records_without_embeddings(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0)))
    store.save(stored_record("twin-voice", (0.0, 1.0), is_negative=True))

    voiceprint_list(settings=settings, store=store)

    rows = json.loads(capsys.readouterr().out)
    assert [row["person_id"] for row in rows] == ["alice", "twin-voice"]
    assert rows[1]["is_negative"] is True
    assert all("embedding" not in row for row in rows)


def test_purge_expired_journals_one_event_per_id(settings: Settings) -> None:
    store = FakeStore()
    store.save(stored_record("alice", (1.0, 0.0), last_used_at="2026-01-01T00:00:00+00:00"))
    store.save(stored_record("bob", (0.0, 1.0), last_used_at=datetime.now(UTC).isoformat()))
    journal = RecordingJournal()

    voiceprint_purge_expired(settings=settings, store=store, journal=journal)

    assert [row["person_id"] for row in journal.events("voiceprint_expired")] == ["alice"]
    assert sorted(store.records) == ["bob"]


def test_list_without_store_and_without_dsn_raises(settings: Settings) -> None:
    assert settings.checkpoint.url is None

    with pytest.raises(CliError, match="FAKEYOU_CHECKPOINT__URL"):
        voiceprint_list(settings=settings, store=None)


def test_enroll_with_missing_clip_raises_without_journal(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [
        write_tone_clip(tmp_path / "clip-0.wav"),
        write_tone_clip(tmp_path / "clip-1.wav"),
        tmp_path / "clip-2.wav",
    ]
    journal = RecordingJournal()

    with pytest.raises(CliError, match="cannot be read"):
        voiceprint_enroll(
            "alice",
            [str(path) for path in clips],
            False,
            settings=settings,
            store=FakeStore(),
            journal=journal,
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
        )

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
    store: FakeStore,
    journal: RecordingJournal,
    embedder: ScriptedEmbedder,
    challenge: str = CHALLENGE_DIGITS,
    transcript: str = CHALLENGE_DIGITS,
    owner_name: str = "Maya",
    challenge_id: str = CHALLENGE_ID,
) -> None:
    voiceprint_enroll(
        name,
        clips,
        False,
        settings=settings,
        store=store,
        journal=journal,
        embedder=embedder,
        challenge_id=challenge_id,
        challenge=challenge,
        transcript=transcript,
        owner_name=owner_name,
        spontaneous_clip=spontaneous,
    )


def test_challenge_issues_unique_eight_digit_codes(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    voiceprint_challenge("meeting-1", settings=settings)
    first = json.loads(capsys.readouterr().out)
    voiceprint_challenge("meeting-1", settings=settings)
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
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        store=store,
        journal=journal,
        embedder=embedder,
    )

    record = store.records["alice"]
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
    store = FakeStore()
    journal = RecordingJournal()
    embedder = ScriptedEmbedder([(1.0, 0.0)])

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            spontaneous,
            settings=settings,
            store=store,
            journal=journal,
            embedder=embedder,
            transcript="12345699",
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "reason" in body
    assert store.records == {}
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
    store = FakeStore()
    journal = RecordingJournal()

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            spontaneous,
            settings=settings,
            store=store,
            journal=journal,
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "30" in body["reason"]
    assert store.records == {}
    [row] = journal.events("enrollment_rejected")
    assert row["seconds"] < 30.0
    assert row["segments"] == {"read": 2, "spontaneous": 1}


def test_enroll_low_snr_rejects(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PROVISIONAL_SNR_FLOOR_DB == 10.0
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav", noise=0.45))]
    store = FakeStore()
    journal = RecordingJournal()

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            spontaneous,
            settings=settings,
            store=store,
            journal=journal,
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "snr" in body["reason"].lower()
    assert store.records == {}
    [row] = journal.events("enrollment_rejected")
    assert row["snr_db"] < PROVISIONAL_SNR_FLOOR_DB


def test_enroll_without_spontaneous_rejects(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(3)]
    store = FakeStore()
    journal = RecordingJournal()

    with pytest.raises(SystemExit) as caught:
        gated_enroll(
            "alice",
            clips,
            [],
            settings=settings,
            store=store,
            journal=journal,
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
        )

    assert caught.value.code == 2
    body = json.loads(capsys.readouterr().out)
    assert body["enrolled"] is False
    assert "spontaneous" in body["reason"].lower()
    assert store.records == {}
    [row] = journal.events("enrollment_rejected")
    assert row["segments"] == {"read": 3, "spontaneous": 0}


def test_enroll_single_edit_accepts(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clips = [str(write_speechlike_clip(tmp_path / f"read-{index}.wav")) for index in range(2)]
    spontaneous = [str(write_speechlike_clip(tmp_path / "spontaneous-0.wav"))]
    store = FakeStore()
    journal = RecordingJournal()

    gated_enroll(
        "alice",
        clips,
        spontaneous,
        settings=settings,
        store=store,
        journal=journal,
        embedder=ScriptedEmbedder([(1.0, 0.0)]),
        transcript="12345679",
    )

    assert store.records["alice"].owner_name == "Maya"
    assert len(journal.events("enrollment_accepted")) == 1
    capsys.readouterr()


def test_enroll_partial_liveness_args_raises_without_journal(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    journal = RecordingJournal()

    with pytest.raises(CliError):
        voiceprint_enroll(
            "alice",
            clips,
            False,
            settings=settings,
            store=FakeStore(),
            journal=journal,
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
            challenge_id=CHALLENGE_ID,
        )

    assert journal.events() == []


def test_enroll_owner_name_without_challenge_flags_raises(
    settings: Settings, tmp_path: Path
) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    store = FakeStore()
    journal = RecordingJournal()

    with pytest.raises(CliError):
        voiceprint_enroll(
            "alice",
            clips,
            False,
            settings=settings,
            store=store,
            journal=journal,
            embedder=ScriptedEmbedder([(1.0, 0.0)]),
            owner_name="Maya",
        )

    assert store.records == {}
    assert journal.events() == []


def test_enroll_legacy_stores_empty_owner_name(settings: Settings, tmp_path: Path) -> None:
    clips = [str(write_tone_clip(tmp_path / f"clip-{index}.wav")) for index in range(3)]
    store = FakeStore()

    voiceprint_enroll(
        "alice",
        clips,
        False,
        settings=settings,
        store=store,
        journal=RecordingJournal(),
        embedder=ScriptedEmbedder([(1.0, 0.0)]),
    )

    assert store.records["alice"].owner_name == ""


def test_challenge_and_gated_enroll_parsers_map_to_handlers() -> None:
    parser = cli_main.build_parser()

    options = parser.parse_args(["voiceprint", "challenge", "--meeting", "meeting-1"])
    assert options.handler is voiceprint_challenge
    assert options.meeting == "meeting-1"

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
