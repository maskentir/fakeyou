from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS voiceprints (
    person_id TEXT PRIMARY KEY,
    embedding BYTEA NOT NULL,
    dim SMALLINT NOT NULL,
    n_utterances INTEGER NOT NULL,
    consent_ts TIMESTAMPTZ NOT NULL,
    policy_version TEXT NOT NULL,
    is_negative BOOLEAN NOT NULL DEFAULT FALSE,
    enrolled_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ NOT NULL
)"""


@dataclass(frozen=True, slots=True)
class VoiceprintRecord:
    person_id: str
    embedding: tuple[float, ...]
    dim: int
    n_utterances: int
    consent_ts: str
    policy_version: str
    is_negative: bool
    enrolled_at: str
    last_used_at: str


def ensure_voiceprint_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def pack_embedding(embedding: tuple[float, ...]) -> bytes:
    return struct.pack("<" + "f" * len(embedding), *embedding)


def unpack_embedding(blob: bytes, dim: int) -> tuple[float, ...]:
    return struct.unpack("<" + "f" * dim, bytes(blob))


class VoiceprintStore:
    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def save(self, record: VoiceprintRecord) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO voiceprints (person_id, embedding, dim, n_utterances,"
                " consent_ts, policy_version, is_negative, enrolled_at, last_used_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (person_id) DO UPDATE SET embedding = EXCLUDED.embedding,"
                " dim = EXCLUDED.dim, n_utterances = EXCLUDED.n_utterances,"
                " consent_ts = EXCLUDED.consent_ts, policy_version = EXCLUDED.policy_version,"
                " is_negative = EXCLUDED.is_negative, enrolled_at = EXCLUDED.enrolled_at,"
                " last_used_at = EXCLUDED.last_used_at",
                (
                    record.person_id,
                    pack_embedding(record.embedding),
                    record.dim,
                    record.n_utterances,
                    record.consent_ts,
                    record.policy_version,
                    record.is_negative,
                    record.enrolled_at,
                    record.last_used_at,
                ),
            )
        conn.commit()

    def load_all(self) -> list[VoiceprintRecord]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT person_id, embedding, dim, n_utterances, consent_ts,"
                " policy_version, is_negative, enrolled_at, last_used_at"
                " FROM voiceprints ORDER BY person_id"
            )
            rows = cur.fetchall()
        return [
            VoiceprintRecord(
                person_id=row[0],
                embedding=unpack_embedding(row[1], row[2]),
                dim=row[2],
                n_utterances=row[3],
                consent_ts=str(row[4]),
                policy_version=row[5],
                is_negative=bool(row[6]),
                enrolled_at=str(row[7]),
                last_used_at=str(row[8]),
            )
            for row in rows
        ]

    def delete(self, person_id: str) -> bool:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM voiceprints WHERE person_id = %s", (person_id,))
            removed: bool = cur.rowcount > 0
        conn.commit()
        return removed

    def purge_expired(self, now: datetime, retention_days: int) -> list[str]:
        cutoff = now - timedelta(days=retention_days)
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM voiceprints WHERE last_used_at < %s RETURNING person_id",
                (cutoff,),
            )
            removed = [row[0] for row in cur.fetchall()]
        conn.commit()
        return removed

    def touch_used(self, person_id: str, now: datetime) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE voiceprints SET last_used_at = %s WHERE person_id = %s",
                (now, person_id),
            )
        conn.commit()
