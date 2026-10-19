from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, override

from fakeyou.db.voiceprint_store import VoiceprintRecord
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder


class InMemoryVoiceprints:
    def __init__(self, records: Iterable[VoiceprintRecord] = ()) -> None:
        self._records = {record.person_id: record for record in records}

    def save(self, record: VoiceprintRecord) -> None:
        self._records[record.person_id] = record

    def load_all(self) -> list[VoiceprintRecord]:
        return [self._records[person_id] for person_id in sorted(self._records)]

    def delete(self, person_id: str) -> bool:
        return self._records.pop(person_id, None) is not None

    def purge_expired(self, now: datetime, retention_days: int) -> list[str]:
        cutoff = now - timedelta(days=retention_days)
        expired = sorted(
            person_id for person_id, record in self._records.items() if record.last_used_at < cutoff
        )
        for person_id in expired:
            del self._records[person_id]
        return expired

    def touch_used(self, person_id: str, now: datetime) -> None:
        if person_id in self._records:
            self._records[person_id] = replace(self._records[person_id], last_used_at=now)


CONSENT_AT = datetime(2026, 10, 7, 10, 0, 0, tzinfo=UTC)

ENROLLED_AT = datetime(2026, 10, 7, 10, 0, 1, tzinfo=UTC)

LAST_USED_AT = datetime(2026, 10, 7, 10, 0, 2, tzinfo=UTC)


class FakeEmbedder(SpeakerEmbedder):
    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector
        self.calls: list[list[tuple[float, ...]]] = []

    @override
    def embed(self, utterances: Sequence[Sequence[float]]) -> tuple[float, ...]:
        self.calls.append([tuple(utterance) for utterance in utterances])
        return self._vector


def make_record(
    person_id: str, embedding: tuple[float, ...], owner_name: str = ""
) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts=CONSENT_AT,
        policy_version="v1",
        is_negative=False,
        enrolled_at=ENROLLED_AT,
        last_used_at=LAST_USED_AT,
        owner_name=owner_name,
    )


class InertExtractor:
    def create_stream(self) -> NoReturn:
        raise AssertionError("this embedder must not extract audio")

    def compute(self, stream: Any, /) -> list[float]:
        raise AssertionError(f"this embedder must not compute {stream!r}")
