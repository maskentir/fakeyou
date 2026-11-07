import json
from datetime import UTC, datetime

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from fakeyou.api.app import create_app
from fakeyou.api.owner_model import owner_model
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli import owner_model as cli
from fakeyou.cli.client import ApiFailure, ClientSettings
from fakeyou.cli.owner_model import OwnerModelItem, OwnerModelTarget
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.owner_model import OwnerModelEntry, OwnerModelStore, TwinScope

SETTINGS = ClientSettings(base_url="http://api.test", owner_token="owner-secret", admin_token=None)
ITEM = OwnerModelItem("decision_rule", "hiring", "Hire for ownership.", "firm", "owner_only")
REVISED = OwnerModelItem("decision_rule", "hiring", "Hire slow.", "leaning", "owner_only")


@pytest.fixture
def store(migrated_async_engine: AsyncEngine) -> OwnerModelStore:
    return OwnerModelStore(migrated_async_engine)


@pytest.fixture
def target(settings: Settings, store: OwnerModelStore) -> OwnerModelTarget:
    settings.api.owner_token = SecretStr("owner-secret")
    settings.identity.owner_id = SessionId("demo_owner")
    app = create_app(settings)
    app.dependency_overrides[owner_model] = lambda: store
    return OwnerModelTarget(SETTINGS, "demo_twin", httpx2.ASGITransport(app=app))


def _printed(capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(capsys.readouterr().out)


async def test_the_cli_edits_the_owner_model_through_the_api(
    target: OwnerModelTarget, capsys: pytest.CaptureFixture[str]
) -> None:
    await cli.add_owner_model_item(target, ITEM)
    added = _printed(capsys)
    assert isinstance(added, dict)
    await cli.revise_owner_model_item(
        target,
        added["item_id"],
        OwnerModelItem("decision_rule", "hiring", "Hire slow.", "leaning", "owner_only"),
    )
    capsys.readouterr()
    await cli.show_owner_model(target, version=1)
    earlier = _printed(capsys)
    await cli.retire_owner_model_item(target, added["item_id"])
    capsys.readouterr()
    await cli.show_owner_model_history(target, added["item_id"])
    history = _printed(capsys)

    assert (added["kind"], added["disclosure"], added["status"]) == (
        "decision_rule",
        "owner_only",
        "approved",
    )
    assert isinstance(earlier, dict)
    assert [item["statement"] for item in earlier["items"]] == ["Hire for ownership."]
    assert isinstance(history, list)
    assert [entry["status"] for entry in history] == ["approved", "approved", "retired"]


async def test_the_cli_decides_proposals(
    target: OwnerModelTarget, store: OwnerModelStore, capsys: pytest.CaptureFixture[str]
) -> None:
    proposed = await store.add(
        TwinScope("demo_owner", "demo_twin"),
        OwnerModelEntry("preference", "meetings", "Prefers async updates."),
        "debrief",
        datetime(2026, 11, 8, tzinfo=UTC),
    )

    await cli.show_owner_model_proposals(target)
    listed = _printed(capsys)
    await cli.decide_owner_model_proposal(target, proposed.revision_id, approve=False)
    rejected = _printed(capsys)

    assert isinstance(listed, list)
    assert [entry["revision_id"] for entry in listed] == [proposed.revision_id]
    assert isinstance(rejected, dict)
    assert rejected["status"] == "rejected"


async def test_a_conflict_reaches_the_cli_as_a_catalog_failure(target: OwnerModelTarget) -> None:
    with pytest.raises(ApiFailure) as raised:
        await cli.decide_owner_model_proposal(target, "missing", approve=True)

    assert raised.value.code == "owner_model.not_proposed"
    assert raised.value.exit_code is ExitCode.INPUT_REJECTED
