import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

import fakeyou.cli.main as cli_main
from fakeyou.cli.client import ApiFailure, ClientSettings
from fakeyou.cli.decisions import import_decision_lineage, show_decision_lineage
from tests.test_api_memory import MemoryHarness
from tests.test_api_sessions import BASE_URL, OWNER_TOKEN
from tests.test_decisions import FIRST, SECOND
from tests.test_lineage import MYSQL, POSTGRES, api, curated, seed_two, sidecar

__all__ = ["api"]

SETTINGS = ClientSettings(base_url=BASE_URL, owner_token=OWNER_TOKEN, admin_token=None)


def printed(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(capsys.readouterr().out)
    return body


async def test_operator_imports_a_curated_relationship_and_reads_it_back(
    api: MemoryHarness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = await seed_two(api)
    postgres, mysql = ids[FIRST, POSTGRES], ids[SECOND, MYSQL]
    document = tmp_path / "lineage.json"
    document.write_text(
        json.dumps(curated(SECOND, (postgres, mysql, "potential_contradiction"))),
        encoding="utf-8",
    )
    transport = httpx2.ASGITransport(app=api.app)

    await import_decision_lineage(SETTINGS, document, transport=transport)
    receipt = printed(capsys)
    await show_decision_lineage(SETTINGS, mysql, cursor=None, transport=transport)
    body = printed(capsys)

    assert receipt["comparisons"] == 1
    [comparison] = body["comparisons"]
    assert comparison["relationship"] == "potential_contradiction"
    assert comparison["other"]["text"] == POSTGRES
    assert comparison["other"]["evidence"][0]["attribution"] == "recorded_label"
    assert comparison["authority"] == "derived_candidate"
    assert comparison["owner_approved"] is False


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"schema_version": "decision_lineage/v1"}),
        json.dumps(
            curated(SECOND, ("a" * 32, "b" * 32, "related_discussion")) | {"owner_approved": True}
        ),
    ],
)
async def test_invalid_document_is_refused_before_any_request(
    api: MemoryHarness, tmp_path: Path, content: str
) -> None:
    document = tmp_path / "lineage.json"
    document.write_text(content, encoding="utf-8")
    unreachable = httpx2.MockTransport(lambda _request: pytest.fail("no request expected"))

    with pytest.raises(ApiFailure, match="decision_lineage/v1 import") as caught:
        await import_decision_lineage(SETTINGS, document, transport=unreachable)

    assert caught.value.code == "input.invalid"
    assert not sidecar(api, SECOND).exists()


async def test_missing_document_is_an_input_error(tmp_path: Path) -> None:
    with pytest.raises(ApiFailure, match="cannot read"):
        await import_decision_lineage(SETTINGS, tmp_path / "absent.json")


async def test_refused_import_surfaces_the_server_reason(
    api: MemoryHarness, tmp_path: Path
) -> None:
    ids = await seed_two(api)
    document = tmp_path / "lineage.json"
    document.write_text(
        json.dumps(curated(SECOND, ("f" * 32, ids[SECOND, MYSQL], "related_discussion"))),
        encoding="utf-8",
    )

    with pytest.raises(ApiFailure, match="lineage_record_unknown"):
        await import_decision_lineage(
            SETTINGS, document, transport=httpx2.ASGITransport(app=api.app)
        )


async def test_commands_are_wired_to_the_decisions_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, object]] = []

    async def record_import(_settings: ClientSettings, path: Path) -> None:
        calls.append(("import", path))

    async def record_show(_settings: ClientSettings, record_id: str, *, cursor: str | None) -> None:
        calls.append(("show", (record_id, cursor)))

    monkeypatch.setattr(cli_main, "import_decision_lineage", record_import)
    monkeypatch.setattr(cli_main, "show_decision_lineage", record_show)
    for argv in (
        ["fakeyou", "decisions", "import-lineage", "--file", str(tmp_path / "doc.json")],
        ["fakeyou", "decisions", "lineage", "a" * 32, "--cursor", "next"],
    ):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as finished:
            await asyncio.to_thread(cli_main.main)
        assert finished.value.code in {0, None}

    assert calls == [("import", tmp_path / "doc.json"), ("show", ("a" * 32, "next"))]
