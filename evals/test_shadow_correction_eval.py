import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import fakeyou.memory as memory_module
from evals.test_memory_correction_eval import (
    answer_queries,
    load_correction_fixture,
    query_matches,
    superseded_serve_rate,
    token_present,
    write_fixture_record,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.ingestion import ingest_corrections, ingest_meeting
from fakeyou.ingestion_status import IngestionStatus, status_path, write_status
from fakeyou.memory import (
    create_memory_tools,
    ensure_indexes,
    open_graphiti_memory,
    verify_memory_connection,
)
from fakeyou.providers import memory_config
from fakeyou.record import load_meeting_record
from fakeyou.settings import Settings
from tests.fakes import make_graphiti

SHADOW_GROUP_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def valid_group_id(group_id: str) -> bool:
    return SHADOW_GROUP_PATTERN.fullmatch(group_id) is not None


def shadow_group_id(prod_group: str, stamp: str) -> str:
    return f"{prod_group}-shadow-{stamp}"


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")


def fixture_base_at(fixture: dict[str, Any], offset_minutes: int) -> datetime:
    return datetime.fromisoformat(str(fixture["base_at"])) + timedelta(minutes=offset_minutes)


def stale_served(answer: str, stale: str) -> bool:
    return token_present(stale, answer)


def diff_row(
    query_id: str,
    prod: str,
    shadow: str,
    present: list[str],
    absent: list[str],
    stale: str,
) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "prod": prod,
        "shadow": shadow,
        "token_match": {
            "prod": query_matches(prod, present, absent),
            "shadow": query_matches(shadow, present, absent),
        },
        "stale_served": {
            "prod": stale_served(prod, stale),
            "shadow": stale_served(shadow, stale),
        },
    }


def changed_query_ids(queries: list[dict[str, Any]]) -> list[str]:
    return [str(item["id"]) for item in queries if item["kind"] == "changed"]


def count_superseded(answers: dict[str, str], query_ids: list[str], stale: str) -> int:
    return sum(1 for query_id in query_ids if stale_served(answers.get(query_id, ""), stale))


def build_diff_report(
    prod_answers: dict[str, str],
    shadow_answers: dict[str, str],
    queries: list[dict[str, Any]],
    stale: str,
) -> dict[str, Any]:
    rows = [
        diff_row(
            str(item["id"]),
            prod_answers.get(str(item["id"]), ""),
            shadow_answers.get(str(item["id"]), ""),
            [str(token) for token in item["present"]],
            [str(token) for token in item["absent"]],
            stale,
        )
        for item in queries
    ]
    changed_ids = changed_query_ids(queries)
    return {
        "rows": rows,
        "aggregate": {
            "changed": len(changed_ids),
            "superseded_prod": count_superseded(prod_answers, changed_ids, stale),
            "superseded_shadow": count_superseded(shadow_answers, changed_ids, stale),
            "superseded_rate_prod": superseded_serve_rate(
                [prod_answers.get(query_id, "") for query_id in changed_ids], stale
            ),
            "superseded_rate_shadow": superseded_serve_rate(
                [shadow_answers.get(query_id, "") for query_id in changed_ids], stale
            ),
        },
    }


def diff_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def shadow_wins(report: dict[str, Any]) -> bool:
    aggregate = report["aggregate"]
    return aggregate["superseded_shadow"] == 0 and aggregate["superseded_prod"] >= 1


async def run_shadow_dual(
    memory: Any,
    record_prod: Path,
    record_shadow: Path,
    fixture: dict[str, Any],
    *,
    prod_group: str,
    shadow_group: str,
) -> dict[str, Any]:
    meeting_id = str(fixture["meeting_id"])
    twin_name = str(fixture["twin_name"])
    queries = [dict(item) for item in fixture["queries"]]
    prod_ingested = await ingest_meeting(
        memory,
        load_meeting_record(record_prod),
        group_id=prod_group,
        meeting_id=meeting_id,
        twin_name=twin_name,
    )
    write_status(
        status_path(record_prod),
        IngestionStatus(
            meeting_id=meeting_id,
            group_id=prod_group,
            status="completed",
            windows=prod_ingested.windows,
            ingested=prod_ingested.ingested,
            episode_uuids=dict(prod_ingested.episode_uuids),
        ),
    )
    shadow_ingested = await ingest_meeting(
        memory,
        load_meeting_record(record_shadow),
        group_id=shadow_group,
        meeting_id=meeting_id,
        twin_name=twin_name,
    )
    write_status(
        status_path(record_shadow),
        IngestionStatus(
            meeting_id=meeting_id,
            group_id=shadow_group,
            status="completed",
            windows=shadow_ingested.windows,
            ingested=shadow_ingested.ingested,
            episode_uuids=dict(shadow_ingested.episode_uuids),
        ),
    )
    prod_before = await answer_queries(memory, queries, group_id=prod_group)
    corrected = await ingest_corrections(
        memory, record_shadow, group_id=shadow_group, meeting_id=meeting_id
    )
    shadow_answers = await answer_queries(memory, queries, group_id=shadow_group)
    prod_after = await answer_queries(memory, queries, group_id=prod_group)
    answers = build_diff_report(prod_after, shadow_answers, queries, str(fixture["stale_speaker"]))
    return {
        "prod_completed": prod_ingested.completed,
        "shadow_completed": shadow_ingested.completed and corrected.completed,
        "shadow_corrections": len(corrected.episodes),
        "prod_before": prod_before,
        "prod_after": prod_after,
        "shadow_answers": shadow_answers,
        "prod_group": prod_group,
        "shadow_group": shadow_group,
        "report": answers,
    }


def test_shadow_diff_report_marks_stale_and_aggregates() -> None:
    fixture = load_correction_fixture()
    stale = str(fixture["stale_speaker"])
    queries = [dict(item) for item in fixture["queries"]]
    prod_answers = {}
    shadow_answers = {}
    for item in queries:
        present = [str(token) for token in item["present"]]
        if item["kind"] == "abstention":
            prod_answers[str(item["id"])] = ""
            shadow_answers[str(item["id"])] = ""
        else:
            prod_answers[str(item["id"])] = " ".join([stale, *present])
            shadow_answers[str(item["id"])] = " ".join(present)
    report = build_diff_report(prod_answers, shadow_answers, queries, stale)

    assert [row["query_id"] for row in report["rows"]] == [str(item["id"]) for item in queries]
    changed_row = next(row for row in report["rows"] if row["query_id"] == "q01")
    assert changed_row["token_match"] == {"prod": False, "shadow": True}
    assert changed_row["stale_served"] == {"prod": True, "shadow": False}
    stable_row = next(row for row in report["rows"] if row["query_id"] == "q07")
    assert stable_row["stale_served"] == {"prod": True, "shadow": False}
    assert report["aggregate"]["changed"] == 6
    assert report["aggregate"]["superseded_prod"] == 6
    assert report["aggregate"]["superseded_shadow"] == 0
    assert report["aggregate"]["superseded_rate_prod"] == 1.0
    assert report["aggregate"]["superseded_rate_shadow"] == 0.0
    assert shadow_wins(report) is True
    assert json.loads(diff_report_json(report)) == report
    assert valid_group_id("") is False
    assert valid_group_id("has space") is False
    assert valid_group_id(shadow_group_id("prod1", "20260115120000")) is True


async def test_shadow_dual_run_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = load_correction_fixture()
    stale = str(fixture["stale_speaker"])
    fixed = str(fixture["corrected_speaker"])
    meeting_id = str(fixture["meeting_id"])
    queries = [dict(item) for item in fixture["queries"]]
    prod_group = f"shd-{uuid.uuid4().hex[:8]}"
    shadow_group = shadow_group_id(prod_group, _utc_stamp())

    assert valid_group_id(prod_group)
    assert valid_group_id(shadow_group)
    assert len(create_memory_tools(make_graphiti(), group_id=shadow_group)) == 2

    record_path = write_fixture_record(tmp_path, fixture)
    ingest_store = make_graphiti()
    corrected = await ingest_corrections(
        ingest_store, record_path, group_id=shadow_group, meeting_id=meeting_id
    )

    assert corrected.completed is True
    assert corrected.changed_windows == 1
    assert len(corrected.episodes) == 1

    fixed_lines = str(ingest_store.added[0]["episode_body"]).split("\n")

    assert len(fixed_lines) == 2
    assert all(token_present(fixed, line) for line in fixed_lines)
    assert all(not token_present(stale, line) for line in fixed_lines)

    live_changed = [
        f"{utterance['speaker']}: {utterance['text']}"
        for window in fixture["windows"]
        if window["changed"]
        for utterance in window["utterances"]
    ]
    stable_lines = [
        f"{utterance['speaker']}: {utterance['text']}"
        for window in fixture["windows"]
        if not window["changed"]
        for utterance in window["utterances"]
    ]
    prod_facts = [*live_changed, *stable_lines]
    shadow_facts = [*fixed_lines, *stable_lines]
    prod_store = make_graphiti(
        facts=prod_facts,
        episodes={fact: [f"prod-{index}"] for index, fact in enumerate(prod_facts)},
    )
    shadow_store = make_graphiti(
        facts=shadow_facts,
        episodes={fact: [f"shadow-{index}"] for index, fact in enumerate(shadow_facts)},
    )
    stored: dict[str, Any] = {}
    for index, _fact in enumerate(prod_facts):
        stored[f"prod-{index}"] = SimpleNamespace(
            uuid=f"prod-{index}",
            group_id=prod_group,
            source_description=f"Google Meet {meeting_id} transcript",
            valid_at=fixture_base_at(fixture, index),
            created_at=fixture_base_at(fixture, index),
        )
    for index, _fact in enumerate(shadow_facts):
        stored[f"shadow-{index}"] = SimpleNamespace(
            uuid=f"shadow-{index}",
            group_id=shadow_group,
            source_description=f"Google Meet {meeting_id} transcript",
            valid_at=fixture_base_at(fixture, index),
            created_at=fixture_base_at(fixture, index),
        )

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def active_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        return dict.fromkeys(episode_uuids, "active")

    monkeypatch.setattr(memory_module.EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(memory_module, "read_note_states", active_states)
    prod_before = await answer_queries(prod_store, queries, group_id=prod_group)
    shadow_answers = await answer_queries(shadow_store, queries, group_id=shadow_group)
    prod_after = await answer_queries(prod_store, queries, group_id=prod_group)

    assert prod_after == prod_before
    assert prod_store.searched
    assert all(entry["group_ids"] == [prod_group] for entry in prod_store.searched)
    assert shadow_store.searched
    assert all(entry["group_ids"] == [shadow_group] for entry in shadow_store.searched)

    report = build_diff_report(prod_after, shadow_answers, queries, stale)
    changed_ids = changed_query_ids(queries)
    before_flags = [stale_served(prod_before[query_id], stale) for query_id in changed_ids]
    after_flags = [stale_served(prod_after[query_id], stale) for query_id in changed_ids]

    assert after_flags == before_flags
    assert report["aggregate"]["superseded_shadow"] == 0
    assert report["aggregate"]["superseded_prod"] >= 1
    assert shadow_wins(report) is True
    assert json.loads(diff_report_json(report))["aggregate"] == report["aggregate"]


@pytest.mark.live
async def test_live_shadow_dual_run(tmp_path: Path) -> None:
    fixture = load_correction_fixture()
    prod_group = f"shd-{uuid.uuid4().hex[:8]}"
    shadow_group = shadow_group_id(prod_group, _utc_stamp())

    assert valid_group_id(prod_group)
    assert valid_group_id(shadow_group)

    record_prod = write_fixture_record(tmp_path / "prod", fixture)
    record_shadow = write_fixture_record(tmp_path / "shadow", fixture)
    try:
        config = memory_config(Settings())
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    async with open_graphiti_memory(config) as memory:
        try:
            await verify_memory_connection(memory)
            await ensure_indexes(memory)
        except Exception as error:
            pytest.skip(f"memory store is unreachable: {error}")
        started = time.monotonic()
        dual = await run_shadow_dual(
            memory,
            record_prod,
            record_shadow,
            fixture,
            prod_group=prod_group,
            shadow_group=shadow_group,
        )
        duration = time.monotonic() - started
    report = dual["report"]
    artifact = tmp_path / "shadow-diff-report.json"
    artifact.write_text(diff_report_json(report), encoding="utf-8")
    print(f"\nfixture: memcorr-v1 prod: {prod_group} shadow: {shadow_group}")
    print(f"duration_s: {duration:.1f} aggregate: {json.dumps(report['aggregate'])}")
    print(f"artifact: {artifact}")

    assert dual["prod_completed"] is True
    assert dual["shadow_completed"] is True
    assert dual["shadow_corrections"] == 1
    assert [row["query_id"] for row in report["rows"]] == [
        str(item["id"]) for item in fixture["queries"]
    ]
    changed_ids = changed_query_ids([dict(item) for item in fixture["queries"]])
    stale = str(fixture["stale_speaker"])
    before_flags = [stale_served(dual["prod_before"][query_id], stale) for query_id in changed_ids]
    after_flags = [stale_served(dual["prod_after"][query_id], stale) for query_id in changed_ids]

    assert after_flags == before_flags
    if report["aggregate"]["superseded_prod"] < 1:
        aggregate = json.dumps(report["aggregate"], sort_keys=True)
        pytest.fail(f"INVALID shadow run: baseline shows no stale: {aggregate}")

    assert shadow_wins(report) is True
