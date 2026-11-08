import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.owner_model import (
    OwnerModelEntry,
    OwnerModelSnapshot,
    OwnerModelStore,
    TwinScope,
)

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
SCOPE = TwinScope("owner-1", "twin-1")
OTHER = TwinScope("owner-1", "twin-2")
POSITION = OwnerModelEntry("position", "release", "Ship the beta after the load test passes.")
STYLE = OwnerModelEntry("style_example", "tone", "Short answers, numbers first.")
IMPORTED = (
    OwnerModelEntry("fact", "imported fact", "The beta launches on 14 October."),
    OwnerModelEntry("preference", "imported preference", "Keep the weekly sync short."),
)


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


async def test_valid_at_is_kept_on_the_revision_and_in_the_snapshot(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    since = datetime(2026, 3, 1, tzinfo=timezone(timedelta(hours=7)))
    fact = OwnerModelEntry("fact", "vendor", "We buy cloud from Orchid.", valid_at=since)

    added = await store.add(SCOPE, fact, "owner", NOW)

    assert added.entry.valid_at == since
    [item] = (await store.snapshot(SCOPE)).items
    assert item.entry.valid_at == since


@pytest.mark.parametrize(
    ("valid_at", "code"),
    [
        (datetime(2026, 3, 1), ErrorCode.OWNER_MODEL_VALID_AT_INVALID),
        (NOW + timedelta(seconds=1), ErrorCode.OWNER_MODEL_VALID_AT_FUTURE),
    ],
)
async def test_valid_at_must_be_aware_and_not_after_the_write(
    migrated_async_engine: AsyncEngine, valid_at: datetime, code: ErrorCode
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    fact = OwnerModelEntry("fact", "vendor", "We buy cloud from Orchid.", valid_at=valid_at)

    for attempt in (
        store.add(SCOPE, fact, "owner", NOW),
        store.revise(SCOPE, first.item_id, fact, "owner", NOW),
    ):
        with pytest.raises(FakeYouError, match="valid_at") as raised:
            await attempt
        assert raised.value.code is code
    assert (await store.snapshot(SCOPE)).version == 1


async def test_forget_erases_every_revision_and_leaves_one_tombstone(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    revision = OwnerModelEntry("position", "release", "Ship the beta next week.", reason="Ready.")
    await store.revise(SCOPE, first.item_id, revision, "owner", NOW)
    await store.revise(SCOPE, first.item_id, STYLE, "debrief", NOW)
    kept = await store.add(SCOPE, STYLE, "owner", NOW)

    tombstone = await store.forget(SCOPE, first.item_id, LATER)

    assert (tombstone.item_id, tombstone.status, tombstone.source) == (
        first.item_id,
        "forgotten",
        "owner",
    )
    assert tombstone.entry == OwnerModelEntry("style_example", "", "")
    assert (tombstone.model_version, tombstone.decided_at) == (4, LATER)
    assert await store.history(SCOPE, first.item_id) == [tombstone]
    assert await store.proposals(SCOPE) == []
    for version in (None, 1, 2, 3, 4):
        snapshot = await store.snapshot(SCOPE, version)
        assert all(item.item_id != first.item_id for item in snapshot.items)
    assert [item.item_id for item in (await store.snapshot(SCOPE)).items] == [kept.item_id]
    added = await store.add(SCOPE, POSITION, "owner", LATER)
    assert added.model_version == 5


async def test_forgetting_twice_returns_the_same_tombstone(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    await store.retire(SCOPE, first.item_id, NOW)

    tombstone = await store.forget(SCOPE, first.item_id, LATER)

    assert await store.forget(SCOPE, first.item_id, LATER + timedelta(minutes=1)) == tombstone
    assert (await store.snapshot(SCOPE)).version == tombstone.model_version == 3


async def test_a_forgotten_item_cannot_be_revised_or_retired(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)
    proposed = await store.revise(SCOPE, first.item_id, STYLE, "debrief", NOW)
    await store.forget(SCOPE, first.item_id, LATER)

    for attempt in (
        store.retire(SCOPE, first.item_id, LATER),
        store.revise(SCOPE, first.item_id, STYLE, "owner", LATER),
        store.revise(SCOPE, first.item_id, STYLE, "debrief", LATER),
    ):
        with pytest.raises(FakeYouError, match="was forgotten") as raised:
            await attempt
        assert raised.value.code is ErrorCode.OWNER_MODEL_ITEM_FORGOTTEN
    with pytest.raises(FakeYouError, match="not awaiting a decision") as raised:
        await store.decide(SCOPE, proposed.revision_id, approve=True, at=LATER)
    assert raised.value.code is ErrorCode.OWNER_MODEL_NOT_PROPOSED


async def test_forget_of_an_unknown_or_foreign_item_is_unknown(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    first = await store.add(SCOPE, POSITION, "owner", NOW)

    for scope, item_id in ((SCOPE, "missing"), (OTHER, first.item_id)):
        with pytest.raises(FakeYouError, match="does not exist") as raised:
            await store.forget(scope, item_id, LATER)
        assert raised.value.code is ErrorCode.OWNER_MODEL_ITEM_UNKNOWN
    assert [item.entry for item in (await store.snapshot(SCOPE)).items] == [POSITION]


async def test_an_import_approves_every_statement_under_its_own_version(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    await store.add(SCOPE, POSITION, "owner", NOW)

    imported = await store.import_entries(SCOPE, IMPORTED, LATER)

    assert [
        (revision.entry, revision.source, revision.status, revision.model_version)
        for revision in imported.added
    ] == [(IMPORTED[0], "import", "approved", 2), (IMPORTED[1], "import", "approved", 3)]
    assert {revision.decided_at for revision in imported.added} == {LATER}
    assert imported.skipped == ()
    assert [item.entry for item in (await store.snapshot(SCOPE)).items] == [POSITION, *IMPORTED]


async def test_an_import_skips_statements_already_held_with_the_same_kind(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    await store.import_entries(SCOPE, IMPORTED, NOW)
    respaced = OwnerModelEntry("fact", "imported fact", "The beta  launches on\n14 October.")
    as_preference = OwnerModelEntry("preference", "imported preference", IMPORTED[0].statement)

    again = await store.import_entries(
        SCOPE, (*IMPORTED, respaced, as_preference, as_preference), LATER
    )

    assert [revision.entry for revision in again.added] == [as_preference]
    assert again.skipped == (*IMPORTED, respaced, as_preference)
    assert (await store.snapshot(SCOPE)).version == 3


async def test_a_retired_statement_is_imported_again(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    [fact, _preference] = (await store.import_entries(SCOPE, IMPORTED, NOW)).added
    await store.retire(SCOPE, fact.item_id, NOW)

    again = await store.import_entries(SCOPE, IMPORTED, LATER)

    assert [revision.entry for revision in again.added] == [IMPORTED[0]]
    assert again.added[0].item_id != fact.item_id
    assert again.skipped == (IMPORTED[1],)


async def test_an_import_that_fails_midway_writes_nothing(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)
    unstorable = OwnerModelEntry("fact", "imported fact", "A NUL \x00 byte.")

    with pytest.raises(DBAPIError):
        await store.import_entries(SCOPE, (IMPORTED[0], unstorable), NOW)

    assert await store.snapshot(SCOPE) == OwnerModelSnapshot(version=0, items=())


async def test_concurrent_imports_of_one_persona_add_it_once(
    migrated_async_engine: AsyncEngine,
) -> None:
    store = OwnerModelStore(migrated_async_engine)

    results = await asyncio.gather(*(store.import_entries(SCOPE, IMPORTED, NOW) for _ in range(4)))

    assert sorted(len(result.added) for result in results) == [0, 0, 0, 2]
    assert sorted(len(result.skipped) for result in results) == [0, 2, 2, 2]
    assert (await store.snapshot(SCOPE)).version == 2
