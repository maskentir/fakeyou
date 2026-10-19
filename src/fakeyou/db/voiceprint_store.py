from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from fakeyou.db.tables import voiceprints

UPDATABLE_COLUMNS = tuple(name for name in voiceprints.c.keys() if name != "person_id")


@dataclass(frozen=True, slots=True)
class VoiceprintRecord:
    person_id: str
    embedding: tuple[float, ...]
    dim: int
    n_utterances: int
    consent_ts: datetime
    policy_version: str
    is_negative: bool
    enrolled_at: datetime
    last_used_at: datetime
    owner_name: str = ""


class VoiceprintRepository(Protocol):
    def save(self, record: VoiceprintRecord) -> None: ...

    def load_all(self) -> list[VoiceprintRecord]: ...

    def delete(self, person_id: str) -> bool: ...

    def purge_expired(self, now: datetime, retention_days: int) -> list[str]: ...

    def touch_used(self, person_id: str, now: datetime) -> None: ...


def pack_embedding(embedding: tuple[float, ...]) -> bytes:
    return struct.pack("<" + "f" * len(embedding), *embedding)


def unpack_embedding(blob: bytes | memoryview, dim: int) -> tuple[float, ...]:
    return struct.unpack("<" + "f" * dim, bytes(blob))


class VoiceprintStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, record: VoiceprintRecord) -> None:
        values = {
            "person_id": record.person_id,
            "embedding": pack_embedding(record.embedding),
            "dim": record.dim,
            "n_utterances": record.n_utterances,
            "consent_ts": record.consent_ts,
            "policy_version": record.policy_version,
            "is_negative": record.is_negative,
            "enrolled_at": record.enrolled_at,
            "last_used_at": record.last_used_at,
            "owner_name": record.owner_name,
        }
        statement = insert(voiceprints).values(values)
        upsert = statement.on_conflict_do_update(
            index_elements=[voiceprints.c.person_id],
            set_={name: statement.excluded[name] for name in UPDATABLE_COLUMNS},
        )
        with self._engine.begin() as connection:
            connection.execute(upsert)

    def load_all(self) -> list[VoiceprintRecord]:
        statement = select(voiceprints).order_by(voiceprints.c.person_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_record_from_row(row) for row in rows]

    def delete(self, person_id: str) -> bool:
        statement = delete(voiceprints).where(voiceprints.c.person_id == person_id)
        with self._engine.begin() as connection:
            result = connection.execute(statement)
        return result.rowcount > 0

    def purge_expired(self, now: datetime, retention_days: int) -> list[str]:
        cutoff = now - timedelta(days=retention_days)
        statement = (
            delete(voiceprints)
            .where(voiceprints.c.last_used_at < cutoff)
            .returning(voiceprints.c.person_id)
        )
        with self._engine.begin() as connection:
            return [row.person_id for row in connection.execute(statement)]

    def touch_used(self, person_id: str, now: datetime) -> None:
        statement = (
            update(voiceprints).where(voiceprints.c.person_id == person_id).values(last_used_at=now)
        )
        with self._engine.begin() as connection:
            connection.execute(statement)


def _record_from_row(row: RowMapping) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=row["person_id"],
        embedding=unpack_embedding(row["embedding"], row["dim"]),
        dim=row["dim"],
        n_utterances=row["n_utterances"],
        consent_ts=row["consent_ts"],
        policy_version=row["policy_version"],
        is_negative=row["is_negative"],
        enrolled_at=row["enrolled_at"],
        last_used_at=row["last_used_at"],
        owner_name=row["owner_name"],
    )
