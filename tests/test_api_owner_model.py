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
    retired = await client.delete(f"{BASE}/items/{item_id}")
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
    retire = await client.delete(f"{BASE}/items/{foreign.item_id}")

    assert model.json() == {"twin_id": "demo_twin", "version": 0, "items": []}
    assert (history.status_code, history.json()["error"]["code"]) == (
        404,
        "owner_model.item_unknown",
    )
    assert retire.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {**POSITION, "kind": "opinion"},
        {**POSITION, "statement": "   "},
        {**POSITION, "disclosure": "public"},
        {**POSITION, "source": "debrief"},
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
