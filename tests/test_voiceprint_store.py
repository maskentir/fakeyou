from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fakeyou.settings import DEFAULTS, IdentitySettings
from fakeyou.voiceprint_store import (
    MIGRATION_SQL,
    VoiceprintRecord,
    VoiceprintStore,
    ensure_voiceprint_schema,
    pack_embedding,
)


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self._conn.statements.append((sql, params))
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._conn.results.pop(0)

    @property
    def rowcount(self) -> int:
        return self._conn.rowcounts.pop(0)


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.results: list[list[tuple[Any, ...]]] = []
        self.rowcounts: list[int] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def make_record() -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id="owner-1",
        embedding=(0.5, -0.25, 0.0, 1.0),
        dim=4,
        n_utterances=3,
        consent_ts="2026-10-07T10:00:00+00:00",
        policy_version="v1",
        is_negative=False,
        enrolled_at="2026-10-07T10:00:01+00:00",
        last_used_at="2026-10-07T10:00:02+00:00",
        owner_name="Maya",
    )


def test_ensure_schema_executes_ddl_with_ten_columns() -> None:
    conn = FakeConnection()

    ensure_voiceprint_schema(conn)

    assert len(conn.statements) == 2
    sql = conn.statements[0][0]
    assert "CREATE TABLE IF NOT EXISTS voiceprints" in sql
    second = conn.statements[1][0]
    assert second == MIGRATION_SQL
    assert "ALTER TABLE voiceprints" in second
    assert "ADD COLUMN IF NOT EXISTS owner_name" in second
    for column in (
        "person_id",
        "embedding",
        "dim",
        "n_utterances",
        "consent_ts",
        "policy_version",
        "is_negative",
        "enrolled_at",
        "last_used_at",
        "owner_name",
    ):
        assert column in sql
    assert conn.commits == 1


def test_save_load_all_roundtrip_restores_identical_record() -> None:
    conn = FakeConnection()
    store = VoiceprintStore(lambda: conn)
    record = make_record()

    store.save(record)

    assert len(conn.statements) == 1
    params = conn.statements[0][1]
    row = (
        params[0],
        params[1],
        params[2],
        params[3],
        params[4],
        params[5],
        params[6],
        params[7],
        params[8],
        params[9],
    )
    conn.results.append([row])

    loaded = store.load_all()

    assert loaded == [record]
    assert loaded[0].embedding == record.embedding
    assert loaded[0].owner_name == "Maya"


def test_load_all_converts_datetime_columns_with_isoformat() -> None:
    conn = FakeConnection()
    store = VoiceprintStore(lambda: conn)
    record = make_record()
    conn.results.append(
        [
            (
                record.person_id,
                pack_embedding(record.embedding),
                record.dim,
                record.n_utterances,
                datetime.fromisoformat(record.consent_ts),
                record.policy_version,
                record.is_negative,
                datetime.fromisoformat(record.enrolled_at),
                datetime.fromisoformat(record.last_used_at),
                record.owner_name,
            )
        ]
    )

    loaded = store.load_all()

    assert loaded == [record]


def test_delete_returns_true_when_present_false_when_missing() -> None:
    present_conn = FakeConnection()
    present_conn.rowcounts.append(1)
    present_store = VoiceprintStore(lambda: present_conn)

    assert present_store.delete("owner-1") is True
    assert "DELETE FROM voiceprints" in present_conn.statements[0][0]
    assert present_conn.statements[0][1] == ("owner-1",)

    missing_conn = FakeConnection()
    missing_conn.rowcounts.append(0)
    missing_store = VoiceprintStore(lambda: missing_conn)

    assert missing_store.delete("ghost") is False


def test_purge_expired_deletes_only_expired_and_returns_ids() -> None:
    conn = FakeConnection()
    conn.results.append([("anna",), ("bob",)])
    store = VoiceprintStore(lambda: conn)
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)

    removed = store.purge_expired(now, 90)

    assert removed == ["anna", "bob"]
    sql, params = conn.statements[0]
    assert "DELETE FROM voiceprints" in sql
    assert "last_used_at" in sql
    assert params == (now - timedelta(days=90),)


def test_touch_used_updates_last_used_at() -> None:
    conn = FakeConnection()
    store = VoiceprintStore(lambda: conn)
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)

    store.touch_used("owner-1", now)

    sql, params = conn.statements[0]
    assert "UPDATE voiceprints" in sql
    assert "last_used_at" in sql
    assert params == (now, "owner-1")


def test_voiceprint_defaults_follow_identity_settings() -> None:
    assert DEFAULTS.identity.voiceprint_enabled is False
    assert DEFAULTS.identity.voiceprint_threshold == 0.60
    assert DEFAULTS.identity.voiceprint_min_margin == 0.15
    assert DEFAULTS.identity.voiceprint_min_seconds == 3.0
    assert DEFAULTS.identity.voiceprint_retention_days == 90
    assert DEFAULTS.identity.voiceprint_model_path == ""
    assert (
        DEFAULTS.identity.voiceprint_model_sha256
        == "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b"
    )
    assert DEFAULTS.identity.voiceprint_policy_version == "v1"
    assert IdentitySettings() == DEFAULTS.identity
