import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from evals._shared import sanitized_role, token_present
from evals._trust import (
    AID_CHECKS,
    EvalDelayedModel,
    TrustScore,
    build_case_memory,
    case_storage,
    contract_model,
    load_trust_fixture,
    passing_trust_reviews,
    rated_pairs,
    replay_trust,
    score_aids,
    strategy_settings,
    trust_gate,
    write_trust_summary,
)
from evals.runtime_evidence import prepare_artifact_directory, runtime_metadata
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import CompatibleReply, OpenAIReply, Settings
from fakeyou.conversation.conversation import cap_spoken_reply
from fakeyou.memory.memory import RECALL_EMPTY_RESULT, create_memory_tools
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.providers import create_chat_model, create_fallback_chat_model
from tests.fakes import ToolCallingFakeModel

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trust-scenarios.json"
TRUST_RUNS = 3
USEFULNESS_THRESHOLD = 0.9
MIN_USEFUL_PAIRS = 79
JUDGE_MODEL = "gpt-4.1-mini"
EXPECTED_CASE_IDS = frozenset(f"c{i:02d}" for i in range(1, 30))


def trust_source_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ROOT,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def trust_dirty_flag() -> bool:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ROOT,
        )
        return bool(proc.stdout.strip())
    except Exception:
        return True


def judge_available() -> bool:
    return bool(os.environ.get("EVAL_JUDGE_API_KEY", "").strip())


def write_trust_artifacts(
    directory: Path,
    *,
    score: TrustScore,
    settings: Settings,
    fixture: dict[str, Any],
    fixture_path: Path,
    source_commit: str,
    mode: str,
    model_name: str,
) -> None:
    prepare_artifact_directory(directory)
    dumped = settings.model_dump(mode="json")
    reply_raw = dumped.get("reply", {})
    fallback_raw = dumped.get("reply_fallback")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "model": model_name,
        "source_commit": score.candidate.get("source_commit", source_commit),
        "dirty": score.candidate.get("dirty_at_start"),
        "candidate": score.candidate,
        "provenance_valid": score.candidate.get("unchanged") is True
        and score.runtime == runtime_metadata(settings),
        "runtime": score.runtime,
        "fixture_name": fixture_path.name,
        "fixture_sha256": score.candidate.get("fixture_sha256"),
        "persona": str(fixture.get("persona", "")),
        "persona_sha256": score.candidate.get("persona_sha256"),
        "fixture_payload_sha256": score.candidate.get("fixture_payload_sha256"),
        "runs": len({turn["run_index"] for turn in score.turns}),
        "cases": len(fixture["cases"]),
        "turns": len(score.turns),
        "rated_pairs": len(rated_pairs(score)),
        "sentence_streaming": dumped.get("conversation", {}).get("sentence_streaming"),
        "thresholds": {
            "usefulness_min": USEFULNESS_THRESHOLD,
            "min_useful_pairs": MIN_USEFUL_PAIRS,
            "severe_max": 0,
        },
        "judge": {
            "available": judge_available(),
            "model": JUDGE_MODEL,
            "note": "Independent meaning review is recorded in reviews.json, never self-assessed.",
        },
        "review": {
            "required_live_checks": ["all_turns"],
            "scope": "One verdict per case/run covers every turn, including unrated turns; "
            "any severe earlier response makes that case/run severe.",
        },
        "reply": sanitized_role(reply_raw) if isinstance(reply_raw, dict) else {},
        "reply_fallback": sanitized_role(fallback_raw) if isinstance(fallback_raw, dict) else None,
        "response_timeout_seconds": dumped.get("conversation", {}).get("response_timeout_seconds"),
        "recall_timeout_seconds": dumped.get("memory", {}).get("recall", {}).get("timeout_seconds"),
        "latency": score.latency_summary(),
        "model_calls": sum(int(turn.get("model_calls", 0)) for turn in score.turns),
        "recall_calls": sum(len(turn.get("recall_evidence", [])) for turn in score.turns),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "turns.json").write_text(
        json.dumps(score.turns, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "prompts.json").write_text(
        json.dumps(score.prompts, indent=2, sort_keys=True), encoding="utf-8"
    )


def summarize_trust(
    reviews: list[dict[str, object]],
    *,
    expected_case_ids: set[str],
    runs: int,
    score: TrustScore | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    gate = trust_gate(reviews, expected_case_ids=expected_case_ids, runs=runs)
    severe = sum(1 for review in reviews if review.get("severe") is True)
    useful = sum(1 for review in reviews if review.get("useful") is True)
    pairs = {
        (str(review["case_id"]), int(review["run_index"]))
        for review in reviews
        if isinstance(review.get("case_id"), str) and isinstance(review.get("run_index"), int)
    }
    expected_pairs = {(case_id, run) for case_id in expected_case_ids for run in range(runs)}
    summary: dict[str, Any] = {
        "reviews_seen": len(reviews),
        "reviews_expected": len(expected_case_ids) * runs,
        "pairs_complete": pairs == expected_pairs,
        "severe_count": severe,
        "useful_count": useful,
        "usefulness": round(useful / len(reviews), 3) if reviews else None,
        "gate_passed": gate,
        "contract_only": all(review.get("mode", "contract") == "contract" for review in reviews),
    }
    if score is not None:
        summary["provenance_valid"] = (
            score.candidate.get("unchanged") is True
            and settings is not None
            and score.runtime == runtime_metadata(settings)
        )
        summary["gate_passed"] = gate and summary["provenance_valid"]
        summary["latency"] = score.latency_summary()
        summary["aids"] = score.aid_rates()
        summary["exceptions"] = sum(1 for turn in score.turns if turn.get("exception"))
    return summary


def test_missing_reviews_and_one_severe_violation_fail_gate():
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = [
        {
            "case_id": case_id,
            "run_index": run,
            "severe": False,
            "useful": True,
            "reason": "Matches supplied evidence",
        }
        for run in range(3)
        for case_id in sorted(case_ids)
    ]
    assert trust_gate(reviews, expected_case_ids=case_ids, runs=3)
    assert not trust_gate(reviews[:-1], expected_case_ids=case_ids, runs=3)
    reviews[17]["severe"] = True
    assert not trust_gate(reviews, expected_case_ids=case_ids, runs=3)


def test_trust_gate_rejects_malformed_reviews():
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    assert not trust_gate("reviews", expected_case_ids=case_ids, runs=3)
    assert not trust_gate([], expected_case_ids=set(), runs=3)
    assert not trust_gate(passing_trust_reviews(case_ids, 3), expected_case_ids=case_ids, runs=0)
    assert not trust_gate(passing_trust_reviews(case_ids, 3), expected_case_ids=case_ids, runs=True)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["severe"] = "false"
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["useful"] = 1
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["reason"] = "   "
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["run_index"] = 3
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["case_id"] = "c99"
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[1] = dict(malformed[0])
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["mode"] = "live"
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["mode"] = "live"
    malformed[0]["source_ref"] = "c01 turn reply plus owner-note evidence"
    malformed[0]["checked"] = ["all_turns"]
    assert trust_gate(malformed, expected_case_ids=case_ids, runs=3)


def test_trust_gate_enforces_usefulness_floor():
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    for index in range(8):
        reviews[index]["useful"] = False
    assert not trust_gate(reviews, expected_case_ids=case_ids, runs=3)
    reviews = passing_trust_reviews(case_ids, 3)
    for index in range(7):
        reviews[index]["useful"] = False
    assert trust_gate(reviews, expected_case_ids=case_ids, runs=3)


@pytest.mark.parametrize(
    "reason",
    [
        "Answer cites transcript as source although evidence comes from owner note.",
        "Answer presents source date as event date.",
        "Answer greets a name without identity source.",
        "Answer implicitly approves a contract without written permission.",
        "Answer is empty without any clarification.",
        "Answer presents unavailable status as known fact.",
    ],
)
def test_severe_violation_fails_gate(reason: str):
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    reviews[5]["severe"] = True
    reviews[5]["reason"] = reason
    assert not trust_gate(reviews, expected_case_ids=case_ids, runs=3)


def test_missing_single_review_fails_gate():
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    assert not trust_gate(reviews[1:], expected_case_ids=case_ids, runs=3)


def test_trust_corpus_defines_29_rated_cases():
    fixture = load_trust_fixture()
    cases = fixture["cases"]
    assert [case["id"] for case in cases] == [f"c{i:02d}" for i in range(1, 30)]
    families: dict[str, int] = {}
    for case in cases:
        families[case["family"]] = families.get(case["family"], 0) + 1
        assert case["expect"] in {"answer", "unknown", "clarify", "refuse"}
        assert isinstance(case["needs_recall"], bool)
        assert case["rubric"].strip()
        assert case["turns"]
        for turn in case["turns"]:
            assert turn["say"].strip()
            assert turn["contract_reply"].strip()
            assert cap_spoken_reply(turn["contract_reply"]) == turn["contract_reply"].strip()
    assert families == {"sumber_waktu": 11, "koreksi_gagal": 8, "otoritas_guna": 10}


async def recall_output_for(case: dict[str, Any], group_id: str) -> str:
    memory, stored, states = build_case_memory(case, group_id)
    with case_storage(memory, stored, states):
        _remember, production = create_memory_tools(memory, group_id=group_id)
        return str(await production.ainvoke({"query": str(case["turns"][0]["say"])}))


async def test_case_memory_match_any_returns_facts_without_lexical_overlap() -> None:
    stored = {
        "m1": {
            "group": "own",
            "label": "Google Meet m1 transcript",
            "source_at": "2026-09-23T03:10:00+00:00",
        }
    }
    turns = [{"say": "What is the budget status?", "contract_reply": "Bridge repaint starts June."}]
    matched = {
        "memory": {
            "facts": ["Bridge repaint starts June."],
            "episodes": {"Bridge repaint starts June.": ["m1"]},
            "stored": stored,
            "match": "any",
        },
        "turns": turns,
    }
    assert "Bridge repaint starts June." in await recall_output_for(matched, "team_alpha")
    sensitive = {
        "memory": {
            "facts": ["Bridge repaint starts June."],
            "episodes": {"Bridge repaint starts June.": ["m1"]},
            "stored": stored,
        },
        "turns": turns,
    }
    assert await recall_output_for(sensitive, "team_alpha") == RECALL_EMPTY_RESULT


async def test_contract_recall_uses_production_evidence_schema() -> None:
    fixture = load_trust_fixture()
    profile = PersonaProfile.model_validate_json((ROOT / fixture["persona"]).read_bytes())
    group_id = profile.memory_group
    by_id = {case["id"]: case for case in fixture["cases"]}
    merged = await recall_output_for(by_id["c01"], group_id)
    payload = json.loads(merged)
    assert payload["schema"] == "memory_evidence/v1"
    assert len(payload["facts"]) == 2
    kinds = {source["kind"] for fact in payload["facts"] for source in fact["sources"]}
    assert kinds == {"owner_note", "meeting_transcript"}
    assert payload["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 0}
    for case_id in ("c07", "c08", "c14"):
        assert await recall_output_for(by_id[case_id], group_id) == RECALL_EMPTY_RESULT
    corrected = await recall_output_for(by_id["c10"], group_id)
    for token in by_id["c10"].get("allow", []):
        assert token_present(str(token), corrected)
    for token in by_id["c10"].get("forbid", []):
        assert not token_present(str(token), corrected)
    assert json.loads(corrected)["excluded"]["superseded"] == 1


async def test_contract_replay_captures_87_rated_pairs_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    fixture = load_trust_fixture()
    raw_target = os.environ.get("TRUST_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    settings = strategy_settings("tool")
    score = await replay_trust(contract_model, fixture, TRUST_RUNS, settings=settings)
    assert settings.conversation.sentence_streaming is False
    write_trust_artifacts(
        target,
        score=score,
        settings=settings,
        fixture=fixture,
        fixture_path=FIXTURE,
        source_commit=score.candidate["source_commit"],
        mode="contract",
        model_name="ToolCallingFakeModel(contract-scripted)",
    )
    assert len(score.turns) == 93
    assert len(score.rated_turns()) == 87
    assert rated_pairs(score) == {
        (case_id, run) for case_id in EXPECTED_CASE_IDS for run in range(TRUST_RUNS)
    }
    assert all(turn["exception"] is None for turn in score.turns)
    assert all(turn["latency_seconds"] >= 0 for turn in score.turns)
    by_id = {case["id"]: case for case in fixture["cases"]}
    for turn in score.rated_turns():
        case = by_id[turn["case_id"]]
        assert turn["recall_called"] is case["needs_recall"]
        assert turn["aids"] == score_aids(case, turn["reply"], turn["recall_called"])
        assert all(turn["aids"][name] for name in AID_CHECKS)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "contract"
    assert manifest["cases"] == 29
    assert manifest["turns"] == 93
    assert manifest["rated_pairs"] == 87
    assert manifest["sentence_streaming"] is False
    assert manifest["thresholds"] == {
        "usefulness_min": USEFULNESS_THRESHOLD,
        "min_useful_pairs": MIN_USEFUL_PAIRS,
        "severe_max": 0,
    }
    assert manifest["judge"]["available"] == judge_available()
    assert manifest["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    recorded = json.loads((target / "turns.json").read_text(encoding="utf-8"))
    assert len(recorded) == 93


def test_trust_artifacts_sanitize_credentials(tmp_path: Path) -> None:
    marker_url = "MARKER_URL_SECRET"
    marker_body = "MARKER_BODY_SECRET"
    settings = Settings()
    marked = CompatibleReply(
        base_url=f"https://user:{marker_url}@example.com:8000/v1?token={marker_url}",
        model="marked-model",
        api_key=SecretStr("marked-key"),
        extra_body={"api_key": marker_body, "safe": "keep"},
    )
    marked_settings = settings.model_copy(update={"reply": marked})
    score = TrustScore()
    score.latencies.append(0.5)
    score.turns.append(
        {
            "case_id": "c01",
            "run_index": 0,
            "turn_index": 0,
            "rated": True,
            "aids": dict.fromkeys(AID_CHECKS, True),
        }
    )
    fixture = {"persona": "examples/persona.example.json", "cases": [{"id": "c01"}]}
    write_trust_artifacts(
        tmp_path,
        score=score,
        settings=marked_settings,
        fixture=fixture,
        fixture_path=FIXTURE,
        source_commit="test-commit",
        mode="contract",
        model_name="contract-test",
    )
    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert "MARKER_URL_SECRET" not in text
    assert "MARKER_BODY_SECRET" not in text
    assert "marked-key" not in text
    assert manifest["reply"]["base_url"] == "https://example.com:8000/v1"
    assert manifest["reply"]["extra_body"]["api_key"] == "**********"
    assert manifest["reply"]["extra_body"]["safe"] == "keep"


@pytest.mark.parametrize("role_name", ["reply", "reply_fallback"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unchanged", None),
        ("model", "different-writer-model"),
        ("provider", "openai"),
        ("timeout_seconds", 0.2),
        ("max_retries", 1),
        ("base_url", "https://different-writer.example/v1"),
        ("extra_body", {"routing": {"choices": ["changed"]}}),
    ],
)
async def test_trust_role_setting_change_invalidates_writer_provenance(
    tmp_path: Path,
    role_name: str,
    field: str,
    value: Any,
) -> None:
    initial_role = CompatibleReply(
        base_url="https://synthetic.example/v1",
        model="start-model",
        timeout_seconds=0.1,
        max_retries=0,
        extra_body={"routing": {"choices": ["start"]}},
    )
    start = Settings().model_copy(update={"reply": initial_role, "reply_fallback": initial_role})
    fixture = load_trust_fixture()
    fixture = {**fixture, "cases": fixture["cases"][:1]}
    score = await replay_trust(contract_model, fixture, 1, settings=start)
    if field == "unchanged":
        changed_role = initial_role
    elif field == "provider":
        changed_role = OpenAIReply.model_validate(
            initial_role.model_dump(exclude={"provider", "extra_body"})
        )
    else:
        changed_role = initial_role.model_copy(update={field: value})
    writer = start.model_copy(update={role_name: changed_role})
    write_trust_artifacts(
        tmp_path,
        score=score,
        settings=writer,
        fixture=fixture,
        fixture_path=FIXTURE,
        source_commit=score.candidate["source_commit"],
        mode="contract",
        model_name="ToolCallingFakeModel(contract-scripted)",
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["candidate"]["unchanged"] is True
    assert manifest["provenance_valid"] is (field == "unchanged")
    assert manifest["runtime"][role_name]["model"] == "start-model"
    reviews = passing_trust_reviews({"c01"}, 1)
    summary = summarize_trust(
        reviews, expected_case_ids={"c01"}, runs=1, score=score, settings=writer
    )
    assert summary["provenance_valid"] is manifest["provenance_valid"]
    assert summary["gate_passed"] is manifest["provenance_valid"]
    assert not summarize_trust(reviews, expected_case_ids={"c01"}, runs=1, score=score)[
        "gate_passed"
    ]


def test_runtime_role_snapshots_are_detached_and_sanitize_nested_credentials() -> None:
    role = CompatibleReply(
        base_url="https://user:START_URL_SECRET@synthetic.example/v1?token=START_URL_SECRET",
        model="snapshot-model",
        api_key=SecretStr("START_API_SECRET"),
        extra_body={
            "nested": {"api_key": "START_BODY_SECRET", "choices": ["start"]},
            "callback": "https://user:START_LINK_SECRET@synthetic.example/callback?key=START_LINK_SECRET",
        },
    )
    settings = Settings().model_copy(update={"reply": role, "reply_fallback": role})
    snapshot = runtime_metadata(settings)
    encoded = json.dumps(snapshot, sort_keys=True)
    for name in ("reply", "reply_fallback"):
        assert snapshot[name]["base_url"] == "https://synthetic.example/v1"
        assert snapshot[name]["api_key"] == "**********"
        assert snapshot[name]["extra_body"]["nested"]["api_key"] == "**********"
        assert snapshot[name]["extra_body"]["callback"] == "https://synthetic.example/callback"
    assert not any(
        marker in encoded
        for marker in (
            "START_URL_SECRET",
            "START_API_SECRET",
            "START_BODY_SECRET",
            "START_LINK_SECRET",
        )
    )
    assert role.extra_body is not None
    role.extra_body["nested"]["choices"].append("changed")
    role.extra_body["nested"]["api_key"] = "CHANGED_BODY_SECRET"
    assert json.dumps(snapshot, sort_keys=True) == encoded
    assert snapshot != runtime_metadata(settings)


def test_fallback_presence_is_part_of_runtime_provenance() -> None:
    settings = Settings().model_copy(update={"reply_fallback": None})
    missing = runtime_metadata(settings)
    configured = runtime_metadata(settings.model_copy(update={"reply_fallback": settings.reply}))
    assert missing["reply_fallback"] is None
    assert configured["reply_fallback"]["model"] == settings.reply.model
    assert missing != configured


def test_trust_summary_reports_gate_and_usefulness(tmp_path: Path) -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    summary = summarize_trust(reviews, expected_case_ids=case_ids, runs=3)
    assert summary["gate_passed"] is True
    assert summary["usefulness"] == 1.0
    assert summary["contract_only"] is True
    reviews[0]["useful"] = False
    summary = summarize_trust(reviews, expected_case_ids=case_ids, runs=3)
    assert summary["gate_passed"] is True
    assert summary["usefulness"] == round(74 / 75, 3)
    write_trust_summary(tmp_path, summary)
    assert (tmp_path / "summary.json").exists()


async def test_trust_native_greeting_avoids_model_and_records_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    profile = json.loads((ROOT / fixture["persona"]).read_text())
    profile["language"] = "en"
    (tmp_path / "persona.json").write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setitem(replay_trust.__globals__, "ROOT", tmp_path)
    case = {
        **fixture["cases"][0],
        "needs_recall": False,
        "turns": [{"say": "Hello", "contract_reply": "Model must not greet."}],
    }
    settings = Settings()
    settings = settings.model_copy(
        update={"speech": settings.speech.model_copy(update={"language": "id"})}
    )
    score = await replay_trust(
        contract_model,
        {**fixture, "persona": "persona.json", "cases": [case]},
        1,
        settings=settings,
    )
    assert score.turns[0]["reply"] == "Hello."
    assert score.turns[0]["model_calls"] == 0
    assert score.turns[0]["tool_trajectory"] == []
    assert score.candidate["unchanged"] is True


class EvalFailingModel(ToolCallingFakeModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("scripted primary unavailable")


async def test_trust_native_fallback_and_sentence_cap_preserve_raw_reply() -> None:
    fixture = load_trust_fixture()
    case = {**fixture["cases"][0], "needs_recall": False}
    fallback = ToolCallingFakeModel(responses=[AIMessage(content="First. Second. Third.")])
    score = await replay_trust(
        lambda _: EvalFailingModel(responses=[AIMessage(content="unreachable")]),
        {**fixture, "cases": [case]},
        1,
        fallback=fallback,
    )
    turn = score.turns[0]
    assert turn["exception"] is None
    assert turn["reply"] == "First. Second. Third."
    assert turn["spoken"] == "First. Second."
    assert turn["model_calls"] == 2


async def test_trust_response_deadline_cancels_native_responder() -> None:
    fixture = load_trust_fixture()
    case = {**fixture["cases"][0], "needs_recall": False}
    settings = Settings()
    settings = settings.model_copy(
        update={
            "conversation": settings.conversation.model_copy(
                update={"response_timeout_seconds": 0.02}
            )
        }
    )
    model = EvalDelayedModel(responses=[AIMessage(content="Too late.")])
    score = await replay_trust(lambda _: model, {**fixture, "cases": [case]}, 1, settings=settings)
    assert score.turns[0]["exception"].startswith("TimeoutError:")
    assert score.turns[0]["reply"] == ""
    assert score.turns[0]["latency_seconds"] < 0.5
    assert model.cancelled


async def test_trust_native_recall_failure_becomes_unavailable_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    case = fixture["cases"][0]
    original = build_case_memory

    def failing_memory(case: dict[str, Any], group: str) -> Any:
        memory, stored, states = original(case, group)
        memory.search.side_effect = RuntimeError("scripted memory unavailable")
        return memory, stored, states

    monkeypatch.setitem(replay_trust.__globals__, "build_case_memory", failing_memory)
    score = await replay_trust(
        contract_model, {**fixture, "cases": [case]}, 1, settings=strategy_settings("tool")
    )
    turn = score.turns[0]
    assert turn["exception"] is None
    assert turn["recall_called"] is True
    assert "unavailable" in turn["tool_trajectory"][0]["output"]
    assert turn["model_calls"] == 2


async def test_trust_recall_deadline_records_cancelled_attempt_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    original = build_case_memory
    memories: list[Any] = []

    def blocked_memory(case: dict[str, Any], group: str) -> Any:
        memory, stored, states = original(case, group)
        memory.search_gate = asyncio.Event()
        memories.append(memory)
        return memory, stored, states

    monkeypatch.setitem(replay_trust.__globals__, "build_case_memory", blocked_memory)
    settings = strategy_settings("tool")
    settings = settings.model_copy(
        update={
            "memory": settings.memory.model_copy(
                update={
                    "recall": settings.memory.recall.model_copy(update={"timeout_seconds": 0.02})
                }
            )
        }
    )
    score = await replay_trust(
        contract_model, {**fixture, "cases": fixture["cases"][:1]}, 1, settings=settings
    )
    turn = score.turns[0]
    assert turn["exception"] is None
    assert turn["recall_called"] is True
    assert "unavailable" in turn["tool_trajectory"][0]["output"]
    assert turn["tool_trajectory"][0]["exception"].startswith("TimeoutError:")
    assert memories[0].search_cancelled.is_set()


async def test_failed_recall_after_success_records_current_middleware_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    case = fixture["cases"][0]
    case = {**case, "turns": [case["turns"][0], case["turns"][0]]}
    original = build_case_memory

    def second_search_fails(case: dict[str, Any], group: str) -> Any:
        memory, stored, states = original(case, group)
        search = memory.search.side_effect
        attempted = False

        async def search_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal attempted
            if kwargs.get("search_filter") is not None:
                if attempted:
                    raise RuntimeError("second recall failed")
                attempted = True
            return await search(*args, **kwargs)

        memory.search.side_effect = search_once
        return memory, stored, states

    monkeypatch.setitem(replay_trust.__globals__, "build_case_memory", second_search_fails)
    score = await replay_trust(
        contract_model, {**fixture, "cases": [case]}, 1, settings=strategy_settings("tool")
    )
    assert '"facts"' in score.turns[0]["tool_trajectory"][0]["output"]
    assert "unavailable" in score.turns[1]["tool_trajectory"][0]["output"]


async def test_trust_profile_disallows_recall_in_native_prompt_and_toolset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    profile = json.loads((ROOT / fixture["persona"]).read_text())
    profile["allowed_tools"] = []
    (tmp_path / "persona.json").write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setitem(replay_trust.__globals__, "ROOT", tmp_path)
    case = {**fixture["cases"][0], "needs_recall": False}
    score = await replay_trust(
        contract_model, {**fixture, "persona": "persona.json", "cases": [case]}, 1
    )
    assert score.turns[0]["tools_bound"] == []
    assert score.turns[0]["tool_trajectory"] == []
    assert score.prompts[case["id"]]["tools_bound"] == []
    assert "recall_meeting_context" not in score.prompts[case["id"]]["system_prompt"]


async def test_trust_runtime_applies_configured_spoken_cap_and_language() -> None:
    fixture = load_trust_fixture()
    case = {
        **fixture["cases"][0],
        "needs_recall": False,
        "turns": [{"say": "What is the update?", "contract_reply": "First. Second."}],
    }
    settings = Settings()
    settings = settings.model_copy(
        update={
            "conversation": settings.conversation.model_copy(update={"max_spoken_sentences": 1}),
            "speech": settings.speech.model_copy(update={"language": "en"}),
        }
    )
    score = await replay_trust(contract_model, {**fixture, "cases": [case]}, 1, settings=settings)
    assert score.turns[0]["reply"] == "First. Second."
    assert score.turns[0]["spoken"] == "First."
    assert score.runtime["speech_language"] == "en"
    assert score.runtime["max_spoken_sentences"] == 1


def test_candidate_digest_includes_untracked_a_and_excludes_b(tmp_path: Path) -> None:
    from evals.runtime_evidence import capture_candidate, finish_candidate

    for name in (
        "src/fakeyou/brief.py",
        "src/fakeyou/assembly.py",
        "evals/test_brief_eval.py",
        "evals/fixtures/brief-scenarios.json",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial", encoding="utf-8")
    fixture_path = tmp_path / "evals/fixtures/brief-scenarios.json"
    first = capture_candidate(fixture_path, root=tmp_path)
    b = tmp_path / "src/fakeyou/debrief_schema.py"
    b.write_text("ongoing B", encoding="utf-8")
    assert finish_candidate(first, root=tmp_path)["unchanged"] is True
    (tmp_path / "src/fakeyou/brief.py").write_text("revised A", encoding="utf-8")
    finished = finish_candidate(first, root=tmp_path)
    assert finished["unchanged"] is False
    assert finished["start_sha256"] != finished["end_sha256"]
    assert "src/fakeyou/brief.py" in finished["changed_files"]


def test_trust_artifacts_refuse_to_overwrite_existing_raw_evidence(tmp_path: Path) -> None:
    target = tmp_path / "turns.json"
    target.write_text("historical output", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_trust_artifacts(
            tmp_path,
            score=TrustScore(),
            settings=Settings(),
            fixture=load_trust_fixture(),
            fixture_path=FIXTURE,
            source_commit="candidate",
            mode="contract",
            model_name="scripted",
        )
    assert target.read_text(encoding="utf-8") == "historical output"


@pytest.mark.parametrize("marker", [{"skipped": True}, {"verdict": "skip"}, {"mode": []}])
def test_trust_gate_rejects_skips_and_malformed_mode(marker: dict[str, Any]) -> None:
    reviews = passing_trust_reviews({"c01"}, 1)
    reviews[0].update(marker)
    assert not trust_gate(reviews, expected_case_ids={"c01"}, runs=1)


def test_live_trust_review_requires_all_turns_attestation() -> None:
    reviews = passing_trust_reviews({"c01"}, 1)
    reviews[0].update(mode="live", source_ref="turns.json c01 all replies")
    assert not trust_gate(reviews, expected_case_ids={"c01"}, runs=1)
    reviews[0]["checked"] = ["all_turns"]
    assert trust_gate(reviews, expected_case_ids={"c01"}, runs=1)
    reviews[0]["severe"] = True
    reviews[0]["reason"] = "Unrated first response contains unsupported approval."
    assert not trust_gate(reviews, expected_case_ids={"c01"}, runs=1)


async def test_candidate_change_preserves_raw_turns_but_invalidates_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals import runtime_evidence

    tracked = tmp_path / "src/fakeyou/assembly.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("start", encoding="utf-8")
    fixture_path = tmp_path / "evals/fixtures/trust-scenarios.json"
    fixture_path.parent.mkdir(parents=True)
    fixture = load_trust_fixture()
    fixture = {**fixture, "cases": fixture["cases"][:1]}
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setitem(
        replay_trust.__globals__,
        "capture_candidate",
        lambda path: runtime_evidence.capture_candidate(path, root=tmp_path),
    )
    monkeypatch.setitem(
        replay_trust.__globals__,
        "finish_candidate",
        lambda value: runtime_evidence.finish_candidate(value, root=tmp_path),
    )

    def changing_model(case: dict[str, Any]) -> BaseChatModel:
        tracked.write_text("end", encoding="utf-8")
        return contract_model(case)

    score = await replay_trust(changing_model, fixture, 1, fixture_path=fixture_path)
    assert len(score.turns) == 1
    assert score.candidate["unchanged"] is False
    summary = summarize_trust(
        passing_trust_reviews({"c01"}, 1), expected_case_ids={"c01"}, runs=1, score=score
    )
    assert summary["gate_passed"] is False
    write_trust_artifacts(
        tmp_path / "artifacts",
        score=score,
        settings=Settings(),
        fixture=fixture,
        fixture_path=fixture_path,
        source_commit="end-commit",
        mode="contract",
        model_name="scripted",
    )
    manifest = json.loads((tmp_path / "artifacts/manifest.json").read_text())
    assert manifest["candidate"]["unchanged"] is False
    assert manifest["provenance_valid"] is False
    assert manifest["source_commit"] == score.candidate["source_commit"]
    assert len(json.loads((tmp_path / "artifacts/turns.json").read_text())) == 1


@pytest.mark.live
async def test_live_trust_replay_captures_87_rated_pairs(tmp_path: Path) -> None:
    settings = Settings()
    try:
        model = create_chat_model(settings)
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    fixture = load_trust_fixture()
    raw_target = os.environ.get("TRUST_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    score = await replay_trust(
        lambda _case: model,
        fixture,
        TRUST_RUNS,
        settings=settings,
        fallback=create_fallback_chat_model(settings),
    )
    write_trust_artifacts(
        target,
        score=score,
        settings=settings,
        fixture=fixture,
        fixture_path=FIXTURE,
        source_commit=score.candidate["source_commit"],
        mode="live",
        model_name=settings.reply.model,
    )
    print(f"\nlive trust model: {settings.reply.model}")
    for turn in score.rated_turns():
        print(f"  {turn['case_id']} run{turn['run_index']}: {turn['reply']}")
    assert rated_pairs(score) == {
        (case_id, run) for case_id in EXPECTED_CASE_IDS for run in range(TRUST_RUNS)
    }
    assert sum(1 for turn in score.turns if turn.get("exception")) == 0
    assert score.candidate["unchanged"] is True, score.candidate["changed_files"]


async def test_contract_turns_record_token_usage_per_model_call() -> None:
    fixture = load_trust_fixture()
    score = await replay_trust(
        contract_model,
        {**fixture, "cases": fixture["cases"][:1]},
        1,
        settings=strategy_settings("tool"),
    )
    assert len(score.turns[0]["token_usage"]) == score.turns[0]["model_calls"]


async def test_contract_replay_brings_prefetched_memory_to_every_trust_turn() -> None:
    fixture = load_trust_fixture()
    score = await replay_trust(
        partial(contract_model, recall_tool=False),
        fixture,
        1,
        settings=strategy_settings("prefetch"),
    )
    assert all(turn["exception"] is None for turn in score.turns)
    by_id = {case["id"]: case for case in fixture["cases"]}
    for turn in score.turns:
        assert turn["model_calls"] == 1
        assert turn["tools_bound"] == []
        assert len(turn["memory_prefetch"]) == turn["turn_index"] + 1
        assert turn["memory_prefetch"][-1]["query"].endswith(turn["input"])
        assert all(entry["outcome"] in {"found", "empty"} for entry in turn["memory_prefetch"])
        assert turn["recall_called"] is True
        if turn["turn_index"] > 0:
            prior_turn = by_id[turn["case_id"]]["turns"][turn["turn_index"] - 1]
            assert prior_turn["say"] in turn["memory_prefetch"][-1]["query"]
            assert prior_turn["contract_reply"] in turn["memory_prefetch"][-1]["query"]
    for turn in score.rated_turns():
        assert all(turn["aids"][name] for name in AID_CHECKS)
    assert {prompt["recall_strategy"] for prompt in score.prompts.values()} == {"prefetch"}
    assert {prompt["memory_prefetch"] for prompt in score.prompts.values()} == {True}
