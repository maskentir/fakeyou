from __future__ import annotations

import json
import math
import re
import secrets
import uuid
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintRecord, VoiceprintRepository
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.foundation.numbers import rms
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES, pcm16_to_floats
from fakeyou.foundation.resources import read_package_text
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder, cosine, load_embedder
from fakeyou.observability.journal import Journal
from fakeyou.persistence.voiceprint_access import open_voiceprint_journal, open_voiceprint_store

VOICEPRINT_MIN_RMS = 0.01
MIN_ENROLL_CLIPS = 3
PROVISIONAL_SNR_FLOOR_DB = 10.0
ENROLL_MIN_SECONDS = 30.0
ENROLL_MAX_TRANSCRIPT_EDITS = 1
_SNR_FRAME = 320
_SNR_CEILING_DB = 60.0

VerifyOutcome = Literal["verified", "conflict", "unknown"]


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


@dataclass(frozen=True, slots=True)
class EnrollmentChallenge:
    challenge: str
    challenge_id: str


@dataclass(frozen=True, slots=True)
class EnrollmentAccepted:
    person_id: str
    n_utterances: int
    dim: int
    is_negative: bool
    challenge_id: str | None = None
    owner_name: str | None = None


@dataclass(frozen=True, slots=True)
class EnrollmentRejected:
    reason: str
    challenge_id: str


EnrollmentOutcome = EnrollmentAccepted | EnrollmentRejected


@dataclass(frozen=True, slots=True)
class ClipVerdict:
    person_id: str
    outcome: VerifyOutcome
    score: float
    margin: float


@dataclass(frozen=True, slots=True)
class _Challenge:
    challenge_id: str
    challenge: str
    transcript: str
    owner_name: str


@dataclass(frozen=True, slots=True)
class _Capture:
    subject: EnrollSubject
    utterances: list[list[float]]
    total_seconds: float
    read_clips: int
    spontaneous_clips: int


def issue_challenge() -> EnrollmentChallenge:
    return EnrollmentChallenge(
        challenge=f"{secrets.randbelow(10**8):08d}",
        challenge_id=str(uuid.uuid4()),
    )


def enroll_voiceprint(
    subject: EnrollSubject,
    clip: list[str],
    *,
    gate: LivenessGate = DEFAULT_LIVENESS_GATE,
    settings: Settings,
    deps: VoiceprintDeps = DEFAULT_VOICEPRINT_DEPS,
) -> EnrollmentOutcome:
    spontaneous = list(gate.spontaneous_clip)
    challenge = _challenge_of(gate, spontaneous)
    paths = list(clip) + spontaneous
    if len(paths) < MIN_ENROLL_CLIPS:
        raise FakeYouError(
            ErrorCode.VOICEPRINT_ENROLL_TOO_FEW_CLIPS, minimum=MIN_ENROLL_CLIPS, count=len(paths)
        )
    utterances, total_seconds = _load_utterances(paths, settings.identity.voiceprint_min_seconds)
    capture = _Capture(
        subject=subject,
        utterances=utterances,
        total_seconds=total_seconds,
        read_clips=len(clip),
        spontaneous_clips=len(spontaneous),
    )
    if challenge is None:
        return _enroll_ungated(capture, settings=settings, deps=deps)
    return _enroll_gated(capture, challenge, settings=settings, deps=deps)


def verify_voiceprint_clip(
    name: str,
    clip: str,
    *,
    settings: Settings,
    deps: VoiceprintDeps = DEFAULT_VOICEPRINT_DEPS,
) -> ClipVerdict:
    label, frames, count = _read_clip_frames(clip)
    seconds = count / CAPTURE_SAMPLE_RATE
    if count == 0 or seconds < settings.identity.voiceprint_min_seconds:
        raise FakeYouError(
            ErrorCode.VOICEPRINT_CLIP_TOO_SHORT,
            label=label,
            seconds=seconds,
            minimum=settings.identity.voiceprint_min_seconds,
        )
    utterance = _to_utterance(frames, count)
    with (
        open_voiceprint_store(settings, deps.store) as owned_store,
        open_voiceprint_journal(settings, deps.journal) as owned_journal,
    ):
        prints = owned_store.load_all()
        target = next((record for record in prints if record.person_id == name), None)
        outcome: VerifyOutcome = "unknown"
        score = -1.0
        runner_up = -1.0
        if target is not None:
            probe = _embedder_for(settings, deps.embedder).embed([utterance])
            score, runner_up, negatives = _rank_probe(probe, target.embedding, prints, name)
            outcome = _decide(score, runner_up, negatives, settings)
            if outcome == "verified":
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
    return ClipVerdict(person_id=name, outcome=outcome, score=rounded_score, margin=rounded_margin)


def delete_voiceprint(
    name: str,
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
    journal: Journal | None = None,
) -> None:
    with (
        open_voiceprint_store(settings, store) as owned_store,
        open_voiceprint_journal(settings, journal) as owned_journal,
    ):
        if not owned_store.delete(name):
            raise FakeYouError(ErrorCode.VOICEPRINT_NOT_ENROLLED, name=name)
        owned_journal.record("voiceprint_cleared", source="cli", person_id=name)


def list_voiceprints(
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
) -> list[VoiceprintRecord]:
    with open_voiceprint_store(settings, store) as owned_store:
        return owned_store.load_all()


def digit_words(path: Path | None = None) -> dict[str, str]:
    try:
        if path is None:
            text = read_package_text("assets", "digit_words.json")
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


def digits_only(value: str, path: Path | None = None) -> str:
    words = digit_words(path)
    parts: list[str] = []
    for token in re.findall(r"[0-9A-Za-z]+", value.lower()):
        if token.isdigit():
            parts.append(token)
        elif token in words:
            parts.append(words[token])
        else:
            parts.append("".join(char for char in token if char.isdigit()))
    return "".join(parts)


def _challenge_of(gate: LivenessGate, spontaneous: list[str]) -> _Challenge | None:
    supplied = (gate.challenge_id, gate.challenge, gate.transcript, gate.owner_name)
    if not spontaneous and all(value is None for value in supplied):
        return None
    if not (gate.challenge_id and gate.challenge and gate.transcript and gate.owner_name):
        raise FakeYouError(ErrorCode.VOICEPRINT_ENROLLMENT_GATE_INCOMPLETE)
    return _Challenge(
        challenge_id=gate.challenge_id,
        challenge=gate.challenge,
        transcript=gate.transcript,
        owner_name=gate.owner_name,
    )


def _embedder_for(settings: Settings, embedder: SpeakerEmbedder | None) -> SpeakerEmbedder:
    if embedder is not None:
        return embedder
    return load_embedder(
        settings.identity.voiceprint_model_path, settings.identity.voiceprint_model_sha256
    )


def _store_voiceprint(
    store: VoiceprintRepository,
    capture: _Capture,
    *,
    owner_name: str,
    settings: Settings,
    embedder: SpeakerEmbedder,
) -> int:
    embedding = embedder.embed(capture.utterances)
    stamp = datetime.now(UTC)
    store.save(
        VoiceprintRecord(
            person_id=capture.subject.name,
            embedding=tuple(embedding),
            dim=len(embedding),
            n_utterances=len(capture.utterances),
            consent_ts=stamp,
            policy_version=settings.identity.voiceprint_policy_version,
            is_negative=capture.subject.negative,
            enrolled_at=stamp,
            last_used_at=stamp,
            owner_name=owner_name,
        )
    )
    return len(embedding)


def _enroll_ungated(
    capture: _Capture, *, settings: Settings, deps: VoiceprintDeps
) -> EnrollmentAccepted:
    with (
        open_voiceprint_store(settings, deps.store) as owned_store,
        open_voiceprint_journal(settings, deps.journal) as owned_journal,
    ):
        dim = _store_voiceprint(
            owned_store,
            capture,
            owner_name="",
            settings=settings,
            embedder=_embedder_for(settings, deps.embedder),
        )
        owned_journal.record(
            "voiceprint_recorded",
            source="cli",
            person_id=capture.subject.name,
            n_utterances=len(capture.utterances),
            is_negative=capture.subject.negative,
        )
    return EnrollmentAccepted(
        person_id=capture.subject.name,
        n_utterances=len(capture.utterances),
        dim=dim,
        is_negative=capture.subject.negative,
    )


def _enroll_gated(
    capture: _Capture,
    challenge: _Challenge,
    *,
    settings: Settings,
    deps: VoiceprintDeps,
) -> EnrollmentOutcome:
    snr = min(_snr_db(utterance) for utterance in capture.utterances)
    segments = {"read": capture.read_clips, "spontaneous": capture.spontaneous_clips}
    name = capture.subject.name
    with open_voiceprint_journal(settings, deps.journal) as owned_journal:
        reason = _liveness_reason(
            wanted=digits_only(challenge.challenge),
            heard=digits_only(challenge.transcript),
            total_seconds=capture.total_seconds,
            segments=segments,
            snr=snr,
        )
        if reason is not None:
            owned_journal.record(
                "enrollment_rejected",
                source="cli",
                person_id=name,
                challenge_id=challenge.challenge_id,
                snr_db=round(snr, 2),
                seconds=round(capture.total_seconds, 2),
                segments=segments,
            )
            return EnrollmentRejected(reason=reason, challenge_id=challenge.challenge_id)
        with open_voiceprint_store(settings, deps.store) as owned_store:
            dim = _store_voiceprint(
                owned_store,
                capture,
                owner_name=challenge.owner_name,
                settings=settings,
                embedder=_embedder_for(settings, deps.embedder),
            )
        owned_journal.record(
            "enrollment_accepted",
            source="cli",
            person_id=name,
            challenge_id=challenge.challenge_id,
            snr_db=round(snr, 2),
            seconds=round(capture.total_seconds, 2),
            segments=segments,
        )
        owned_journal.record(
            "voiceprint_recorded",
            source="cli",
            person_id=name,
            n_utterances=len(capture.utterances),
            is_negative=capture.subject.negative,
        )
    return EnrollmentAccepted(
        person_id=name,
        n_utterances=len(capture.utterances),
        dim=dim,
        is_negative=capture.subject.negative,
        challenge_id=challenge.challenge_id,
        owner_name=challenge.owner_name,
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
            or handle.getframerate() != CAPTURE_SAMPLE_RATE
        ):
            raise FakeYouError(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED, label=label)
        count = handle.getnframes()
        frames = handle.readframes(count)
        if len(frames) != count * 2:
            raise FakeYouError(ErrorCode.VOICEPRINT_AUDIO_FORMAT_UNSUPPORTED, label=label)
    return label, frames, count


def _to_utterance(frames: bytes, count: int) -> list[float]:
    return pcm16_to_floats(frames[: count * PCM16_SAMPLE_BYTES])


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
        seconds = count / CAPTURE_SAMPLE_RATE
        if count == 0 or seconds < min_seconds:
            raise FakeYouError(
                ErrorCode.VOICEPRINT_CLIP_TOO_SHORT,
                label=label,
                seconds=seconds,
                minimum=min_seconds,
            )
        utterance = _to_utterance(frames, count)
        level = rms(utterance)
        if level < VOICEPRINT_MIN_RMS:
            raise FakeYouError(
                ErrorCode.VOICEPRINT_CLIP_SILENT,
                label=label,
                level=level,
                threshold=VOICEPRINT_MIN_RMS,
            )
        utterances.append(utterance)
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


def _rank_probe(
    probe: tuple[float, ...],
    target_embedding: tuple[float, ...],
    prints: list[VoiceprintRecord],
    name: str,
) -> tuple[float, float, float]:
    score = cosine(probe, target_embedding)
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
    return score, runner_up, negatives


def _decide(score: float, runner_up: float, negatives: float, settings: Settings) -> VerifyOutcome:
    if negatives >= score or score < settings.identity.voiceprint_threshold:
        return "unknown"
    if score - runner_up < settings.identity.voiceprint_min_margin:
        return "conflict"
    return "verified"
