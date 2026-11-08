import json
import sys
from datetime import UTC, datetime

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

import fakeyou.cli.main as cli_main
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


async def test_the_cli_forgets_an_item_and_sends_valid_at(
    target: OwnerModelTarget, capsys: pytest.CaptureFixture[str]
) -> None:
    since = datetime(2026, 3, 1, 8, tzinfo=UTC)
    fact = OwnerModelItem(
        "fact", "vendor", "Orchid hosts us.", "firm", "room_speakable", valid_at=since
    )
    await cli.add_owner_model_item(target, fact)
    added = _printed(capsys)
    assert isinstance(added, dict)
    await cli.forget_owner_model_item(target, added["item_id"])
    forgotten = _printed(capsys)
    await cli.show_owner_model_history(target, added["item_id"])
    history = _printed(capsys)

    assert datetime.fromisoformat(added["valid_at"]) == since
    assert forgotten == {"item_id": added["item_id"], "forgotten": True}
    assert isinstance(history, list)
    assert [(entry["status"], entry["statement"]) for entry in history] == [("forgotten", "")]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["add", "fact", "vendor", "Orchid hosts us.", "--twin-id", "t"], None),
        (
            [
                "add",
                "fact",
                "vendor",
                "Orchid hosts us.",
                "--twin-id",
                "t",
                "--valid-at",
                "2026-03-01T08:00:00+07:00",
            ],
            datetime(2026, 3, 1, 1, tzinfo=UTC),
        ),
        (
            [
                "revise",
                "item-1",
                "fact",
                "vendor",
                "Orchid hosts us.",
                "--twin-id",
                "t",
                "--valid-at",
                "2026-03-01T01:00:00Z",
            ],
            datetime(2026, 3, 1, 1, tzinfo=UTC),
        ),
    ],
)
def test_valid_at_is_parsed_as_an_aware_datetime(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: datetime | None
) -> None:
    captured: list[OwnerModelItem] = []

    async def record(_target: OwnerModelTarget, *args: object) -> None:
        item = args[-1]
        assert isinstance(item, OwnerModelItem)
        captured.append(item)

    monkeypatch.setattr(cli_main, f"{argv[0]}_owner_model_item", record)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "owner-model", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [item] = captured
    assert item.valid_at == expected


@pytest.mark.parametrize("value", ["2026-03-01T08:00:00", "yesterday"])
def test_valid_at_without_a_timezone_is_refused_before_the_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    argv = ["owner-model", "add", "fact", "v", "s", "--twin-id", "t", "--valid-at", value]
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 2
    assert "valid_at must be an ISO 8601 datetime with a timezone" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "target_name"),
    [("retire", "retire_owner_model_item"), ("forget", "forget_owner_model_item")],
)
def test_retire_and_forget_commands_reach_their_own_routes(
    monkeypatch: pytest.MonkeyPatch, command: str, target_name: str
) -> None:
    called: list[str] = []

    async def record(_target: OwnerModelTarget, item_id: str) -> None:
        called.append(item_id)

    monkeypatch.setattr(cli_main, target_name, record)
    argv = ["fakeyou", "owner-model", command, "item-1", "--twin-id", "t"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert called == ["item-1"]
