import json
import struct
import sys
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import psycopg

from fakeyou.checkpoint import checkpoint_dsn
from fakeyou.cli.errors import CliError
from fakeyou.deaf import rms_level
from fakeyou.journal import Journal, journal_path
from fakeyou.settings import Settings
from fakeyou.voiceprint_embed import SpeakerEmbedder, cosine, load_embedder
from fakeyou.voiceprint_store import (
    VoiceprintRecord,
    VoiceprintStore,
    ensure_voiceprint_schema,
)

VOICEPRINT_MIN_RMS = 0.01
VOICEPRINT_SAMPLE_RATE = 16000
VOICEPRINT_JOURNAL_ID = "voiceprint"
MIN_ENROLL_CLIPS = 3


def _reject(message: str) -> NoReturn:
    print(f"fakeyou: error: {message}", file=sys.stderr)
    raise SystemExit(2)


@contextmanager
def _open_store(settings: Settings, store: VoiceprintStore | None) -> Iterator[VoiceprintStore]:
    if store is not None:
        yield store
        return
    dsn = checkpoint_dsn(settings)
    if dsn is None:
        raise CliError("Set FAKEYOU_CHECKPOINT__URL in .env before using this command.")
    conn = psycopg.connect(dsn)
    try:
        ensure_voiceprint_schema(conn)
        yield VoiceprintStore(lambda: conn)
    finally:
        conn.close()


@contextmanager
def _open_journal(settings: Settings, journal: Journal | None) -> Iterator[Journal]:
    if journal is not None:
        yield journal
        return
    owned = Journal(
        VOICEPRINT_JOURNAL_ID,
        path=journal_path(settings.paths.journal_dir, VOICEPRINT_JOURNAL_ID),
    )
    try:
        yield owned
    finally:
        owned.close()


def _embedder_for(settings: Settings, embedder: SpeakerEmbedder | None) -> SpeakerEmbedder:
    if embedder is not None:
        return embedder
    return load_embedder(
        settings.identity.voiceprint_model_path, settings.identity.voiceprint_model_sha256
    )


def _read_clip_frames(path: str) -> tuple[str, bytes, int]:
    label = Path(path).name
    try:
        handle = wave.open(path, "rb")
    except FileNotFoundError:
        raise CliError(f"clip cannot be read: {label}") from None
    except (wave.Error, EOFError, OSError):
        _reject(f"unsupported audio format in {label}: need 16 kHz mono 16-bit WAV")
    with handle:
        if (
            handle.getnchannels() != 1
            or handle.getsampwidth() != 2
            or handle.getframerate() != VOICEPRINT_SAMPLE_RATE
        ):
            _reject(f"unsupported audio format in {label}: need 16 kHz mono 16-bit WAV")
        count = handle.getnframes()
        frames = handle.readframes(count)
    return label, frames, count


def _to_utterance(frames: bytes, count: int) -> list[float]:
    samples = struct.unpack(f"<{count}h", frames[: count * 2])
    return [sample / 32768.0 for sample in samples]


def voiceprint_enroll(
    name: str,
    clip: list[str],
    negative: bool,
    *,
    settings: Settings,
    store: VoiceprintStore | None = None,
    journal: Journal | None = None,
    embedder: SpeakerEmbedder | None = None,
) -> None:
    if len(clip) < MIN_ENROLL_CLIPS:
        _reject(f"enroll needs at least {MIN_ENROLL_CLIPS} clips, got {len(clip)}")
    min_seconds = settings.identity.voiceprint_min_seconds
    utterances: list[list[float]] = []
    for path in clip:
        label, frames, count = _read_clip_frames(path)
        seconds = count / VOICEPRINT_SAMPLE_RATE
        if seconds < min_seconds:
            _reject(
                f"clip too short: {label} holds {seconds:.2f}s of audio,"
                f" need at least {min_seconds:.1f}s"
            )
        level = rms_level(frames, channels=1)
        if level < VOICEPRINT_MIN_RMS:
            _reject(f"clip is silent: {label} rms {level:.4f} below {VOICEPRINT_MIN_RMS}")
        utterances.append(_to_utterance(frames, count))
    with (
        _open_store(settings, store) as owned_store,
        _open_journal(settings, journal) as owned_journal,
    ):
        embedding = _embedder_for(settings, embedder).embed(utterances)
        stamp = datetime.now(UTC).isoformat()
        owned_store.save(
            VoiceprintRecord(
                person_id=name,
                embedding=tuple(embedding),
                dim=len(embedding),
                n_utterances=len(utterances),
                consent_ts=stamp,
                policy_version=settings.identity.voiceprint_policy_version,
                is_negative=negative,
                enrolled_at=stamp,
                last_used_at=stamp,
            )
        )
        owned_journal.record(
            "voiceprint_recorded",
            source="cli",
            person_id=name,
            n_utterances=len(utterances),
            is_negative=negative,
        )
    print(
        json.dumps(
            {
                "person_id": name,
                "n_utterances": len(utterances),
                "dim": len(embedding),
                "is_negative": negative,
            }
        )
    )


def voiceprint_verify(
    name: str,
    clip: str,
    *,
    settings: Settings,
    store: VoiceprintStore | None = None,
    journal: Journal | None = None,
    embedder: SpeakerEmbedder | None = None,
) -> None:
    _label, frames, count = _read_clip_frames(clip)
    with (
        _open_store(settings, store) as owned_store,
        _open_journal(settings, journal) as owned_journal,
    ):
        probe = _embedder_for(settings, embedder).embed([_to_utterance(frames, count)])
        prints = owned_store.load_all()
        target = next((record for record in prints if record.person_id == name), None)
        if target is None:
            score = -1.0
            runner_up = -1.0
            outcome = "unknown"
        else:
            score = cosine(probe, target.embedding)
            runner_up = max(
                [
                    cosine(probe, record.embedding)
                    for record in prints
                    if record.person_id != name and not record.is_negative
                ],
                default=-1.0,
            )
            negatives = max(
                [cosine(probe, record.embedding) for record in prints if record.is_negative],
                default=-1.0,
            )
            if negatives >= score:
                outcome = "unknown"
            elif score < settings.identity.voiceprint_threshold:
                outcome = "unknown"
            elif score - runner_up < settings.identity.voiceprint_min_margin:
                outcome = "conflict"
            else:
                outcome = "verified"
                owned_store.touch_used(name, datetime.now(UTC))
        rounded_score = round(score, 4)
        rounded_margin = round(score - runner_up, 4)
        owned_journal.record(
            "voiceprint_decided",
            source="cli",
            person_id=name,
            outcome=outcome,
            score=rounded_score,
            margin=rounded_margin,
        )
    print(
        json.dumps(
            {
                "person_id": name,
                "outcome": outcome,
                "score": rounded_score,
                "margin": rounded_margin,
            }
        )
    )


def voiceprint_delete(
    name: str,
    *,
    settings: Settings,
    store: VoiceprintStore | None = None,
    journal: Journal | None = None,
) -> None:
    with (
        _open_store(settings, store) as owned_store,
        _open_journal(settings, journal) as owned_journal,
    ):
        if not owned_store.delete(name):
            raise CliError(f'no voiceprint enrolled for "{name}"')
        owned_journal.record("voiceprint_cleared", source="cli", person_id=name)
    print(json.dumps({"person_id": name, "removed": True}))


def voiceprint_list(
    *,
    settings: Settings,
    store: VoiceprintStore | None = None,
) -> None:
    with _open_store(settings, store) as owned_store:
        prints = owned_store.load_all()
    print(
        json.dumps(
            [
                {
                    "person_id": record.person_id,
                    "dim": record.dim,
                    "n_utterances": record.n_utterances,
                    "is_negative": record.is_negative,
                    "enrolled_at": record.enrolled_at,
                    "last_used_at": record.last_used_at,
                }
                for record in prints
            ]
        )
    )


def voiceprint_purge_expired(
    *,
    settings: Settings,
    store: VoiceprintStore | None = None,
    journal: Journal | None = None,
) -> None:
    with (
        _open_store(settings, store) as owned_store,
        _open_journal(settings, journal) as owned_journal,
    ):
        removed = owned_store.purge_expired(
            datetime.now(UTC), settings.identity.voiceprint_retention_days
        )
        for person_id in removed:
            owned_journal.record("voiceprint_expired", source="cli", person_id=person_id)
    print(json.dumps({"removed": removed}))
