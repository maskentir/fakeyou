import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from fakeyou.db.session_status import PhaseUpdate, SessionStatusStore

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


async def test_a_new_session_starts_created_and_cannot_be_created_twice(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)

    created = await store.create("m1", "owner-1", NOW)

    assert created is not None
    assert (created.desired_state, created.phase, created.terminal_reason) == (
        "created",
        "created",
        None,
    )
    assert await store.create("m1", "owner-1", LATER) is None
    assert await store.get("m1") == created


async def test_the_owner_lists_only_their_sessions_in_creation_order(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await store.create("m2", "owner-1", LATER)
    await store.create("m1", "owner-1", NOW)
    await store.create("m3", "owner-2", NOW)

    listed = await store.list_for("owner-1")

    assert [status.meeting_id for status in listed] == ["m1", "m2"]


async def test_a_terminal_phase_keeps_its_reason_and_detail_and_is_final(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await store.create("m1", "owner-1", NOW)
    await store.record_phase("m1", PhaseUpdate("active", NOW))

    ended = await store.record_phase(
        "m1",
        PhaseUpdate("ended", LATER, terminal_reason="left_by_participant", detail="asked to leave"),
    )
    late = await store.record_phase("m1", PhaseUpdate("active", LATER + timedelta(seconds=1)))

    assert ended is not None
    assert ended.terminal
    assert (ended.terminal_reason, ended.detail) == ("left_by_participant", "asked to leave")
    assert late is None
    assert await store.get("m1") == ended


async def test_desired_state_is_written_without_touching_the_phase(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await store.create("m1", "owner-1", NOW)
    await store.record_phase("m1", PhaseUpdate("lobby", NOW))

    wanted = await store.desire("m1", "joined", LATER)

    assert wanted is not None
    assert (wanted.desired_state, wanted.phase) == ("joined", "lobby")
    assert await store.desire("missing", "stopped", LATER) is None


async def test_desire_changes_only_a_different_live_wish(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await store.create("m1", "owner-1", NOW)

    first = await store.desire("m1", "joined", LATER)
    second = await store.desire("m1", "joined", LATER)
    await store.record_phase("m1", PhaseUpdate("ended", LATER, terminal_reason="ended_by_host"))

    assert first is not None
    assert second is None
    assert await store.desire("m1", "stopped", LATER) is None


async def test_a_discarded_session_is_gone(migrated_async_engine: AsyncEngine) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await store.create("m1", "owner-1", NOW)

    await store.discard("m1")

    assert await store.get("m1") is None
    assert await store.create("m1", "owner-1", LATER) is not None


async def test_the_schema_is_checked_once_per_store(migrated_async_engine: AsyncEngine) -> None:
    store = SessionStatusStore(migrated_async_engine)

    await store.require_current()
    await store.require_current()


async def _wanted(store: SessionStatusStore, meeting_id: str, at: datetime = NOW) -> None:
    await store.create(meeting_id, "owner-1", at)
    await store.desire(meeting_id, "joined", at)


async def test_a_claim_takes_one_wanted_session_and_fences_it(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await store.create("idle", "owner-1", NOW)
    await _wanted(store, "m1")

    claimed = await store.claim("worker-a", "attempt-1", 30.0)
    again = await store.claim("worker-b", "attempt-2", 30.0)

    assert claimed is not None
    assert (claimed.meeting_id, claimed.attempt_id, claimed.attempts) == ("m1", "attempt-1", 1)
    assert claimed.lease_owner == "worker-a"
    assert again is None
    stale = await store.record_phase("m1", PhaseUpdate("active", LATER), attempt_id="attempt-0")
    fresh = await store.record_phase("m1", PhaseUpdate("active", LATER), attempt_id="attempt-1")
    assert stale is None
    assert fresh is not None


async def test_only_the_holder_renews_and_a_release_frees_the_session(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await _wanted(store, "m1")
    await store.claim("worker-a", "attempt-1", 30.0)

    held = await store.renew("m1", "worker-a", "attempt-1", 30.0)
    foreign = await store.renew("m1", "worker-b", "attempt-1", 30.0)
    await store.release("m1", "worker-a", "attempt-1")
    reclaimed = await store.claim("worker-b", "attempt-2", 30.0)

    assert held is not None
    assert foreign is None
    assert reclaimed is not None
    assert (reclaimed.lease_owner, reclaimed.attempts) == ("worker-b", 2)
    assert await store.renew("m1", "worker-a", "attempt-1", 30.0) is None


async def test_an_expired_lease_is_claimed_by_the_next_worker(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await _wanted(store, "m1")
    await store.claim("worker-a", "attempt-1", -1.0)

    taken = await store.claim("worker-b", "attempt-2", 30.0)

    assert taken is not None
    assert (taken.lease_owner, taken.attempt_id) == ("worker-b", "attempt-2")


async def test_concurrent_claims_never_take_the_same_session(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await _wanted(store, "m1", NOW)
    await _wanted(store, "m2", LATER)

    first, second = await asyncio.gather(
        store.claim("worker-a", "attempt-a", 30.0), store.claim("worker-b", "attempt-b", 30.0)
    )

    claimed = {status.meeting_id for status in (first, second) if status is not None}
    assert claimed == {"m1", "m2"}


async def test_finished_and_unwanted_sessions_are_never_claimed(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = SessionStatusStore(migrated_async_engine)
    await store.create("created", "owner-1", NOW)
    await _wanted(store, "done")
    await store.record_phase("done", PhaseUpdate("ended", LATER, terminal_reason="ended_by_host"))

    assert await store.claim("worker-a", "attempt-1", 30.0) is None
