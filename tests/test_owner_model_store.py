import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.owner_model import OwnerModelEntry, OwnerModelStore, TwinScope

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
SCOPE = TwinScope("owner-1", "twin-1")
OTHER = TwinScope("owner-1", "twin-2")
POSITION = OwnerModelEntry("position", "release", "Ship the beta after the load test passes.")
STYLE = OwnerModelEntry("style_example", "tone", "Short answers, numbers first.")


async def test_owner_authored_items_are_active_at_once_and_each_bumps_the_version(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)

    first = await store.add(SCOPE, POSITION, "owner", NOW)
    second = await store.add(SCOPE, STYLE, "import", LATER)
    snapshot = await store.snapshot(SCOPE)

    assert (first.status, first.model_version, first.decided_at) == ("approved", 1, NOW)
    assert second.model_version == 2
    assert snapshot.version == 2
    assert [item.entry for item in snapshot.items] == [POSITION, STYLE]


async def test_a_debrief_proposal_stays_out_of_the_model_until_approved(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    await store.add(SCOPE, STYLE, "owner", NOW)

    proposed = await store.add(SCOPE, POSITION, "debrief", NOW)

    assert (proposed.status, proposed.model_version, proposed.decided_at) == (
        "proposed",
        None,
        None,
    )
    assert await store.proposals(SCOPE) == [proposed]
    assert [item.entry for item in (await store.snapshot(SCOPE)).items] == [STYLE]

    approved = await store.decide(SCOPE, proposed.revision_id, approve=True, at=LATER)

    assert (approved.status, approved.model_version, approved.decided_at) == (
        "approved",
        2,
        LATER,
    )
    assert await store.proposals(SCOPE) == []
    assert [item.entry for item in (await store.snapshot(SCOPE)).items] == [STYLE, POSITION]


async def test_a_rejected_proposal_never_changes_the_version(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    proposed = await store.add(SCOPE, POSITION, "debrief", NOW)

    rejected = await store.decide(SCOPE, proposed.revision_id, approve=False, at=LATER)

    assert (rejected.status, rejected.model_version) == ("rejected", None)
    assert await store.snapshot(SCOPE) == (await store.snapshot(SCOPE, 0))
    assert (await store.snapshot(SCOPE)).version == 0
    with pytest.raises(FakeYouError, match="not awaiting a decision") as raised:
        await store.decide(SCOPE, proposed.revision_id, approve=True, at=LATER)
    assert raised.value.code is ErrorCode.OWNER_MODEL_NOT_PROPOSED


async def test_a_revision_replaces_the_item_and_older_versions_stay_readable(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    changed = OwnerModelEntry(
        "position", "release", "Ship the beta once support is staffed.", confidence="leaning"
    )

    revised = await store.revise(SCOPE, first.item_id, changed, "owner", LATER)

    assert (revised.item_id, revised.revision, revised.model_version) == (first.item_id, 2, 2)
    assert [item.entry for item in (await store.snapshot(SCOPE)).items] == [changed]
    assert [item.entry for item in (await store.snapshot(SCOPE, 1)).items] == [POSITION]
    assert [revision.revision for revision in await store.history(SCOPE, first.item_id)] == [1, 2]


async def test_a_pending_revision_keeps_the_approved_one_active(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    suggestion = OwnerModelEntry("position", "release", "Ship the beta next week.")

    pending = await store.revise(SCOPE, first.item_id, suggestion, "debrief", LATER)

    assert pending.status == "proposed"
    assert [item.entry for item in (await store.snapshot(SCOPE)).items] == [POSITION]


async def test_a_retired_item_leaves_the_model_and_cannot_be_revised(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)

    retired = await store.retire(SCOPE, first.item_id, LATER)

    assert (retired.status, retired.model_version, retired.entry) == ("retired", 2, POSITION)
    snapshot = await store.snapshot(SCOPE)
    assert (snapshot.version, snapshot.items) == (2, ())
    assert [item.entry for item in (await store.snapshot(SCOPE, 1)).items] == [POSITION]
    for attempt in (
        store.retire(SCOPE, first.item_id, LATER),
        store.revise(SCOPE, first.item_id, STYLE, "owner", LATER),
    ):
        with pytest.raises(FakeYouError, match="is retired") as raised:
            await attempt
        assert raised.value.code is ErrorCode.OWNER_MODEL_ITEM_RETIRED


async def test_a_proposal_for_a_retired_item_cannot_be_approved_into_the_model(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    suggestion = OwnerModelEntry("position", "release", "Ship the beta next week.")
    pending = await store.revise(SCOPE, first.item_id, suggestion, "debrief", NOW)
    await store.retire(SCOPE, first.item_id, LATER)

    with pytest.raises(FakeYouError, match="is retired") as raised:
        await store.decide(SCOPE, pending.revision_id, approve=True, at=LATER)

    assert raised.value.code is ErrorCode.OWNER_MODEL_ITEM_RETIRED
    assert (await store.snapshot(SCOPE)).items == ()


async def test_items_and_proposals_never_cross_twins(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    proposed = await store.add(SCOPE, STYLE, "debrief", NOW)

    assert (await store.snapshot(OTHER)).items == ()
    assert await store.proposals(OTHER) == []
    for attempt in (
        store.history(OTHER, first.item_id),
        store.retire(OTHER, first.item_id, LATER),
        store.revise(OTHER, first.item_id, STYLE, "owner", LATER),
    ):
        with pytest.raises(FakeYouError, match="does not exist") as raised:
            await attempt
        assert raised.value.code is ErrorCode.OWNER_MODEL_ITEM_UNKNOWN
    with pytest.raises(FakeYouError, match="not awaiting a decision") as raised:
        await store.decide(OTHER, proposed.revision_id, approve=True, at=LATER)
    assert raised.value.code is ErrorCode.OWNER_MODEL_NOT_PROPOSED


async def test_concurrent_approvals_get_distinct_versions(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)

    added = await asyncio.gather(
        *(
            store.add(SCOPE, OwnerModelEntry("fact", f"topic-{index}", "A fact."), "owner", NOW)
            for index in range(8)
        )
    )

    assert sorted(revision.model_version or 0 for revision in added) == list(range(1, 9))
    assert (await store.snapshot(SCOPE)).version == 8
