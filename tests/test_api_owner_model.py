from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from fakeyou.api.app import create_app
from fakeyou.api.owner_model import owner_model
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.owner_model import OwnerModelEntry, OwnerModelStore, TwinScope

OWNER_TOKEN = "owner-secret"
AUTH = {"Authorization": f"Bearer {OWNER_TOKEN}"}
BASE = "/v1/twins/demo_twin/owner-model"
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
POSITION = {"kind": "position", "topic": "release", "statement": "Ship after the load test."}


@pytest.fixture
def store(migrated_async_engine: AsyncEngine) -> OwnerModelStore:
    return OwnerModelStore(migrated_async_engine)


@pytest.fixture
async def client(settings: Settings, store: OwnerModelStore) -> AsyncIterator[httpx2.AsyncClient]:
    settings.api.owner_token = SecretStr(OWNER_TOKEN)
    settings.identity.owner_id = SessionId("demo_owner")
    app = create_app(settings)
    app.dependency_overrides[owner_model] = lambda: store
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver", headers=AUTH
    ) as client:
        yield client


async def test_the_owner_adds_revises_and_retires_an_item(client: httpx2.AsyncClient) -> None:
    added = await client.post(BASE, json={**POSITION, "statement": "  Ship after the load test.  "})
    item_id = added.json()["item_id"]
    revised = await client.post(
        f"{BASE}/items/{item_id}/revisions",
        json={**POSITION, "statement": "Ship once support is staffed.", "confidence": "leaning"},
    )
    current = await client.get(BASE)
    earlier = await client.get(BASE, params={"version": 1})
    retired = await client.post(f"{BASE}/items/{item_id}/retire")
    late = await client.post(f"{BASE}/items/{item_id}/revisions", json=POSITION)
    history = await client.get(f"{BASE}/items/{item_id}")

    assert added.status_code == 201
    assert added.json() | {"revision_id": None, "item_id": None} == {
        "revision_id": None,
        "item_id": None,
        "revision": 1,
        "kind": "position",
        "topic": "release",
        "statement": "Ship after the load test.",
        "reason": None,
        "confidence": "firm",
        "disclosure": "room_speakable",
        "valid_at": None,
        "source": "owner",
        "status": "approved",
        "model_version": 1,
        "created_at": added.json()["created_at"],
        "decided_at": added.json()["created_at"],
    }
    assert (revised.status_code, revised.json()["revision"]) == (201, 2)
    assert current.json()["version"] == 2
    assert [item["statement"] for item in current.json()["items"]] == [
        "Ship once support is staffed."
    ]
    assert [item["statement"] for item in earlier.json()["items"]] == ["Ship after the load test."]
    assert (retired.status_code, retired.json()["status"]) == (200, "retired")
    assert (late.status_code, late.json()["error"]["code"]) == (409, "owner_model.item_retired")
    assert [entry["status"] for entry in history.json()] == ["approved", "approved", "retired"]


async def test_the_owner_approves_or_rejects_proposals(
    client: httpx2.AsyncClient, store: OwnerModelStore
) -> None:
    scope = TwinScope("demo_owner", "demo_twin")
    kept = await store.add(scope, OwnerModelEntry("fact", "team", "Rani leads QA."), "debrief", NOW)
    dropped = await store.add(scope, OwnerModelEntry("fact", "team", "Rani quit."), "debrief", NOW)

    listed = await client.get(f"{BASE}/proposals")
    approved = await client.post(f"{BASE}/proposals/{kept.revision_id}/approve")
    rejected = await client.post(f"{BASE}/proposals/{dropped.revision_id}/reject")
    again = await client.post(f"{BASE}/proposals/{dropped.revision_id}/approve")
    model = await client.get(BASE)

    assert [entry["revision_id"] for entry in listed.json()] == [
        kept.revision_id,
        dropped.revision_id,
    ]
    assert (approved.json()["status"], approved.json()["model_version"]) == ("approved", 1)
    assert rejected.json()["status"] == "rejected"
    assert (again.status_code, again.json()["error"]["code"]) == (409, "owner_model.not_proposed")
    assert [item["statement"] for item in model.json()["items"]] == ["Rani leads QA."]
    assert (await client.get(f"{BASE}/proposals")).json() == []


async def test_another_owners_items_stay_invisible(
    client: httpx2.AsyncClient, store: OwnerModelStore
) -> None:
    foreign = await store.add(
        TwinScope("someone_else", "demo_twin"),
        OwnerModelEntry("fact", "team", "Private."),
        "owner",
        NOW,
    )

    model = await client.get(BASE)
    history = await client.get(f"{BASE}/items/{foreign.item_id}")
    retire = await client.post(f"{BASE}/items/{foreign.item_id}/retire")
    forget = await client.delete(f"{BASE}/items/{foreign.item_id}")

    assert model.json() == {"twin_id": "demo_twin", "version": 0, "items": []}
    assert (history.status_code, history.json()["error"]["code"]) == (
        404,
        "owner_model.item_unknown",
    )
    assert retire.status_code == 404
    assert forget.status_code == 404
    [kept] = await store.history(foreign.scope, foreign.item_id)
    assert kept.entry.statement == "Private."


async def test_forget_erases_the_item_and_answers_without_a_body(
    client: httpx2.AsyncClient,
) -> None:
    added = await client.post(BASE, json=POSITION)
    item_id = added.json()["item_id"]

    forgotten = await client.delete(f"{BASE}/items/{item_id}")
    again = await client.delete(f"{BASE}/items/{item_id}")
    history = await client.get(f"{BASE}/items/{item_id}")
    retire = await client.post(f"{BASE}/items/{item_id}/retire")
    revise = await client.post(f"{BASE}/items/{item_id}/revisions", json=POSITION)
    model = await client.get(BASE)

    assert (forgotten.status_code, forgotten.content) == (204, b"")
    assert again.status_code == 204
    [tombstone] = history.json()
    assert (tombstone["status"], tombstone["topic"], tombstone["statement"]) == (
        "forgotten",
        "",
        "",
    )
    assert "Ship after the load test." not in history.text
    for refused in (retire, revise):
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            409,
            "owner_model.item_forgotten",
        )
    assert model.json() == {"twin_id": "demo_twin", "version": 2, "items": []}


async def test_valid_at_round_trips_and_may_not_be_in_the_future(
    client: httpx2.AsyncClient,
) -> None:
    added = await client.post(BASE, json={**POSITION, "valid_at": "2026-03-01T00:00:00+07:00"})
    future = await client.post(BASE, json={**POSITION, "valid_at": "2999-01-01T00:00:00+00:00"})

    assert added.status_code == 201
    assert datetime.fromisoformat(added.json()["valid_at"]) == datetime(
        2026, 2, 28, 17, 0, tzinfo=UTC
    )
    assert (future.status_code, future.json()["error"]["code"]) == (
        400,
        "owner_model.valid_at_future",
    )


@pytest.mark.parametrize(
    "body",
    [
        {**POSITION, "kind": "opinion"},
        {**POSITION, "statement": "   "},
        {**POSITION, "disclosure": "public"},
        {**POSITION, "source": "debrief"},
        {**POSITION, "valid_at": "2026-03-01T00:00:00"},
        {**POSITION, "valid_at": "not a time"},
    ],
)
async def test_invalid_items_are_rejected(client: httpx2.AsyncClient, body: object) -> None:
    response = await client.post(BASE, json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"


async def test_requests_without_the_owner_token_are_unauthorized(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.get(BASE, headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 401


async def test_an_import_adds_facts_and_preferences_once(client: httpx2.AsyncClient) -> None:
    body = {
        "facts": ["  The beta launches on 14 October.  "],
        "preferences": ["Keep the weekly sync short."],
    }

    first = await client.post(f"{BASE}/imports", json=body)
    second = await client.post(f"{BASE}/imports", json=body)
    current = await client.get(BASE)

    assert first.status_code == 200
    assert [
        (item["kind"], item["topic"], item["statement"], item["source"], item["status"])
        for item in first.json()["added"]
    ] == [
        ("fact", "imported fact", "The beta launches on 14 October.", "import", "approved"),
        ("preference", "imported preference", "Keep the weekly sync short.", "import", "approved"),
    ]
    assert [item["model_version"] for item in first.json()["added"]] == [1, 2]
    assert first.json()["skipped"] == []
    assert second.json() == {
        "added": [],
        "skipped": [
            {"kind": "fact", "statement": "The beta launches on 14 October."},
            {"kind": "preference", "statement": "Keep the weekly sync short."},
        ],
    }
    assert current.json()["version"] == 2


@pytest.mark.parametrize(
    "body",
    [
        {"facts": ["   "]},
        {"facts": "The beta launches on 14 October."},
        {"decision_rules": ["Hire slow."]},
        {"facts": ["x" * 2001]},
    ],
)
async def test_invalid_imports_are_rejected(client: httpx2.AsyncClient, body: object) -> None:
    response = await client.post(f"{BASE}/imports", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"
