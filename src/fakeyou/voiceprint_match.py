from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fakeyou.faults import Fault
from fakeyou.voiceprint_embed import SAMPLE_RATE, SpeakerEmbedder, cosine
from fakeyou.voiceprint_store import VoiceprintStore

if TYPE_CHECKING:
    from fakeyou.faults import FaultReporter
    from fakeyou.identity import SpeakerRegistry
    from fakeyou.journal import Journal

MatchOutcome = Literal["verified", "unknown", "conflict", "negative"]
DecidedOutcome = Literal["verified", "unknown", "conflict", "mismatched_owner"]

EMPTY_SCORE = -1.0


@dataclass(frozen=True, slots=True)
class VoiceScores:
    person_id: str | None
    s1: float
    s2: float
    outcome: MatchOutcome


class VoiceprintMatcher:
    def __init__(
        self, store: VoiceprintStore, *, threshold: float = 0.60, min_margin: float = 0.15
    ) -> None:
        self._store = store
        self._threshold = threshold
        self._min_margin = min_margin

    def match(self, embedding: Sequence[float]) -> VoiceScores:
        ranked: list[tuple[float, str]] = []
        best_negative = EMPTY_SCORE
        has_negative = False
        for record in self._store.load_all():
            score = cosine(embedding, record.embedding)
            if record.is_negative:
                has_negative = True
                best_negative = max(best_negative, score)
            else:
                ranked.append((score, record.person_id))
        ranked.sort(reverse=True)
        person = ranked[0][1] if ranked else None
        s1 = ranked[0][0] if ranked else EMPTY_SCORE
        s2 = ranked[1][0] if len(ranked) > 1 else EMPTY_SCORE
        if has_negative and best_negative >= s1:
            return VoiceScores(person, s1, s2, "negative")
        if s1 < self._threshold:
            return VoiceScores(person, s1, s2, "unknown")
        if s1 - s2 < self._min_margin:
            return VoiceScores(person, s1, s2, "conflict")
        return VoiceScores(person, s1, s2, "verified")


def score_turn_audio(
    clip: bytes,
    embedder: SpeakerEmbedder,
    matcher: VoiceprintMatcher,
    *,
    min_seconds: float,
) -> VoiceScores | None:
    usable = len(clip) - len(clip) % 2
    if usable <= 0 or usable / (SAMPLE_RATE * 2) < min_seconds:
        return None
    samples = struct.unpack("<" + "h" * (usable // 2), clip[:usable])
    utterance = [sample / 32768.0 for sample in samples]
    return matcher.match(embedder.embed([utterance]))


def apply_voice_scores(
    registry: SpeakerRegistry,
    speaker_uuid: str,
    scores: VoiceScores,
    *,
    device: str | None,
    owners: Mapping[str, str],
    journal: Journal | None = None,
    faults: FaultReporter | None = None,
) -> DecidedOutcome:
    registry.note_voice_scores(speaker_uuid, scores)
    person = scores.person_id
    owner: str | None = None
    if scores.outcome == "verified" and person is not None and device is not None:
        owner = owners.get(device)
    if owner is not None and person is not None and owner != person:
        if faults is not None:
            faults.record(
                Fault(
                    source="audio",
                    kind="voiceprint_misbind",
                    detail=f"voice verified as {person} on device owned by {owner}",
                )
            )
        if journal is not None:
            journal.record(
                "voiceprint_decided",
                source="audio",
                person_id=person,
                outcome="mismatched_owner",
                score=round(scores.s1, 4),
                margin=round(scores.s1 - scores.s2, 4),
                speaker_uuid=speaker_uuid,
                device=device,
                owner=owner,
            )
        return "mismatched_owner"
    outcome: DecidedOutcome = "unknown" if scores.outcome == "negative" else scores.outcome
    if journal is not None:
        journal.record(
            "voiceprint_decided",
            source="audio",
            person_id=person,
            outcome=outcome,
            score=round(scores.s1, 4),
            margin=round(scores.s1 - scores.s2, 4),
            speaker_uuid=speaker_uuid,
        )
    return outcome
