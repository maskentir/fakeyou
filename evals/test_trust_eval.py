import contextlib
import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

import fakeyou.memory as memory_module
from evals.test_replay_eval import _sanitized_role
from fakeyou.agent import create_meeting_agent
from fakeyou.conversation import LangGraphResponder, TranscriptEvent, cap_spoken_reply
from fakeyou.memory import RECALL_EMPTY_RESULT, create_memory_tools
from fakeyou.persona import PersonaProfile, build_system_prompt
from fakeyou.providers import create_chat_model
from fakeyou.settings import CompatibleReply, Settings
from tests.fakes import ToolCallingFakeModel, make_graphiti, recall_call

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trust-scenarios.json"
TRUST_RUNS = 3
USEFULNESS_THRESHOLD = 0.9
MIN_USEFUL_OF_72 = 65
JUDGE_MODEL = "gpt-4.1-mini"
EXPECTED_CASE_IDS = frozenset(f"c{i:02d}" for i in range(1, 25))
REVIEW_MODES = frozenset({"contract", "live"})
AID_CHECKS = (
    "answered",
    "within_two_sentences",
    "recall_as_needed",
    "allow_present",
    "forbid_absent",
)


def trust_gate(reviews: list[dict[str, object]], *, expected_case_ids: set[str], runs: int) -> bool:
    if not isinstance(reviews, list):
        return False
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
        return False
    if not isinstance(expected_case_ids, (set, frozenset)) or not expected_case_ids:
        return False
    for expected in expected_case_ids:
        if not isinstance(expected, str) or not expected:
            return False
    if len(reviews) != len(expected_case_ids) * runs:
        return False
    seen: set[tuple[str, int]] = set()
    useful = 0
    for review in reviews:
        if not isinstance(review, dict):
            return False
        case_id = review.get("case_id")
        run_index = review.get("run_index")
        severe = review.get("severe")
        positive = review.get("useful")
        reason = review.get("reason")
        if not isinstance(case_id, str) or case_id not in expected_case_ids:
            return False
        if not isinstance(run_index, int) or isinstance(run_index, bool):
            return False
        if run_index < 0 or run_index >= runs:
            return False
        if type(severe) is not bool or type(positive) is not bool:
            return False
        if not isinstance(reason, str) or not reason.strip():
            return False
        mode = review.get("mode", "contract")
        if mode not in REVIEW_MODES:
            return False
        if mode == "live":
            source_ref = review.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref.strip():
                return False
        pair = (case_id, run_index)
        if pair in seen:
            return False
        seen.add(pair)
        if severe:
            return False
        if positive:
            useful += 1
    if len(seen) != len(expected_case_ids) * runs:
        return False
    return useful * 10 >= len(reviews) * 9


@dataclass
class TrustScore:
    turns: list[dict[str, Any]] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    def rated_turns(self) -> list[dict[str, Any]]:
        return [turn for turn in self.turns if turn.get("rated")]

    def latency_summary(self) -> dict[str, Any]:
        ordered = sorted(self.latencies)
        if not ordered:
            return {"p50_s": None, "p95_s": 0.0, "n": 0}
        p95 = (
            statistics.quantiles(ordered, n=20, method="inclusive")[18]
            if len(ordered) > 1
            else ordered[0]
        )
        return {
            "p50_s": round(statistics.median(ordered), 3),
            "p95_s": round(p95, 3),
            "n": len(ordered),
        }

    def aid_rates(self) -> dict[str, Any]:
        rated = self.rated_turns()
        rates: dict[str, Any] = {}
        for name in AID_CHECKS:
            values = [turn["aids"][name] for turn in rated if name in turn.get("aids", {})]
            rates[name] = {
                "rate": round(sum(values) / len(values), 3) if values else None,
                "samples": len(values),
            }
        return rates


def load_trust_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_case_memory(
    case: dict[str, Any], group_id: str
) -> tuple[Any, dict[str, Any], dict[str, str]]:
    spec = case.get("memory", {})
    facts = [str(item) for item in spec.get("facts", [])]
    episodes = {
        str(fact): [str(uuid) for uuid in ids] for fact, ids in spec.get("episodes", {}).items()
    }
    fact_groups = {str(fact): str(group) for fact, group in spec.get("fact_groups", {}).items()}
    memory = make_graphiti(facts=facts, episodes=episodes, fact_groups=fact_groups)
    stored: dict[str, Any] = {}
    for uuid, item in spec.get("stored", {}).items():
        raw_group = str(item["group"])
        resolved = group_id if raw_group == "own" else raw_group
        source_at = datetime.fromisoformat(str(item["source_at"]))
        recorded_at = datetime.fromisoformat(str(item.get("recorded_at", item["source_at"])))
        stored[str(uuid)] = SimpleNamespace(
            uuid=str(uuid),
            group_id=resolved,
            source_description=str(item["label"]),
            valid_at=source_at,
            created_at=recorded_at,
        )
    states = {str(uuid): str(status) for uuid, status in spec.get("states", {}).items()}
    return memory, stored, states


@contextlib.contextmanager
def case_storage(memory: Any, stored: dict[str, Any], states: dict[str, str]):
    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        assert driver is memory.driver
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def scripted_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        assert group_id
        return {uuid: states[uuid] for uuid in episode_uuids if uuid in states}

    with patch.object(memory_module.EpisodicNode, "get_by_uuids", get_by_uuids):
        with patch.object(memory_module, "read_note_states", scripted_states):
            yield


def recording_recall_tool(production: BaseTool, calls: list[dict[str, str]]) -> BaseTool:
    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        output = await production.ainvoke({"query": query})
        calls.append({"query": query, "output": str(output)})
        return str(output)

    return recall_meeting_context


def token_present(token: str, reply: str) -> bool:
    return re.search(r"\b" + re.escape(token) + r"\b", reply, re.IGNORECASE) is not None


def score_aids(case: dict[str, Any], reply: str, recalled: bool) -> dict[str, bool]:
    return {
        "answered": bool(reply.strip()),
        "within_two_sentences": cap_spoken_reply(reply) == reply.strip(),
        "recall_as_needed": recalled if case.get("needs_recall", False) else True,
        "allow_present": all(token_present(str(token), reply) for token in case.get("allow", [])),
        "forbid_absent": all(
            not token_present(str(token), reply) for token in case.get("forbid", [])
        ),
    }


def contract_model(case: dict[str, Any]) -> ToolCallingFakeModel:
    responses: list[AIMessage] = []
    for turn in case["turns"]:
        if case.get("needs_recall", False):
            responses.append(recall_call({"query": str(turn["say"])}))
        responses.append(AIMessage(content=str(turn["contract_reply"])))
    return ToolCallingFakeModel(responses=responses)


async def replay_trust(
    model_factory: Callable[[dict[str, Any]], BaseChatModel],
    fixture: dict[str, Any],
    runs: int,
) -> TrustScore:
    profile = PersonaProfile.model_validate_json((ROOT / fixture["persona"]).read_bytes())
    persona = build_system_prompt(profile)
    group_id = profile.memory_group
    score = TrustScore()
    for case in fixture["cases"]:
        memory, stored, states = build_case_memory(case, group_id)
        with case_storage(memory, stored, states):
            _remember, production = create_memory_tools(memory, group_id=group_id)
            for run in range(runs):
                calls: list[dict[str, str]] = []
                agent = create_meeting_agent(
                    model_factory(case),
                    tools=[recording_recall_tool(production, calls)],
                    persona=persona,
                    checkpointer=InMemorySaver(),
                )
                responder = LangGraphResponder(agent, f"trust-{case['id']}-{run}")
                for index, turn in enumerate(case["turns"]):
                    seen_calls = len(calls)
                    event = TranscriptEvent(
                        id=f"{case['id']}-{run}-{index}",
                        timestamp=datetime.now(UTC).isoformat(),
                        source="meeting_audio",
                        text=str(turn["say"]),
                        finalized=True,
                    )
                    started = time.monotonic()
                    try:
                        reply = await responder.respond([event], lambda: None)
                        error: str | None = None
                    except Exception as exc:
                        reply = ""
                        error = f"{type(exc).__name__}: {exc}"
                    latency = time.monotonic() - started
                    evidence = calls[seen_calls:]
                    score.latencies.append(latency)
                    score.replies.append(reply)
                    rated = index == len(case["turns"]) - 1
                    score.turns.append(
                        {
                            "case_id": case["id"],
                            "family": case["family"],
                            "run_index": run,
                            "turn_index": index,
                            "rated": rated,
                            "input": str(turn["say"]),
                            "reply": reply,
                            "recall_called": bool(evidence),
                            "recall_evidence": evidence,
                            "latency_seconds": latency,
                            "exception": error,
                            "aids": score_aids(case, reply, bool(evidence)) if rated else {},
                        }
                    )
    return score


def rated_pairs(score: TrustScore) -> set[tuple[str, int]]:
    return {(str(turn["case_id"]), int(turn["run_index"])) for turn in score.rated_turns()}


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
    directory.mkdir(parents=True, exist_ok=True)
    dumped = settings.model_dump(mode="json")
    reply_raw = dumped.get("reply", {})
    fallback_raw = dumped.get("reply_fallback")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "model": model_name,
        "source_commit": source_commit,
        "dirty": trust_dirty_flag(),
        "fixture_name": fixture_path.name,
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "persona": str(fixture.get("persona", "")),
        "runs": TRUST_RUNS,
        "cases": len(fixture["cases"]),
        "turns": len(score.turns),
        "rated_pairs": len(rated_pairs(score)),
        "sentence_streaming": dumped.get("conversation", {}).get("sentence_streaming"),
        "thresholds": {
            "usefulness_min": USEFULNESS_THRESHOLD,
            "min_useful_of_72": MIN_USEFUL_OF_72,
            "severe_max": 0,
        },
        "judge": {
            "available": judge_available(),
            "model": JUDGE_MODEL,
            "note": "Independent meaning review is recorded in reviews.json, never self-assessed.",
        },
        "reply": _sanitized_role(reply_raw) if isinstance(reply_raw, dict) else {},
        "reply_fallback": _sanitized_role(fallback_raw) if isinstance(fallback_raw, dict) else None,
        "response_timeout_seconds": dumped.get("conversation", {}).get("response_timeout_seconds"),
        "recall_timeout_seconds": dumped.get("memory", {}).get("recall", {}).get("timeout_seconds"),
        "latency": score.latency_summary(),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "turns.json").write_text(
        json.dumps(score.turns, indent=2, sort_keys=True), encoding="utf-8"
    )


def summarize_trust(
    reviews: list[dict[str, object]],
    *,
    expected_case_ids: set[str],
    runs: int,
    score: TrustScore | None = None,
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
        summary["latency"] = score.latency_summary()
        summary["aids"] = score.aid_rates()
        summary["exceptions"] = sum(1 for turn in score.turns if turn.get("exception"))
    return summary


def write_trust_summary(directory: Path, summary: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )


def passing_trust_reviews(
    case_ids: set[str] | frozenset[str], runs: int
) -> list[dict[str, object]]:
    return [
        {
            "case_id": case_id,
            "run_index": run,
            "severe": False,
            "useful": True,
            "reason": "Matches supplied evidence",
        }
        for run in range(runs)
        for case_id in sorted(case_ids)
    ]


def test_missing_reviews_and_one_severe_violation_fail_gate():
    case_ids = {f"c{i:02d}" for i in range(1, 25)}
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
    case_ids = {f"c{i:02d}" for i in range(1, 25)}
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
    assert trust_gate(malformed, expected_case_ids=case_ids, runs=3)


def test_trust_gate_enforces_usefulness_floor():
    case_ids = {f"c{i:02d}" for i in range(1, 25)}
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
    case_ids = {f"c{i:02d}" for i in range(1, 25)}
    reviews = passing_trust_reviews(case_ids, 3)
    reviews[5]["severe"] = True
    reviews[5]["reason"] = reason
    assert not trust_gate(reviews, expected_case_ids=case_ids, runs=3)


def test_missing_single_review_fails_gate():
    case_ids = {f"c{i:02d}" for i in range(1, 25)}
    reviews = passing_trust_reviews(case_ids, 3)
    assert not trust_gate(reviews[1:], expected_case_ids=case_ids, runs=3)


def test_trust_corpus_defines_24_rated_cases():
    fixture = load_trust_fixture()
    cases = fixture["cases"]
    assert [case["id"] for case in cases] == [f"c{i:02d}" for i in range(1, 25)]
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
    assert families == {"sumber_waktu": 8, "koreksi_gagal": 8, "otoritas_guna": 8}


async def recall_output_for(case: dict[str, Any], group_id: str) -> str:
    memory, stored, states = build_case_memory(case, group_id)
    with case_storage(memory, stored, states):
        _remember, production = create_memory_tools(memory, group_id=group_id)
        return str(await production.ainvoke({"query": str(case["turns"][0]["say"])}))


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


async def test_contract_replay_captures_72_rated_pairs_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    fixture = load_trust_fixture()
    raw_target = os.environ.get("TRUST_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    score = await replay_trust(contract_model, fixture, TRUST_RUNS)
    settings = Settings()
    assert settings.conversation.sentence_streaming is False
    write_trust_artifacts(
        target,
        score=score,
        settings=settings,
        fixture=fixture,
        fixture_path=FIXTURE,
        source_commit=trust_source_commit(),
        mode="contract",
        model_name="ToolCallingFakeModel(contract-scripted)",
    )
    assert len(score.turns) == 75
    assert len(score.rated_turns()) == 72
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
    assert manifest["cases"] == 24
    assert manifest["turns"] == 75
    assert manifest["rated_pairs"] == 72
    assert manifest["sentence_streaming"] is False
    assert manifest["thresholds"] == {
        "usefulness_min": USEFULNESS_THRESHOLD,
        "min_useful_of_72": MIN_USEFUL_OF_72,
        "severe_max": 0,
    }
    assert manifest["judge"]["available"] == judge_available()
    assert manifest["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    recorded = json.loads((target / "turns.json").read_text(encoding="utf-8"))
    assert len(recorded) == 75


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


def test_trust_summary_reports_gate_and_usefulness(tmp_path: Path) -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 25)}
    reviews = passing_trust_reviews(case_ids, 3)
    summary = summarize_trust(reviews, expected_case_ids=case_ids, runs=3)
    assert summary["gate_passed"] is True
    assert summary["usefulness"] == 1.0
    assert summary["contract_only"] is True
    reviews[0]["useful"] = False
    summary = summarize_trust(reviews, expected_case_ids=case_ids, runs=3)
    assert summary["gate_passed"] is True
    assert summary["usefulness"] == round(71 / 72, 3)
    write_trust_summary(tmp_path, summary)
    assert (tmp_path / "summary.json").exists()


@pytest.mark.live
async def test_live_trust_replay_captures_72_rated_pairs(tmp_path: Path) -> None:
    settings = Settings()
    try:
        model = create_chat_model(settings)
    except ValueError as error:
        pytest.skip(str(error))
    fixture = load_trust_fixture()
    raw_target = os.environ.get("TRUST_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    score = await replay_trust(lambda _case: model, fixture, TRUST_RUNS)
    write_trust_artifacts(
        target,
        score=score,
        settings=settings,
        fixture=fixture,
        fixture_path=FIXTURE,
        source_commit=trust_source_commit(),
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
