import dataclasses
import json
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import fakeyou.debrief.lineage as lineage_module
from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import PhaseUpdate
from fakeyou.debrief.debrief import read_debrief
from fakeyou.debrief.lineage import (
    DecisionLineage,
    lineage_path,
    lineage_revision,
    publish_lineage,
)
from fakeyou.debrief.register import MeetingScope, records_from_report
from fakeyou.persistence.ingestion_status import record_lock
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.lease import LockPurpose
from tests.test_api_memory import MemoryHarness
from tests.test_api_sessions import SESSION_PAYLOAD, STATUS_AT
from tests.test_decisions import FIRST, MEETINGS, OWNER_APPROVAL_CLAIM, SECOND, write_debrief

OWNER, TWIN = "demo_owner", "demo_twin"
EARLY = "2026-10-03T12:00:00+00:00"
LATE = "2026-10-10T12:00:00+00:00"
LATEST = "2026-10-17T12:00:00+00:00"
THIRD = "arch-review-3"
POSTGRES = "The billing service moves to Postgres 17 this quarter."
FREEZE = "Freeze schema changes until the migration is done."
ROLLBACK = "Who owns the rollback plan?"
MYSQL = "The billing service stays on MySQL for now."
DIMAS = "Dimas writes the rollback plan."
MANAGED = "Decide whether to pay for the managed database."

Ids = dict[tuple[str, str], str]


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[MemoryHarness]:
    harness = MemoryHarness(settings)
    try:
        yield harness
    finally:
        await harness.aclose()


def seed_snapshot(
    api: MemoryHarness, meeting_id: str, at: str, *, owner_id: str = OWNER, twin_id: str = TWIN
) -> None:
    create = SessionCreate.model_validate(
        {
            "link": "https://meet.google.com/abc-defg-hij",
            "meeting_id": meeting_id,
            "owner_id": owner_id,
            "twin_id": twin_id,
            "audio": True,
            "conversation": True,
            "persona": {**SESSION_PAYLOAD["persona"], "owner_id": owner_id, "twin_id": twin_id},
        }
    )
    snapshot = build_snapshot(create, meeting_id=meeting_id, at=at)
    api.status.seed(snapshot.model_dump(mode="json"), STATUS_AT)


def seed(
    api: MemoryHarness,
    meeting_id: str,
    at: str,
    *,
    owner_id: str = OWNER,
    twin_id: str = TWIN,
    **changes: Any,
) -> Path:
    seed_snapshot(api, meeting_id, at, owner_id=owner_id, twin_id=twin_id)
    return write_debrief(api, meeting_id, owner_id=owner_id, twin_id=twin_id, **changes)


async def record_ids(api: MemoryHarness) -> Ids:
    response = await api.get("/v1/decisions?limit=200")
    assert response.status_code == 200, response.text
    return {
        (record["meeting_id"], record["text"]): record["record_id"]
        for record in response.json()["records"]
    }


async def register_view(api: MemoryHarness) -> dict[str, Any]:
    response = await api.get("/v1/decisions?limit=200")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def seed_two(api: MemoryHarness) -> Ids:
    seed(api, FIRST, EARLY)
    seed(api, SECOND, LATE)
    return await record_ids(api)


def curated(
    meeting_id: str,
    *pairs: tuple[str, str, str],
    owner_id: str = OWNER,
    twin_id: str = TWIN,
) -> dict[str, Any]:
    return {
        "schema_version": "decision_lineage/v1",
        "owner_id": owner_id,
        "twin_id": twin_id,
        "meeting_id": meeting_id,
        "method": "manual_curated",
        "comparisons": [
            {"earlier_record_id": earlier, "later_record_id": later, "relationship": label}
            for earlier, later, label in pairs
        ],
    }


async def imported(api: MemoryHarness, document: dict[str, Any]) -> dict[str, Any]:
    response = await api.put(f"/v1/decisions/lineage/{document['meeting_id']}", json=document)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def refused(api: MemoryHarness, document: dict[str, Any]) -> str:
    response = await api.put(f"/v1/decisions/lineage/{document['meeting_id']}", json=document)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "input.invalid"
    message: str = error["message"]
    return message


async def lineage(api: MemoryHarness, record_id: str, query: str = "") -> dict[str, Any]:
    response = await api.get(f"/v1/decisions/{record_id}/lineage{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def sidecar(api: MemoryHarness, meeting_id: str) -> Path:
    return MeetingDir(api.settings.paths.record_dir, SessionId(meeting_id)).decision_lineage


async def forget(api: MemoryHarness, meeting_id: str, *, keep_record: bool) -> None:
    await api.status.record_phase(
        meeting_id, PhaseUpdate("ended", STATUS_AT, terminal_reason="stopped_by_owner")
    )
    response = await api.post(
        f"/v1/sessions/{meeting_id}/forget", json={"keep_record": keep_record}
    )
    assert response.status_code == 200, response.text


def accounting(**counts: int) -> dict[str, int]:
    empty = dict.fromkeys(
        (
            "related_discussion",
            "potential_contradiction",
            "potential_supersession",
            "no_established_relationship",
            "unresolved",
            "invalidated",
            "not_compared",
        ),
        0,
    )
    return empty | counts


async def test_curated_contradiction_is_served_with_both_evidence_trails(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    postgres, mysql = ids[FIRST, POSTGRES], ids[SECOND, MYSQL]
    before = await register_view(api)
    views = {record["record_id"]: record for record in before["records"]}

    receipt = await imported(api, curated(SECOND, (postgres, mysql, "potential_contradiction")))
    later = await lineage(api, mysql)
    earlier = await lineage(api, postgres)

    assert receipt == {
        "meeting_id": SECOND,
        "twin_id": TWIN,
        "method": "manual_curated",
        "imported_at": "2026-10-03T12:00:00Z",
        "comparisons": 1,
        "revision": lineage_revision(sidecar(api, SECOND).read_bytes()),
        "outcome": "created",
    }
    assert later["meetings"][1]["lineage_revision"] == receipt["revision"]
    [incoming] = later["comparisons"]
    [outgoing] = earlier["comparisons"]
    assert later["record"] == views[mysql]
    assert incoming["other"] == views[postgres]
    assert outgoing["other"] == views[mysql]
    assert incoming["relation_id"] == outgoing["relation_id"]
    assert (incoming["direction"], outgoing["direction"]) == ("incoming", "outgoing")
    for comparison in (incoming, outgoing):
        assert comparison["relationship"] == "potential_contradiction"
        assert (comparison["earlier_record_id"], comparison["later_record_id"]) == (
            postgres,
            mysql,
        )
        assert comparison["authority"] == "derived_candidate"
        assert comparison["review_status"] == "unreviewed"
        assert comparison["owner_approved"] is False
        assert comparison["method"] == "manual_curated"
        assert comparison["other"]["authority"] == "meeting_claim"
        assert comparison["other"]["owner_approved"] is False
    assert incoming["other"]["evidence"][0]["quote"] in MEETINGS[FIRST]["transcript"][0]["text"]
    assert await register_view(api) == before


async def test_supersession_never_hides_or_rewrites_the_earlier_claim(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    before = await register_view(api)

    await imported(
        api,
        curated(
            SECOND,
            (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "potential_supersession"),
            (ids[FIRST, FREEZE], ids[SECOND, MYSQL], "related_discussion"),
        ),
    )
    after = await register_view(api)

    assert after == before
    assert POSTGRES in [record["text"] for record in after["records"]]
    assert "superseded" not in json.dumps(await lineage(api, ids[FIRST, POSTGRES]))


async def test_relation_to_an_approval_claim_leaves_the_owner_decision_pending(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)

    await imported(
        api,
        curated(
            SECOND,
            (ids[FIRST, OWNER_APPROVAL_CLAIM], ids[SECOND, MANAGED], "related_discussion"),
        ),
    )
    body = await lineage(api, ids[SECOND, MANAGED])

    assert body["record"]["status"] == "needs_owner"
    assert body["record"]["owner_approved"] is False
    [comparison] = body["comparisons"]
    assert comparison["other"]["status"] == "stated"
    assert comparison["other"]["evidence"][0]["participant_id"] == "budi"
    assert api.owner_model.current.version == 0
    assert api.owner_model.current.items == ()
    assert api.owner_model.scopes == []


@pytest.mark.parametrize(
    "smuggled",
    [
        {"owner_approved": True},
        {"resolution": "MySQL wins"},
        {"rationale": "the owner agreed"},
        {"review_status": "reviewed"},
    ],
)
async def test_import_cannot_carry_approval_resolution_or_review(
    api: MemoryHarness, smuggled: dict[str, Any]
) -> None:
    ids = await seed_two(api)
    document = curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    in_comparison = {**document, "comparisons": [{**document["comparisons"][0], **smuggled}]}

    for attempt in ({**document, **smuggled}, in_comparison):
        response = await api.put(f"/v1/decisions/lineage/{SECOND}", json=attempt)
        assert response.status_code in {400, 422}, response.text
    assert not sidecar(api, SECOND).exists()


@pytest.mark.parametrize(
    "tamper",
    [
        lambda stored: stored | {"owner_approved": True},
        lambda stored: stored | {"owner_id": "rival_owner"},
        lambda stored: stored | {"comparisons": stored["comparisons"] * 2},
        lambda stored: {
            **stored,
            "comparisons": [{**stored["comparisons"][0], "relationship": "verified_decision"}],
        },
    ],
)
async def test_tampered_sidecar_serves_nothing_and_says_so(
    api: MemoryHarness, tamper: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion")))
    path = sidecar(api, SECOND)
    path.write_text(json.dumps(tamper(json.loads(path.read_text()))), encoding="utf-8")

    body = await lineage(api, mysql)

    assert body["comparisons"] == []
    assert body["accounting"] == accounting(not_compared=4)
    assert {item["meeting_id"]: item["lineage"] for item in body["meetings"]} == {
        FIRST: "missing",
        SECOND: "invalid",
    }


async def test_unreadable_sidecar_is_invalid(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    sidecar(api, SECOND).write_bytes(b"\x00not json")

    body = await lineage(api, ids[SECOND, MYSQL])

    assert body["meetings"][1]["lineage"] == "invalid"
    assert body["comparisons"] == []


async def test_omitted_pairs_are_not_compared_and_explicit_none_is_counted(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]

    await imported(
        api,
        curated(
            SECOND,
            (ids[FIRST, POSTGRES], mysql, "potential_contradiction"),
            (ids[FIRST, ROLLBACK], mysql, "no_established_relationship"),
            (ids[FIRST, ROLLBACK], ids[SECOND, DIMAS], "related_discussion"),
        ),
    )
    body = await lineage(api, mysql)

    assert body["accounting"] == accounting(
        potential_contradiction=1, no_established_relationship=1, not_compared=2
    )
    assert [item["relationship"] for item in body["comparisons"]] == [
        "potential_contradiction",
        "no_established_relationship",
    ]
    assert (await lineage(api, ids[FIRST, FREEZE]))["accounting"] == accounting(not_compared=3)


async def test_another_owners_record_is_indistinguishable_from_an_unknown_one(
    api: MemoryHarness,
) -> None:
    await seed_two(api)
    record_path = seed(api, "rival-review", EARLY, owner_id="rival_owner", fixture=FIRST)
    scope = MeetingScope("rival_owner", TWIN, "rival-review", EARLY)
    rival = records_from_report(scope, read_debrief(record_path))[0].record_id

    foreign = await api.get(f"/v1/decisions/{rival}/lineage")
    unknown = await api.get(f"/v1/decisions/{'0' * 32}/lineage")
    malformed = await api.get("/v1/decisions/not-a-record/lineage")

    assert foreign.status_code == unknown.status_code == malformed.status_code == 404
    assert foreign.json() == unknown.json() == malformed.json()
    assert "rival" not in foreign.text


async def test_imports_never_reach_another_owners_records_or_meetings(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    record_path = seed(api, "rival-review", EARLY, owner_id="rival_owner", fixture=FIRST)
    scope = MeetingScope("rival_owner", TWIN, "rival-review", EARLY)
    rival = records_from_report(scope, read_debrief(record_path))[0].record_id
    mysql = ids[SECOND, MYSQL]

    assert "lineage_record_unknown" in await refused(
        api, curated(SECOND, (rival, mysql, "potential_contradiction"))
    )
    assert "lineage_scope_mismatch" in await refused(
        api,
        curated(
            SECOND,
            (ids[FIRST, POSTGRES], mysql, "potential_contradiction"),
            owner_id="rival_owner",
        ),
    )
    hidden = await api.put(
        "/v1/decisions/lineage/rival-review",
        json=curated("rival-review", (ids[FIRST, POSTGRES], rival, "related_discussion")),
    )
    assert hidden.status_code == 404
    assert not lineage_path(record_path).exists()
    assert not sidecar(api, SECOND).exists()


async def test_forgetting_the_earlier_meeting_leaves_an_unresolved_count(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    postgres, mysql = ids[FIRST, POSTGRES], ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (postgres, mysql, "potential_contradiction")))

    await forget(api, FIRST, keep_record=False)
    body = await lineage(api, mysql)

    assert body["comparisons"] == []
    assert body["accounting"] == accounting(unresolved=1)
    assert POSTGRES not in json.dumps(body)
    assert (await api.get(f"/v1/decisions/{postgres}/lineage")).status_code == 404


async def test_forgetting_the_later_meeting_removes_its_lineage(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    postgres = ids[FIRST, POSTGRES]
    await imported(api, curated(SECOND, (postgres, ids[SECOND, MYSQL], "related_discussion")))

    await forget(api, SECOND, keep_record=False)
    body = await lineage(api, postgres)

    assert not sidecar(api, SECOND).exists()
    assert body["comparisons"] == []
    assert body["accounting"] == accounting()
    assert MYSQL not in json.dumps(body)


async def test_forget_keeping_the_records_keeps_the_lineage(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion")))
    before = await lineage(api, mysql)

    await forget(api, FIRST, keep_record=True)
    await forget(api, SECOND, keep_record=True)

    assert await lineage(api, mysql) == before


async def test_changed_item_is_never_repointed_to_its_replacement(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_contradiction")))
    items = [dict(item) for item in MEETINGS[FIRST]["items"]]
    items[0]["text"] = "The billing service moves to Postgres 17 next quarter."

    write_debrief(api, FIRST, items=items)
    body = await lineage(api, mysql)

    assert body["comparisons"] == []
    assert body["accounting"] == accounting(unresolved=1, not_compared=4)


async def test_identical_regeneration_keeps_the_relation(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_contradiction")))
    before = await lineage(api, mysql)

    write_debrief(api, FIRST)
    write_debrief(api, SECOND)

    assert await lineage(api, mysql) == before


async def test_changed_source_metadata_with_the_same_item_text_invalidates(
    api: MemoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await seed_two(api)
    postgres, mysql = ids[FIRST, POSTGRES], ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (postgres, mysql, "potential_contradiction")))
    relabelled = json.loads(json.dumps(MEETINGS[FIRST]))
    relabelled["transcript"][0]["participant_id"] = "budi"
    monkeypatch.setitem(MEETINGS, "arch-review-1-relabelled", relabelled)

    write_debrief(api, FIRST, fixture="arch-review-1-relabelled")
    body = await lineage(api, mysql)

    assert (await record_ids(api))[FIRST, POSTGRES] == postgres
    assert body["comparisons"] == []
    assert body["accounting"] == accounting(invalidated=1, not_compared=3)


async def test_stale_later_debrief_serves_no_relation(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    postgres = ids[FIRST, POSTGRES]
    record = MeetingDir(api.settings.paths.record_dir, SessionId(SECOND)).record
    await imported(api, curated(SECOND, (postgres, ids[SECOND, MYSQL], "related_discussion")))

    with record.open("a", encoding="utf-8") as handle:
        handle.write(record.read_text(encoding="utf-8").splitlines()[0] + "\n")
    body = await lineage(api, postgres)

    assert body["comparisons"] == []
    assert body["accounting"] == accounting(unresolved=1)
    assert {item["meeting_id"]: item["debrief"] for item in body["meetings"]} == {
        FIRST: "available",
        SECOND: "stale",
    }


async def test_unattributed_evidence_stays_unattributed(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    await imported(
        api, curated(SECOND, (ids[FIRST, FREEZE], ids[SECOND, MANAGED], "related_discussion"))
    )

    body = await lineage(api, ids[SECOND, MANAGED])

    assert body["record"]["evidence"][0]["attribution"] == "unattributed"
    assert body["comparisons"][0]["other"]["evidence"][0]["attribution"] == "unattributed"


async def test_equal_meeting_timestamps_cannot_order_a_pair(api: MemoryHarness) -> None:
    seed(api, FIRST, EARLY)
    seed(api, SECOND, EARLY)
    ids = await record_ids(api)

    for label in ("potential_supersession", "potential_contradiction"):
        assert "lineage_order_unverified" in await refused(
            api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], label))
        )


async def test_timestamp_without_an_offset_cannot_order_a_pair(api: MemoryHarness) -> None:
    seed(api, FIRST, "2026-10-03T12:00:00")
    seed(api, SECOND, LATE)
    ids = await record_ids(api)

    assert "lineage_order_unverified" in await refused(
        api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    )


async def test_meeting_ids_never_decide_the_order(api: MemoryHarness) -> None:
    seed(api, FIRST, LATE)
    seed(api, SECOND, EARLY)
    ids = await record_ids(api)

    assert "lineage_later_record_outside_meeting" in await refused(
        api, curated(SECOND, (ids[SECOND, MYSQL], ids[FIRST, POSTGRES], "potential_supersession"))
    )
    assert "lineage_order_unverified" in await refused(
        api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "potential_supersession"))
    )
    receipt = await imported(
        api, curated(FIRST, (ids[SECOND, MYSQL], ids[FIRST, POSTGRES], "potential_supersession"))
    )
    assert receipt["comparisons"] == 1


@pytest.mark.parametrize(
    ("pairs", "reason"),
    [
        ([(SECOND, MYSQL, SECOND, MYSQL, "related_discussion")], "lineage_self_pair"),
        ([(SECOND, DIMAS, SECOND, MYSQL, "related_discussion")], "lineage_same_meeting"),
        (
            [
                (FIRST, POSTGRES, SECOND, MYSQL, "related_discussion"),
                (FIRST, POSTGRES, SECOND, MYSQL, "related_discussion"),
            ],
            "lineage_duplicate_pair",
        ),
        (
            [
                (FIRST, POSTGRES, SECOND, MYSQL, "related_discussion"),
                (FIRST, POSTGRES, SECOND, MYSQL, "potential_contradiction"),
            ],
            "lineage_conflicting_labels",
        ),
    ],
)
async def test_invalid_pairs_are_refused_without_writing(
    api: MemoryHarness, pairs: list[tuple[str, str, str, str, str]], reason: str
) -> None:
    ids = await seed_two(api)
    document = curated(
        SECOND,
        *[(ids[em, et], ids[lm, lt], label) for em, et, lm, lt, label in pairs],
    )

    assert reason in await refused(api, document)
    assert not sidecar(api, SECOND).exists()


async def test_unknown_record_ids_are_refused(api: MemoryHarness) -> None:
    ids = await seed_two(api)

    assert "lineage_record_unknown" in await refused(
        api, curated(SECOND, ("f" * 32, ids[SECOND, MYSQL], "related_discussion"))
    )


async def test_refused_import_keeps_the_previous_lineage(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion")))
    stored = sidecar(api, SECOND).read_bytes()

    await refused(api, curated(SECOND, ("f" * 32, mysql, "potential_contradiction")))

    assert sidecar(api, SECOND).read_bytes() == stored


async def test_reimport_with_the_current_revision_replaces_the_lineage(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    first = await imported(
        api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion"))
    )

    second = await imported(
        api,
        curated(SECOND, (ids[FIRST, FREEZE], mysql, "potential_supersession"))
        | {"replaces": first["revision"]},
    )
    body = await lineage(api, mysql)

    assert second["outcome"] == "replaced"
    assert second["revision"] != first["revision"]
    assert second["revision"] == lineage_revision(sidecar(api, SECOND).read_bytes())
    assert body["meetings"][1]["lineage_revision"] == second["revision"]
    assert [item["other"]["text"] for item in body["comparisons"]] == [FREEZE]


async def test_records_of_another_twin_are_never_compared(api: MemoryHarness) -> None:
    seed(api, FIRST, EARLY, twin_id="other_twin")
    seed(api, SECOND, LATE)
    ids = await record_ids(api)

    assert "lineage_cross_twin" in await refused(
        api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    )
    assert "lineage_scope_mismatch" in await refused(
        api,
        curated(
            SECOND,
            (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"),
            twin_id="other_twin",
        ),
    )
    body = await lineage(api, ids[SECOND, MYSQL])
    assert [item["meeting_id"] for item in body["meetings"]] == [SECOND]
    assert body["accounting"] == accounting()


async def test_later_meeting_without_a_usable_debrief_cannot_import(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    MeetingDir(api.settings.paths.record_dir, SessionId(SECOND)).debrief_json.unlink()

    assert "lineage_source_unavailable" in await refused(
        api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    )


async def test_document_must_name_the_meeting_it_is_published_to(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    document = curated(FIRST, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))

    response = await api.put(f"/v1/decisions/lineage/{SECOND}", json=document)

    assert response.status_code == 400
    assert "lineage_scope_mismatch" in response.json()["error"]["message"]


async def test_window_counts_the_most_recent_meetings_then_records(
    api: MemoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(api, FIRST, EARLY)
    seed(api, "arch-review-1b", LATE, fixture=FIRST)
    seed(api, SECOND, LATEST)
    ids = await record_ids(api)
    mysql = ids[SECOND, MYSQL]
    monkeypatch.setattr(lineage_module, "WINDOW_MEETINGS", 1)

    assert "lineage_outside_window" in await refused(
        api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion"))
    )
    monkeypatch.setattr(lineage_module, "WINDOW_MEETINGS", 2)
    monkeypatch.setattr(lineage_module, "WINDOW_RECORDS", 5)
    assert "lineage_outside_window" in await refused(
        api, curated(SECOND, (ids[FIRST, FREEZE], mysql, "related_discussion"))
    )
    receipt = await imported(
        api,
        curated(
            SECOND,
            (ids["arch-review-1b", ROLLBACK], mysql, "related_discussion"),
            (ids[FIRST, POSTGRES], mysql, "potential_contradiction"),
        ),
    )
    assert receipt["comparisons"] == 2


async def test_incoming_and_outgoing_relations_page_in_meeting_order(
    api: MemoryHarness,
) -> None:
    seed(api, FIRST, EARLY)
    seed(api, SECOND, LATE)
    seed(api, THIRD, LATEST, fixture=SECOND)
    ids = await record_ids(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_contradiction")))
    await imported(api, curated(THIRD, (mysql, ids[THIRD, MYSQL], "related_discussion")))

    first = await lineage(api, mysql, "?limit=1")
    rest = await lineage(api, mysql, f"?limit=1&cursor={first['next_cursor']}")

    assert [item["direction"] for item in [*first["comparisons"], *rest["comparisons"]]] == [
        "incoming",
        "outgoing",
    ]
    assert rest["comparisons"][0]["other"]["meeting_id"] == THIRD
    assert rest["next_cursor"] is None
    assert first["accounting"] == accounting(
        potential_contradiction=1, related_discussion=1, not_compared=5
    )


async def test_lineage_cursor_is_bound_to_its_record_and_result_set(api: MemoryHarness) -> None:
    seed(api, FIRST, EARLY)
    seed(api, SECOND, LATE)
    seed(api, THIRD, LATEST, fixture=SECOND)
    ids = await record_ids(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_contradiction")))
    third = await imported(api, curated(THIRD, (mysql, ids[THIRD, MYSQL], "related_discussion")))
    cursor = (await lineage(api, mysql, "?limit=1"))["next_cursor"]

    other = await api.get(f"/v1/decisions/{ids[FIRST, POSTGRES]}/lineage?cursor={cursor}")
    await imported(
        api,
        curated(THIRD, (mysql, ids[THIRD, DIMAS], "related_discussion"))
        | {"replaces": third["revision"]},
    )
    stale = await api.get(f"/v1/decisions/{mysql}/lineage?cursor={cursor}")

    assert other.status_code == stale.status_code == 400
    assert "cursor_query_mismatch" in other.json()["error"]["message"]
    assert "cursor_stale" in stale.json()["error"]["message"]


async def test_lineage_requires_the_owner_token(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    document = curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion"))

    for principal, status in ((None, 401), ("admin", 403)):
        read = await api.get(f"/v1/decisions/{mysql}/lineage", principal=principal)
        write = await api.put(f"/v1/decisions/lineage/{SECOND}", json=document, principal=principal)
        assert (read.status_code, write.status_code) == (status, status)
    assert not sidecar(api, SECOND).exists()


async def test_lineage_is_read_back_after_a_restart(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_contradiction")))
    before = await lineage(api, mysql)

    restarted = MemoryHarness(api.settings)
    try:
        seed_snapshot(restarted, FIRST, EARLY)
        seed_snapshot(restarted, SECOND, LATE)
        assert await lineage(restarted, mysql) == before
    finally:
        await restarted.aclose()


async def test_failed_write_keeps_the_published_lineage(
    api: MemoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await seed_two(api)
    await imported(
        api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    )
    path = sidecar(api, SECOND)
    stored = path.read_bytes()
    replacement = DecisionLineage.model_validate_json(stored).model_copy(
        update={"imported_at": datetime(2026, 10, 4, 12, tzinfo=UTC)}
    )
    record = MeetingDir(api.settings.paths.record_dir, SessionId(SECOND)).record

    def refuse_replace(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("fakeyou.persistence.files.os.replace", refuse_replace)
    with pytest.raises(OSError, match="disk full"):
        publish_lineage(record, replacement)

    assert path.read_bytes() == stored
    assert sorted(item.name for item in path.parent.glob("decision-lineage.json*")) == [path.name]


def record_of(api: MemoryHarness, meeting_id: str) -> Path:
    return MeetingDir(api.settings.paths.record_dir, SessionId(meeting_id)).record


async def conflict(api: MemoryHarness, document: dict[str, Any], code: str) -> str:
    response = await api.put(f"/v1/decisions/lineage/{document['meeting_id']}", json=document)
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == code
    message: str = error["message"]
    return message


async def test_import_while_a_forget_holds_the_meeting_is_refused(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    document = curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))

    with record_lock(record_of(api, SECOND), purpose="forget"):
        await conflict(api, document, "decision_lineage.locked")

    assert not sidecar(api, SECOND).exists()
    assert (await imported(api, document))["outcome"] == "created"


async def test_forget_while_an_import_holds_the_meeting_keeps_everything(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    await imported(
        api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    )
    stored = sidecar(api, SECOND).read_bytes()
    await api.status.record_phase(
        SECOND, PhaseUpdate("ended", STATUS_AT, terminal_reason="stopped_by_owner")
    )

    with record_lock(record_of(api, SECOND), purpose="lineage"):
        refused_forget = await api.post(
            f"/v1/sessions/{SECOND}/forget", json={"keep_record": False}
        )

    assert refused_forget.status_code == 409
    assert refused_forget.json()["error"]["code"] == "record.locked"
    assert sidecar(api, SECOND).read_bytes() == stored
    assert record_of(api, SECOND).exists()
    await forget(api, SECOND, keep_record=False)
    assert not record_of(api, SECOND).parent.exists()


async def test_import_never_recreates_a_meeting_forgotten_before_it_held_the_lock(
    api: MemoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await seed_two(api)
    meeting_dir = record_of(api, SECOND).parent
    real_lock = record_lock

    @contextmanager
    def forgotten_first(record_path: Path, *, purpose: LockPurpose) -> Iterator[str]:
        shutil.rmtree(meeting_dir)
        with real_lock(record_path, purpose=purpose) as token:
            yield token

    monkeypatch.setattr(lineage_module, "record_lock", forgotten_first)

    assert "lineage_source_unavailable" in await refused(
        api, curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    )
    assert not meeting_dir.exists()


async def test_import_after_a_completed_forget_finds_no_meeting(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    document = curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
    await forget(api, SECOND, keep_record=False)

    response = await api.put(f"/v1/decisions/lineage/{SECOND}", json=document)

    assert response.status_code == 404
    assert not record_of(api, SECOND).parent.exists()


async def test_a_different_import_never_silently_overwrites(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    first = await imported(
        api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_contradiction"))
    )
    stored = sidecar(api, SECOND).read_bytes()

    message = await conflict(
        api,
        curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion")),
        "decision_lineage.exists",
    )

    assert first["revision"] in message
    assert sidecar(api, SECOND).read_bytes() == stored


async def test_identical_reimport_is_idempotent(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    pairs = (
        (ids[FIRST, POSTGRES], mysql, "potential_contradiction"),
        (ids[FIRST, FREEZE], mysql, "no_established_relationship"),
    )
    first = await imported(api, curated(SECOND, *pairs))
    stored = sidecar(api, SECOND).read_bytes()
    api.app.state.deps = dataclasses.replace(api.app.state.deps, now_iso=lambda: LATEST)

    again = await imported(api, curated(SECOND, *reversed(pairs)))
    stale_hint = await imported(api, curated(SECOND, *pairs) | {"replaces": "0" * 32})

    assert again == stale_hint == first | {"outcome": "unchanged"}
    assert sidecar(api, SECOND).read_bytes() == stored


async def test_concurrent_replacements_from_one_revision_admit_only_the_first(
    api: MemoryHarness,
) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    base = await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion")))
    winner = await imported(
        api,
        curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_contradiction"))
        | {"replaces": base["revision"]},
    )
    stored = sidecar(api, SECOND).read_bytes()

    message = await conflict(
        api,
        curated(SECOND, (ids[FIRST, POSTGRES], mysql, "potential_supersession"))
        | {"replaces": base["revision"]},
        "decision_lineage.revision_stale",
    )

    assert winner["revision"] in message
    assert sidecar(api, SECOND).read_bytes() == stored
    [comparison] = (await lineage(api, mysql))["comparisons"]
    assert comparison["relationship"] == "potential_contradiction"


async def test_replacing_a_lineage_that_does_not_exist_is_stale(api: MemoryHarness) -> None:
    ids = await seed_two(api)

    await conflict(
        api,
        curated(SECOND, (ids[FIRST, POSTGRES], ids[SECOND, MYSQL], "related_discussion"))
        | {"replaces": "a" * 32},
        "decision_lineage.revision_stale",
    )

    assert not sidecar(api, SECOND).exists()


async def test_an_invalid_sidecar_is_replaced_only_by_its_revision(api: MemoryHarness) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    sidecar(api, SECOND).write_bytes(b"\x00not json")
    document = curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion"))
    revision = (await lineage(api, mysql))["meetings"][1]["lineage_revision"]

    await conflict(api, document, "decision_lineage.exists")
    replaced = await imported(api, document | {"replaces": revision})

    assert revision == lineage_revision(b"\x00not json")
    assert replaced["outcome"] == "replaced"


async def test_failed_replacement_keeps_the_sidecar_and_releases_the_lock(
    api: MemoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await seed_two(api)
    mysql = ids[SECOND, MYSQL]
    base = await imported(api, curated(SECOND, (ids[FIRST, POSTGRES], mysql, "related_discussion")))
    stored = sidecar(api, SECOND).read_bytes()
    replacement = curated(SECOND, (ids[FIRST, FREEZE], mysql, "related_discussion")) | {
        "replaces": base["revision"]
    }

    def refuse_replace(*_args: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr("fakeyou.persistence.files.os.replace", refuse_replace)
        with pytest.raises(OSError, match="disk full"):
            await api.put(f"/v1/decisions/lineage/{SECOND}", json=replacement)

    assert sidecar(api, SECOND).read_bytes() == stored
    assert not MeetingDir(api.settings.paths.record_dir, SessionId(SECOND)).lock.exists()
    assert (await imported(api, replacement))["outcome"] == "replaced"
