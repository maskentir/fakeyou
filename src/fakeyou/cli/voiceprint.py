import json
import math
import re
import secrets
import struct
import uuid
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import NoReturn

from fakeyou.audio.deaf import rms_level
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.db.engine import open_database
from fakeyou.db.schema import require_current
from fakeyou.db.voiceprint_store import VoiceprintRecord, VoiceprintRepository, VoiceprintStore
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder, cosine, load_embedder
from fakeyou.observability.journal import Journal, JournalFile, journal_path

VOICEPRINT_MIN_RMS = 0.01
VOICEPRINT_SAMPLE_RATE = 16000
VOICEPRINT_JOURNAL_ID = "voiceprint"
MIN_ENROLL_CLIPS = 3
PROVISIONAL_SNR_FLOOR_DB = 10.0
ENROLL_MIN_SECONDS = 30.0
ENROLL_MAX_TRANSCRIPT_EDITS = 1
_SNR_FRAME = 320
_SNR_CEILING_DB = 60.0


@dataclass(frozen=True, slots=True)
class EnrollSubject:
    name: str
    negative: bool = False


@dataclass(frozen=True, slots=True)
class LivenessGate:
    challenge_id: str | None = None
    challenge: str | None = None
    transcript: str | None = None
    owner_name: str | None = None
    spontaneous_clip: tuple[str, ...] = ()


DEFAULT_LIVENESS_GATE = LivenessGate()


@dataclass(frozen=True, slots=True)
class VoiceprintDeps:
    store: VoiceprintRepository | None = None
    journal: Journal | None = None
    embedder: SpeakerEmbedder | None = None


DEFAULT_VOICEPRINT_DEPS = VoiceprintDeps()


@contextmanager
def _open_store(
    settings: Settings, store: VoiceprintRepository | None
) -> Iterator[VoiceprintRepository]:
    if store is not None:
        yield store
        return
    with open_database(settings) as engine:
        require_current(engine)
        yield VoiceprintStore(engine)


@contextmanager
def _open_journal(settings: Settings, journal: Journal | None) -> Iterator[Journal]:
    if journal is not None:
        yield journal
        return
    owned = Journal(
        VOICEPRINT_JOURNAL_ID,
        file=JournalFile(journal_path(settings.paths.journal_dir, VOICEPRINT_JOURNAL_ID)),
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
        raise FakeYouError(ErrorCode.VOICEPRINT_CLIP_UNREADABLE, label=label) from None
    except (wave.Error, EOFError, OSError):
        raise FakeYouError(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED, label=label) from None
    with handle:
        if (
            handle.getnchannels() != 1
            or handle.getsampwidth() != 2
            or handle.getframerate() != VOICEPRINT_SAMPLE_RATE
        ):
            raise FakeYouError(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED, label=label)
        count = handle.getnframes()
        frames = handle.readframes(count)
        if len(frames) != count * 2:
            raise FakeYouError(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED, label=label)
    return label, frames, count


def _to_utterance(frames: bytes, count: int) -> list[float]:
    samples = struct.unpack(f"<{count}h", frames[: count * 2])
    return [sample / 32768.0 for sample in samples]


def _digit_words(path: Path | None = None) -> dict[str, str]:
    try:
        if path is None:
            text = (files("fakeyou") / "assets" / "digit_words.json").read_text(encoding="utf-8")
        else:
            text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    words: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if len(value) != 1 or not value.isascii() or not value.isdigit():
            continue
        words[key.lower()] = value
    return words


def _digits_only(value: str, path: Path | None = None) -> str:
    words = _digit_words(path)
    parts: list[str] = []
    for token in re.findall(r"[0-9A-Za-z]+", value.lower()):
        if token.isdigit():
            parts.append(token)
        elif token in words:
            parts.append(words[token])
        else:
            parts.append("".join(char for char in token if char.isdigit()))
    return "".join(parts)


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[len(right)]


def _snr_db(samples: list[float]) -> float:
    frames = [samples[index : index + _SNR_FRAME] for index in range(0, len(samples), _SNR_FRAME)]
    energies = sorted(sum(value * value for value in frame) / len(frame) for frame in frames)
    if not energies:
        return 0.0
    edge = max(1, len(energies) // 10)
    noise = sum(energies[:edge]) / edge
    signal = sum(energies[-edge:]) / edge
    if noise <= 0.0:
        return _SNR_CEILING_DB if signal > 0.0 else 0.0
    if signal <= noise:
        return 0.0
    return min(_SNR_CEILING_DB, 10.0 * math.log10((signal - noise) / noise))


def _load_utterances(paths: list[str], min_seconds: float) -> tuple[list[list[float]], float]:
    utterances: list[list[float]] = []
    total_seconds = 0.0
    for path in paths:
        label, frames, count = _read_clip_frames(path)
        seconds = count / VOICEPRINT_SAMPLE_RATE
        if count == 0 or seconds < min_seconds:
            raise FakeYouError(
                ErrorCode.VOICEPRINT_CLIP_TOO_SHORT,
                label=label,
                seconds=seconds,
                minimum=min_seconds,
            )
        level = rms_level(frames, channels=1)
        if level < VOICEPRINT_MIN_RMS:
            raise FakeYouError(
                ErrorCode.VOICEPRINT_CLIP_SILENT,
                label=label,
                level=level,
                threshold=VOICEPRINT_MIN_RMS,
            )
        utterances.append(_to_utterance(frames, count))
        total_seconds += seconds
    return utterances, total_seconds


def _liveness_reason(
    *,
    wanted: str,
    heard: str,
    total_seconds: float,
    segments: dict[str, int],
    snr: float,
) -> str | None:
    if not wanted or not heard:
        return "challenge transcript holds no digits to verify liveness"
    distance = _edit_distance(wanted, heard)
    if distance > ENROLL_MAX_TRANSCRIPT_EDITS:
        return (
            f"challenge transcript differs by {distance} edits,"
            f" need at most {ENROLL_MAX_TRANSCRIPT_EDITS}"
        )
    if total_seconds < ENROLL_MIN_SECONDS:
        return (
            f"enrollment audio holds {total_seconds:.2f}s, need at least {ENROLL_MIN_SECONDS:.1f}s"
        )
    if segments["read"] < 1 or segments["spontaneous"] < 1:
        return "enrollment needs at least 1 read segment and 1 spontaneous segment"
    if snr < PROVISIONAL_SNR_FLOOR_DB:
        return f"enrollment snr {snr:.2f}db below floor {PROVISIONAL_SNR_FLOOR_DB:.1f}db"
    return None


def voiceprint_challenge(*, settings: Settings) -> None:
    emit_json(
        {
            "challenge": f"{secrets.randbelow(10**8):08d}",
            "challenge_id": str(uuid.uuid4()),
        }
    )


def voiceprint_enroll(
    subject: EnrollSubject,
    clip: list[str],
    *,
    gate: LivenessGate = DEFAULT_LIVENESS_GATE,
    settings: Settings,
    deps: VoiceprintDeps = DEFAULT_VOICEPRINT_DEPS,
) -> None:
    spontaneous = list(gate.spontaneous_clip)
    gated = (
        len(spontaneous) > 0
        or gate.challenge_id is not None
        or gate.challenge is not None
        or gate.transcript is not None
        or gate.owner_name is not None
    )
    if gated and not (gate.challenge_id and gate.challenge and gate.transcript and gate.owner_name):
        raise FakeYouError(ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE)
    paths = list(clip) + spontaneous
    if len(paths) < MIN_ENROLL_CLIPS:
        raise FakeYouError(
            ErrorCode.VOICEPRINT_ENROLL_TOO_FEW_CLIPS, minimum=MIN_ENROLL_CLIPS, count=len(paths)
        )
    utterances, total_seconds = _load_utterances(paths, settings.identity.voiceprint_min_seconds)
    if not gated:
        with (
            _open_store(settings, deps.store) as owned_store,
            _open_journal(settings, deps.journal) as owned_journal,
        ):
            embedding = _embedder_for(settings, deps.embedder).embed(utterances)
            stamp = datetime.now(UTC)
            owned_store.save(
                VoiceprintRecord(
                    person_id=subject.name,
                    embedding=tuple(embedding),
                    dim=len(embedding),
                    n_utterances=len(utterances),
                    consent_ts=stamp,
                    policy_version=settings.identity.voiceprint_policy_version,
                    is_negative=subject.negative,
                    enrolled_at=stamp,
                    last_used_at=stamp,
                    owner_name="",
                )
            )
            owned_journal.record(
                "voiceprint_recorded",
                source="cli",
                person_id=subject.name,
                n_utterances=len(utterances),
                is_negative=subject.negative,
            )
        emit_json(
            {
                "person_id": subject.name,
                "n_utterances": len(utterances),
                "dim": len(embedding),
                "is_negative": subject.negative,
            }
        )
        return
    assert gate.challenge_id and gate.challenge and gate.transcript and gate.owner_name
    segments = {"read": len(clip), "spontaneous": len(spontaneous)}
    snr = min(_snr_db(utterance) for utterance in utterances)
    with _open_journal(settings, deps.journal) as owned_journal:
        heard = _digits_only(gate.transcript)
        wanted = _digits_only(gate.challenge)

        def _reject(reason: str) -> NoReturn:
            owned_journal.record(
                "enrollment_rejected",
                source="cli",
                person_id=subject.name,
                challenge_id=gate.challenge_id,
                snr_db=round(snr, 2),
                seconds=round(total_seconds, 2),
                segments=segments,
            )
            emit_json({"enrolled": False, "reason": reason, "challenge_id": gate.challenge_id})
            raise SystemExit(ExitCode.INPUT_REJECTED)

        reason = _liveness_reason(
            wanted=wanted,
            heard=heard,
            total_seconds=total_seconds,
            segments=segments,
            snr=snr,
        )
        if reason is not None:
            _reject(reason)
        with _open_store(settings, deps.store) as owned_store:
            embedding = _embedder_for(settings, deps.embedder).embed(utterances)
            stamp = datetime.now(UTC)
            owned_store.save(
                VoiceprintRecord(
                    person_id=subject.name,
                    embedding=tuple(embedding),
                    dim=len(embedding),
                    n_utterances=len(utterances),
                    consent_ts=stamp,
                    policy_version=settings.identity.voiceprint_policy_version,
                    is_negative=subject.negative,
                    enrolled_at=stamp,
                    last_used_at=stamp,
                    owner_name=gate.owner_name,
                )
            )
        owned_journal.record(
            "enrollment_accepted",
            source="cli",
            person_id=subject.name,
            challenge_id=gate.challenge_id,
            snr_db=round(snr, 2),
            seconds=round(total_seconds, 2),
            segments=segments,
        )
        owned_journal.record(
            "voiceprint_recorded",
            source="cli",
            person_id=subject.name,
            n_utterances=len(utterances),
            is_negative=subject.negative,
        )
    emit_json(
        {
            "person_id": subject.name,
            "n_utterances": len(utterances),
            "dim": len(embedding),
            "is_negative": subject.negative,
            "challenge_id": gate.challenge_id,
            "owner_name": gate.owner_name,
        }
    )


def voiceprint_verify(
    name: str,
    clip: str,
    *,
    settings: Settings,
    deps: VoiceprintDeps = DEFAULT_VOICEPRINT_DEPS,
) -> None:
    label, frames, count = _read_clip_frames(clip)
    seconds = count / VOICEPRINT_SAMPLE_RATE
    if count == 0 or seconds < settings.identity.voiceprint_min_seconds:
        raise FakeYouError(
            ErrorCode.VOICEPRINT_CLIP_TOO_SHORT,
            label=label,
            seconds=seconds,
            minimum=settings.identity.voiceprint_min_seconds,
        )
    utterance = _to_utterance(frames, count)
    with (
        _open_store(settings, deps.store) as owned_store,
        _open_journal(settings, deps.journal) as owned_journal,
    ):
        prints = owned_store.load_all()
        target = next((record for record in prints if record.person_id == name), None)
        if target is None:
            score = -1.0
            runner_up = -1.0
            outcome = "unknown"
        else:
            probe = _embedder_for(settings, deps.embedder).embed([utterance])
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
            if negatives >= score or score < settings.identity.voiceprint_threshold:
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
    emit_json(
        {
            "person_id": name,
            "outcome": outcome,
            "score": rounded_score,
            "margin": rounded_margin,
        }
    )


def voiceprint_delete(
    name: str,
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
    journal: Journal | None = None,
) -> None:
    with (
        _open_store(settings, store) as owned_store,
        _open_journal(settings, journal) as owned_journal,
    ):
        if not owned_store.delete(name):
            raise FakeYouError(ErrorCode.VOICEPRINT_NOT_ENROLLED, name=name)
        owned_journal.record("voiceprint_cleared", source="cli", person_id=name)
    emit_json({"person_id": name, "removed": True})


def voiceprint_list(
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
) -> None:
    with _open_store(settings, store) as owned_store:
        prints = owned_store.load_all()
    emit_json(
        [
            {
                "person_id": record.person_id,
                "dim": record.dim,
                "n_utterances": record.n_utterances,
                "is_negative": record.is_negative,
                "enrolled_at": record.enrolled_at.isoformat(),
                "last_used_at": record.last_used_at.isoformat(),
            }
            for record in prints
        ]
    )


def voiceprint_purge_expired(
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
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
    emit_json({"removed": removed})
