from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine

from fakeyou.db.engine import create_engine_for_url
from fakeyou.db.voiceprint_store import (
    VoiceprintRecord,
    VoiceprintRepository,
    VoiceprintStore,
    pack_embedding,
    unpack_embedding,
)
from tests._fakes_common import DATABASE_URL

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
OFFSET = timezone(timedelta(hours=7))
EMBEDDING = (0.1, -0.2, 0.3, 0.4)


def make_record(
    person_id: str = "owner-1",
    *,
    embedding: tuple[float, ...] = EMBEDDING,
    last_used_at: datetime = NOW,
    is_negative: bool = False,
    owner_name: str = "Maya",
) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts=datetime(2026, 10, 7, 10, 0, tzinfo=OFFSET),
        policy_version="v1",
        is_negative=is_negative,
        enrolled_at=datetime(2026, 10, 7, 10, 0, 1, tzinfo=OFFSET),
        last_used_at=last_used_at,
        owner_name=owner_name,
    )


def test_save_then_load_all_round_trips_the_record(migrated_engine: Engine) -> None:
    store = VoiceprintStore(migrated_engine)
    record = make_record()

    store.save(record)

    [loaded] = store.load_all()
    assert loaded.embedding == pytest.approx(record.embedding)
    assert replace(loaded, embedding=record.embedding) == record
    assert all(
        moment.utcoffset() is not None
        for moment in (loaded.consent_ts, loaded.enrolled_at, loaded.last_used_at)
    )


def test_save_with_the_same_person_id_overwrites_the_record(migrated_engine: Engine) -> None:
    store = VoiceprintStore(migrated_engine)
    store.save(make_record(owner_name="Old", embedding=(0.1, 0.2, 0.3, 0.4)))
    replacement = make_record(owner_name="New", embedding=(0.5, 0.6, 0.7, 0.8))

    store.save(replacement)

    [loaded] = store.load_all()
    assert loaded.embedding == pytest.approx(replacement.embedding)
    assert replace(loaded, embedding=replacement.embedding) == replacement


def test_load_all_is_ordered_by_person_id(migrated_engine: Engine) -> None:
    store = VoiceprintStore(migrated_engine)
    for person_id in ("carol", "alice", "bob"):
        store.save(make_record(person_id))

    assert [record.person_id for record in store.load_all()] == ["alice", "bob", "carol"]


def test_delete_returns_true_once_then_false(migrated_engine: Engine) -> None:
    store = VoiceprintStore(migrated_engine)
    store.save(make_record())

    assert store.delete("owner-1") is True
    assert store.delete("owner-1") is False
    assert store.load_all() == []


def test_purge_expired_removes_only_rows_older_than_the_retention_window(
    migrated_engine: Engine,
) -> None:
    store = VoiceprintStore(migrated_engine)
    cutoff = NOW - timedelta(days=90)
    store.save(make_record("alice", last_used_at=cutoff - timedelta(seconds=1)))
    store.save(make_record("bob", last_used_at=cutoff - timedelta(days=400)))
    store.save(make_record("carol", last_used_at=cutoff))
    store.save(make_record("dave", last_used_at=NOW))

    removed = store.purge_expired(NOW, 90)

    assert sorted(removed) == ["alice", "bob"]
    assert [record.person_id for record in store.load_all()] == ["carol", "dave"]


@pytest.mark.parametrize(
    ("person_id", "expected"),
    [("owner-1", LATER), ("ghost", NOW)],
    ids=["existing", "missing"],
)
def test_touch_used_updates_only_an_existing_person(
    migrated_engine: Engine, person_id: str, expected: datetime
) -> None:
    store = VoiceprintStore(migrated_engine)
    store.save(make_record("owner-1", last_used_at=NOW))

    store.touch_used(person_id, LATER)

    assert [(record.person_id, record.last_used_at) for record in store.load_all()] == [
        ("owner-1", expected)
    ]


@pytest.mark.parametrize(
    ("is_negative", "owner_name"),
    [(True, "Maya"), (False, "")],
    ids=["negative-with-owner", "positive-without-owner"],
)
def test_is_negative_and_owner_name_round_trip(
    migrated_engine: Engine, *, is_negative: bool, owner_name: str
) -> None:
    store = VoiceprintStore(migrated_engine)
    store.save(make_record(is_negative=is_negative, owner_name=owner_name))

    [loaded] = store.load_all()
    assert (loaded.is_negative, loaded.owner_name) == (is_negative, owner_name)


def test_pack_and_unpack_embedding_round_trip() -> None:
    blob = pack_embedding(EMBEDDING)

    assert len(blob) == 4 * len(EMBEDDING)
    assert unpack_embedding(blob, len(EMBEDDING)) == pytest.approx(EMBEDDING)
    assert unpack_embedding(memoryview(blob), len(EMBEDDING)) == pytest.approx(EMBEDDING)


def test_store_is_accepted_where_a_voiceprint_repository_is_annotated() -> None:
    engine = create_engine_for_url(
        DATABASE_URL, connect_timeout_seconds=5.0, statement_timeout_seconds=30.0
    )
    try:
        repository: VoiceprintRepository = VoiceprintStore(engine)
    finally:
        engine.dispose()

    assert isinstance(repository, VoiceprintStore)
