import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fakeyou.catalog.errors import FakeYouError
from fakeyou.diarize_client import DiarizedTurn
from fakeyou.diarize_correction import write_correction_bundle
from fakeyou.ingestion import ingest_corrections, ingest_meeting
from fakeyou.ingestion_status import IngestionStatus, status_path, write_status
from fakeyou.memory import (
    RECALL_EMPTY_RESULT,
    ensure_indexes,
    open_graphiti_memory,
    search_memory_evidence,
    verify_memory_connection,
)
from fakeyou.providers import memory_config
from fakeyou.record import TranscriptEntry, load_meeting_record
from fakeyou.settings import DEFAULTS, Settings

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "memory-correction-v1.json"


def load_correction_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def token_present(token: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(token) + r"\b", text, re.IGNORECASE) is not None


def query_matches(answer: str, present: list[str], absent: list[str]) -> bool:
    return all(token_present(token, answer) for token in present) and not any(
        token_present(token, answer) for token in absent
    )


def superseded_serve_rate(answers: list[str], stale: str) -> float:
    if not answers:
        return 0.0
    return sum(1 for answer in answers if token_present(stale, answer)) / len(answers)


def answer_accuracy(matches: list[bool]) -> float:
    if not matches:
        return 0.0
    return sum(1 for matched in matches if matched) / len(matches)


def invalidation_recall(edges: list[Any]) -> float | None:
    if not edges:
        return None
    return sum(1 for edge in edges if edge.invalid_at is not None) / len(edges)


def over_invalidation_count(edges: list[Any]) -> int:
    return sum(1 for edge in edges if edge.invalid_at is not None or edge.expired_at is not None)


def entity_merge_rate(nodes: list[Any], name: str) -> float | None:
    identities = {node.uuid for node in nodes if node.name == name}
    if not identities:
        return None
    return 1.0 if len(identities) == 1 else 0.0


def correction_gate_passed(*, invalidation: float | None, superseded: float) -> bool:
    return invalidation == 1.0 and superseded == 0.0


def pre_stale_hit(
    pre_answers: dict[str, str] | None, changed_ids: list[str] | None, stale: str
) -> bool | None:
    if pre_answers is None or not changed_ids:
        return None
    return all(token_present(stale, pre_answers.get(query_id, "")) for query_id in changed_ids)


def eval_validity(
    *, changed_edges: int, stable_edges: int, fixed_edges: int, pre_hit: bool | None
) -> dict[str, Any]:
    valid = changed_edges >= 1 and stable_edges >= 1 and fixed_edges >= 1 and pre_hit is True
    return {
        "changed_edges": changed_edges,
        "stable_edges": stable_edges,
        "fixed_edges": fixed_edges,
        "pre_stale_hit": pre_hit,
        "valid": valid,
    }


def eval_is_valid(validity: dict[str, Any]) -> bool:
    return (
        validity["changed_edges"] >= 1
        and validity["stable_edges"] >= 1
        and validity["fixed_edges"] >= 1
        and validity["pre_stale_hit"] is True
    )


def over_invalidation_passed(value: int, *, valid: bool) -> bool | None:
    if not valid:
        return None
    return value == 0


def entity_merge_promotion_ready(history: list[dict[str, Any]]) -> bool:
    if len(history) < 3:
        return False
    tail = history[-3:]
    return all(
        run["valid"] is True and run["entity_merge_rate"] == 1.0 and run["fixed_edges"] >= 1
        for run in tail
    )


def accuracy_gate_threshold(history: list[dict[str, Any]]) -> float | None:
    scored = [run["corrected_answer_accuracy"] for run in history if run["valid"] is True]
    if len(scored) < 3:
        return None
    median = sorted(scored[-3:])[1]
    if median >= 0.75:
        return 0.5
    return None


def write_fixture_record(target: Path, fixture: dict[str, Any]) -> Path:
    base = datetime.fromisoformat(str(fixture["base_at"]))
    lines: list[str] = []
    counter = 0
    for window in fixture["windows"]:
        for utterance in window["utterances"]:
            moment = base + timedelta(seconds=int(utterance["offset_seconds"]))
            entry = TranscriptEntry(
                at=moment,
                id=f"u{counter:02d}",
                spoken_at=moment.isoformat(),
                source="meeting_audio",
                text=str(utterance["text"]),
                participant_id=str(utterance["speaker"]),
                speaker_evidence_id=None,
                capture_ms=float(utterance["offset_seconds"]) * 1000.0,
            )
            lines.append(entry.model_dump_json())
            counter += 1
    record_path = target / str(fixture["meeting_id"]) / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    correction = fixture["correction"]
    write_correction_bundle(
        record_path.parent / "audio",
        record_path,
        [
            DiarizedTurn(
                start_ms=int(correction["start_ms"]),
                end_ms=int(correction["end_ms"]),
                speaker=str(correction["speaker"]),
            )
        ],
        clock=lambda: datetime.now(UTC),
    )
    return record_path


async def answer_queries(
    memory: Any, queries: list[dict[str, Any]], *, group_id: str
) -> dict[str, str]:
    recall = DEFAULTS.memory.recall
    answers: dict[str, str] = {}
    for item in queries:
        answers[str(item["id"])] = await search_memory_evidence(
            memory, str(item["query"]), group_id=group_id, recall=recall
        )
    return answers


async def run_memory_correction(
    memory: Any, record_path: Path, fixture: dict[str, Any], *, group_id: str
) -> dict[str, Any]:
    meeting_id = str(fixture["meeting_id"])
    ingested = await ingest_meeting(
        memory,
        load_meeting_record(record_path),
        group_id=group_id,
        meeting_id=meeting_id,
        twin_name=str(fixture["twin_name"]),
    )
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id=meeting_id,
            group_id=group_id,
            status="completed",
            windows=ingested.windows,
            ingested=ingested.ingested,
            episode_uuids=dict(ingested.episode_uuids),
        ),
    )
    queries = [dict(item) for item in fixture["queries"]]
    pre_answers = await answer_queries(memory, queries, group_id=group_id)
    corrected = await ingest_corrections(
        memory, record_path, group_id=group_id, meeting_id=meeting_id
    )
    post_answers = await answer_queries(memory, queries, group_id=group_id)
    changed_index = next(int(window["index"]) for window in fixture["windows"] if window["changed"])
    changed = await memory.get_nodes_and_edges_by_episode([ingested.episode_uuids[changed_index]])
    stable_uuids = [
        episode_uuid
        for index, episode_uuid in ingested.episode_uuids.items()
        if index != changed_index
    ]
    stable = await memory.get_nodes_and_edges_by_episode(stable_uuids)
    fixed = await memory.get_nodes_and_edges_by_episode(list(corrected.episodes))
    stale = str(fixture["stale_speaker"])
    changed_ids = [str(item["id"]) for item in queries if item["kind"] == "changed"]
    pre_hit = pre_stale_hit(pre_answers, changed_ids, stale)
    validity = eval_validity(
        changed_edges=len(changed.edges),
        stable_edges=len(stable.edges),
        fixed_edges=len(fixed.edges),
        pre_hit=pre_hit,
    )
    scored = [item for item in queries if item["kind"] != "abstention"]
    matches = {
        str(item["id"]): query_matches(
            post_answers[str(item["id"])],
            [str(token) for token in item["present"]],
            [str(token) for token in item["absent"]],
        )
        for item in scored
    }
    abstained = [
        post_answers[str(item["id"])] == RECALL_EMPTY_RESULT
        for item in queries
        if item["kind"] == "abstention"
    ]
    metrics = {
        "invalidation_recall": invalidation_recall(changed.edges),
        "superseded_serve_rate": superseded_serve_rate(
            [post_answers[query_id] for query_id in changed_ids], stale
        ),
        "corrected_answer_accuracy": answer_accuracy([matches[query_id] for query_id in matches]),
        "over_invalidation": over_invalidation_count(stable.edges),
        "entity_merge_rate": entity_merge_rate(
            [*stable.nodes, *fixed.nodes], str(fixture["corrected_speaker"])
        ),
        "abstention_rate": answer_accuracy(abstained),
    }
    return {
        "windows": ingested.windows,
        "completed": ingested.completed and corrected.completed,
        "correction_episodes": len(corrected.episodes),
        "pre_answers": pre_answers,
        "post_answers": post_answers,
        "matches": matches,
        "changed_edges": len(changed.edges),
        "stable_edges": len(stable.edges),
        "fixed_edges": len(fixed.edges),
        "validity": validity,
        "valid": validity["valid"],
        "metrics": metrics,
    }


def test_correction_fixture_schema() -> None:
    fixture = load_correction_fixture()

    assert fixture["version"] == "memory-correction-v1"
    assert fixture["meeting_id"] == "memcorr-v1"
    assert len(fixture["windows"]) == 3
    changed = [window for window in fixture["windows"] if window["changed"]]
    assert [window["index"] for window in changed] == [1]
    for window in fixture["windows"]:
        assert len(window["utterances"]) >= 2
    queries = fixture["queries"]
    assert len(queries) == 10
    kinds = [query["kind"] for query in queries]
    assert kinds.count("changed") == 6
    assert kinds.count("stable") == 2
    assert kinds.count("abstention") == 2
    for query in queries:
        assert query["present"] or query["kind"] == "abstention"
        assert fixture["stale_speaker"] in query["absent"]
    for query in queries:
        if query["kind"] == "changed":
            assert fixture["corrected_speaker"] in query["present"]


def test_token_present_matches_words_only() -> None:
    assert token_present("Nadia", "Nadia said that Joko is the PIC.") is True
    assert token_present("nadia", "NADIA decided the schedule.") is True
    assert token_present("Riko", "Nadia decided the schedule.") is False
    assert token_present("Riko", "Rikovan joined the meeting.") is False
    assert token_present("demo", "The client demo moves to Thursday.") is True


def test_query_matches_checks_present_and_absent() -> None:
    answer = "Nadia said that Joko is the PIC for the rollout."

    assert query_matches(answer, ["Nadia", "rollout"], ["Riko"]) is True
    assert query_matches(answer, ["Nadia", "Monday"], ["Riko"]) is False
    assert query_matches(answer, ["Nadia"], ["Joko"]) is False
    assert query_matches("", [], ["Riko"]) is True


def test_superseded_rate_counts_stale_answers() -> None:
    assert superseded_serve_rate([], "Riko") == 0.0
    assert superseded_serve_rate(["Nadia decided the demo schedule."], "Riko") == 0.0
    assert (
        superseded_serve_rate(
            [
                "Nadia decided the demo schedule.",
                "Riko said that Joko is the PIC for the rollout.",
            ],
            "Riko",
        )
        == 0.5
    )


def test_answer_accuracy_counts_matching_answers() -> None:
    assert answer_accuracy([]) == 0.0
    assert answer_accuracy([True, True, True]) == 1.0
    assert answer_accuracy([True, False, True, False]) == 0.5


def test_invalidation_recall_counts_invalidated_edges() -> None:
    assert invalidation_recall([]) is None
    edges = [
        SimpleNamespace(invalid_at="2026-01-15T10:00:00+00:00", expired_at=None),
        SimpleNamespace(invalid_at=None, expired_at=None),
    ]

    assert invalidation_recall(edges) == 0.5
    assert invalidation_recall(edges[:1]) == 1.0


def test_over_invalidation_counts_valid_edges() -> None:
    assert over_invalidation_count([]) == 0
    edges = [
        SimpleNamespace(invalid_at=None, expired_at=None),
        SimpleNamespace(invalid_at="2026-01-15T10:00:00+00:00", expired_at=None),
        SimpleNamespace(invalid_at=None, expired_at="2026-01-15T10:00:00+00:00"),
    ]

    assert over_invalidation_count(edges) == 2


def test_entity_merge_rate_tracks_single_node() -> None:
    assert entity_merge_rate([], "Nadia") is None
    assert (
        entity_merge_rate(
            [
                SimpleNamespace(uuid="node-a", name="Nadia"),
                SimpleNamespace(uuid="node-a", name="Nadia"),
                SimpleNamespace(uuid="node-b", name="Joko"),
            ],
            "Nadia",
        )
        == 1.0
    )
    assert (
        entity_merge_rate(
            [
                SimpleNamespace(uuid="node-a", name="Nadia"),
                SimpleNamespace(uuid="node-c", name="Nadia"),
            ],
            "Nadia",
        )
        == 0.0
    )


def test_gate_passes_only_on_full_invalidation_and_zero_superseded() -> None:
    assert correction_gate_passed(invalidation=1.0, superseded=0.0) is True
    assert correction_gate_passed(invalidation=0.5, superseded=0.0) is False
    assert correction_gate_passed(invalidation=1.0, superseded=0.25) is False
    assert correction_gate_passed(invalidation=None, superseded=0.0) is False


def test_gate_rejects_recall_serving_stale_fact() -> None:
    stale = load_correction_fixture()["stale_speaker"]
    planted = ["Riko said that Joko is the PIC for the rollout."]

    assert superseded_serve_rate(planted, stale) == 1.0
    assert correction_gate_passed(invalidation=1.0, superseded=1.0) is False


def test_validity_rejects_empty_stable_edges() -> None:
    validity = eval_validity(changed_edges=2, stable_edges=0, fixed_edges=3, pre_hit=True)

    assert validity["valid"] is False
    assert eval_is_valid(validity) is False


def test_validity_rejects_empty_fixed_edges() -> None:
    validity = eval_validity(changed_edges=2, stable_edges=4, fixed_edges=0, pre_hit=True)

    assert validity["valid"] is False
    assert eval_is_valid(validity) is False


def test_validity_rejects_empty_superseded_evidence() -> None:
    assert pre_stale_hit({}, [], "Riko") is None
    assert pre_stale_hit(None, None, "Riko") is None

    validity = eval_validity(changed_edges=2, stable_edges=4, fixed_edges=3, pre_hit=None)

    assert validity["valid"] is False
    assert eval_is_valid(validity) is False


def test_validity_rejects_missing_pre_stale_hit() -> None:
    missed = eval_validity(changed_edges=2, stable_edges=4, fixed_edges=3, pre_hit=False)
    absent = eval_validity(changed_edges=2, stable_edges=4, fixed_edges=3, pre_hit=None)

    assert missed["valid"] is False
    assert absent["valid"] is False
    assert eval_is_valid(missed) is False
    assert eval_is_valid(absent) is False


def test_valid_case_evaluates_gates_normally() -> None:
    validity = eval_validity(changed_edges=2, stable_edges=4, fixed_edges=3, pre_hit=True)
    merged = {"valid": True, "entity_merge_rate": 1.0, "fixed_edges": 3}
    scored = {"valid": True, "corrected_answer_accuracy": 0.75}

    assert validity["valid"] is True
    assert eval_is_valid(validity) is True
    assert over_invalidation_passed(0, valid=True) is True
    assert over_invalidation_passed(2, valid=True) is False
    assert over_invalidation_passed(0, valid=False) is None
    assert entity_merge_promotion_ready([merged, merged, merged]) is True
    assert entity_merge_promotion_ready([merged, merged]) is False
    assert accuracy_gate_threshold([scored, scored, scored]) == 0.5
    assert accuracy_gate_threshold([scored, scored]) is None


@pytest.mark.live
async def test_live_memory_correction_gate(tmp_path: Path) -> None:
    fixture = load_correction_fixture()
    record_path = write_fixture_record(tmp_path, fixture)
    try:
        config = memory_config(Settings())
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    group_id = f"memcorr-{uuid.uuid4().hex[:8]}"
    async with open_graphiti_memory(config) as memory:
        try:
            await verify_memory_connection(memory)
            await ensure_indexes(memory)
        except Exception as error:
            pytest.skip(f"memory store is unreachable: {error}")
        started = time.monotonic()
        summary = await run_memory_correction(memory, record_path, fixture, group_id=group_id)
        duration = time.monotonic() - started
    metrics = summary["metrics"]
    print(f"\nfixture: {FIXTURE.name} group: {group_id} duration_s: {duration:.1f}")
    print(f"metrics: {json.dumps(metrics, sort_keys=True)}")
    print(f"validity: {json.dumps(summary['validity'], sort_keys=True)}")
    print(f"changed_edges: {summary['changed_edges']} matches: {summary['matches']}")
    if not eval_is_valid(summary["validity"]):
        validity_json = json.dumps(summary["validity"], sort_keys=True)
        pytest.fail(f"INVALID memory-correction run: {validity_json}")
    assert summary["correction_episodes"] == 1
    assert summary["changed_edges"] >= 1
    assert metrics["invalidation_recall"] == 1.0
    assert metrics["superseded_serve_rate"] == 0.0
    assert correction_gate_passed(
        invalidation=metrics["invalidation_recall"], superseded=metrics["superseded_serve_rate"]
    )
    assert over_invalidation_passed(metrics["over_invalidation"], valid=summary["valid"]) is True
