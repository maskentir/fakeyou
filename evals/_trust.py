import asyncio
import contextlib
import hashlib
import json
import os
import statistics
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override
from unittest.mock import patch

from graphiti_core.nodes import EpisodicNode
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

import fakeyou.memory.memory as memory_module
from evals._shared import (
    REPLAY_EVIDENCE,
    ArtifactMeta,
    ReplayOptions,
    cited_reply,
    sanitized_role,
    token_present,
)
from evals.runtime_evidence import (
    ModelCallCounter,
    ResponderContext,
    capture_candidate,
    finish_candidate,
    native_responder,
    prepare_artifact_directory,
    respond_with_deadline,
    runtime_metadata,
)
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import cap_spoken_reply
from fakeyou.foundation.digest import canonical_json_digest
from fakeyou.foundation.timeparse import require_iso_datetime
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.persona import PersonaProfile, meeting_tool_names, persona_digest
from fakeyou.observability.faults import capture_error
from fakeyou.orchestration.persona_prompt import build_persona_prompt
from tests._conversation_fakes import ToolCallingFakeModel
from tests._memory_fakes import make_graphiti

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trust-scenarios.json"
TRUST_RUNS = 3
USEFULNESS_THRESHOLD = 0.9
MIN_USEFUL_PAIRS = 79
JUDGE_MODEL = "gpt-4.1-mini"
EXPECTED_CASE_IDS = frozenset(f"c{i:02d}" for i in range(1, 30))
REVIEW_MODES = frozenset({"contract", "live"})
AID_CHECKS = (
    "answered",
    "within_two_sentences",
    "recall_as_needed",
    "allow_present",
    "forbid_absent",
)


def _review_identity(
    review: dict[str, object], *, expected_case_ids: set[str] | frozenset[str], runs: int
) -> tuple[str, int] | None:
    verdict = review.get("verdict")
    if review.get("skipped", False) is not False or (
        isinstance(verdict, str) and verdict.strip().lower() in {"skip", "skipped"}
    ):
        return None
    case_id = review.get("case_id")
    run_index = review.get("run_index")
    if not isinstance(case_id, str) or case_id not in expected_case_ids:
        return None
    if not isinstance(run_index, int) or isinstance(run_index, bool):
        return None
    if run_index < 0 or run_index >= runs:
        return None
    return case_id, run_index


def _review_flags(review: dict[str, object]) -> tuple[bool, bool] | None:
    severe = review.get("severe")
    positive = review.get("useful")
    reason = review.get("reason")
    if type(severe) is not bool or type(positive) is not bool:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return severe, positive


def _review_mode_ok(review: dict[str, object]) -> bool:
    mode = review.get("mode", "contract")
    if not isinstance(mode, str) or mode not in REVIEW_MODES:
        return False
    if mode != "live":
        return True
    source_ref = review.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        return False
    checked = review.get("checked")
    return isinstance(checked, list) and "all_turns" in checked


def _checked_review(
    review: object, *, expected_case_ids: set[str] | frozenset[str], runs: int
) -> tuple[tuple[str, int], bool, bool] | None:
    if not isinstance(review, dict):
        return None
    identity = _review_identity(review, expected_case_ids=expected_case_ids, runs=runs)
    if identity is None:
        return None
    flags = _review_flags(review)
    if flags is None or not _review_mode_ok(review):
        return None
    severe, positive = flags
    return identity, severe, positive


def _valid_gate_shape(
    reviews: list[dict[str, object]], *, expected_case_ids: set[str] | frozenset[str], runs: int
) -> bool:
    if not isinstance(reviews, list):
        return False
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
        return False
    if not isinstance(expected_case_ids, (set, frozenset)) or not expected_case_ids:
        return False
    for expected in expected_case_ids:
        if not isinstance(expected, str) or not expected:
            return False
    return len(reviews) == len(expected_case_ids) * runs


def trust_gate(
    reviews: list[dict[str, object]], *, expected_case_ids: set[str] | frozenset[str], runs: int
) -> bool:
    if not _valid_gate_shape(reviews, expected_case_ids=expected_case_ids, runs=runs):
        return False
    seen: set[tuple[str, int]] = set()
    useful = 0
    for review in reviews:
        checked = _checked_review(review, expected_case_ids=expected_case_ids, runs=runs)
        if checked is None:
            return False
        pair, severe, positive = checked
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
    candidate: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, dict[str, Any]] = field(default_factory=dict)

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
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def build_case_memory(
    case: dict[str, Any], group_id: str
) -> tuple[Any, dict[str, Any], dict[str, str]]:
    spec = case.get("memory", {})
    facts = [str(item) for item in spec.get("facts", [])]
    episodes = {
        str(fact): [str(uuid) for uuid in ids] for fact, ids in spec.get("episodes", {}).items()
    }
    fact_groups = {str(fact): str(group) for fact, group in spec.get("fact_groups", {}).items()}
    memory = make_graphiti(
        facts=facts,
        episodes=episodes,
        fact_groups=fact_groups,
        query_sensitive=True,
        match_all=spec.get("match") == "any",
    )
    stored: dict[str, Any] = {}
    for uuid, item in spec.get("stored", {}).items():
        raw_group = str(item["group"])
        resolved = group_id if raw_group == "own" else raw_group
        source_at = require_iso_datetime(str(item["source_at"]))
        recorded_at = require_iso_datetime(str(item.get("recorded_at", item["source_at"])))
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
def case_storage(memory: Any, stored: dict[str, Any], states: dict[str, str]) -> Iterator[None]:
    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        assert driver is memory.driver
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def scripted_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        assert group_id
        return {uuid: states[uuid] for uuid in episode_uuids if uuid in states}

    with patch.object(EpisodicNode, "get_by_uuids", get_by_uuids):
        with patch.object(memory_module, "read_note_states", scripted_states):
            yield


def score_aids(case: dict[str, Any], reply: str, *, recalled: bool) -> dict[str, bool]:
    return {
        "answered": bool(reply.strip()),
        "within_two_sentences": cap_spoken_reply(reply) == reply.strip(),
        "recall_as_needed": recalled if case.get("needs_recall", False) else True,
        "allow_present": all(token_present(str(token), reply) for token in case.get("allow", [])),
        "forbid_absent": all(
            not token_present(str(token), reply) for token in case.get("forbid", [])
        ),
    }


def _contract_content(turn: dict[str, Any]) -> str:
    reply = str(turn["contract_reply"])
    cites = turn.get("contract_cites")
    if cites is None:
        return reply
    return cited_reply(reply, *map(str, cites), language="id")


def contract_model(case: dict[str, Any]) -> ToolCallingFakeModel:
    responses: list[BaseMessage] = [
        AIMessage(content=_contract_content(turn)) for turn in case["turns"]
    ]
    return ToolCallingFakeModel(responses=responses)


async def replay_trust(
    model_factory: Callable[[dict[str, Any]], BaseChatModel],
    fixture: dict[str, Any],
    runs: int,
    options: ReplayOptions | None = None,
) -> TrustScore:
    options = options or ReplayOptions()
    candidate = capture_candidate(options.fixture_path or FIXTURE)
    settings = options.settings if options.settings is not None else Settings()
    fallback = options.fallback
    profile = PersonaProfile.model_validate_json((ROOT / fixture["persona"]).read_bytes())
    candidate["persona_sha256"] = persona_digest(profile)
    candidate["fixture_payload_sha256"] = canonical_json_digest(fixture, compact=True)
    names = meeting_tool_names(profile, None, memory_requested=True)
    prefetch = "recall_meeting_context" in names
    persona = build_persona_prompt(profile.model_copy(update={"allowed_tools": names}))
    group_id = profile.memory_group
    score = TrustScore(runtime=runtime_metadata(settings))
    for case in fixture["cases"]:
        memory, stored, states = build_case_memory(case, group_id)
        with case_storage(memory, stored, states):
            score.prompts[case["id"]] = {
                "system_prompt": persona,
                "prompt_sha256": hashlib.sha256(persona.encode("utf-8")).hexdigest(),
                "memory_prefetch": prefetch,
            }
            for run in range(runs):
                trajectory: list[dict[str, Any]] = []
                counter = ModelCallCounter()
                async with native_responder(
                    model_factory(case),
                    ResponderContext(
                        settings=settings,
                        profile=profile,
                        memory=memory,
                        brief=None,
                        fallback=fallback,
                        counter=counter,
                        trajectory=trajectory,
                        meeting_id=f"trust-eval-{case['id']}-{run}",
                    ),
                ) as responder:
                    for index, turn in enumerate(case["turns"]):
                        seen_tools = len(trajectory)
                        seen_prefetch = len(counter.prefetch)
                        seen_usage = len(counter.usage)
                        seen_calls = counter.calls
                        event = TranscriptEvent(
                            id=f"{case['id']}-{run}-{index}",
                            timestamp=datetime.now(UTC).isoformat(),
                            source="meeting_audio",
                            text=str(turn["say"]),
                            finalized=True,
                            participant_id="unknown",
                            speaker_evidence_id=None,
                            evidence_needs=REPLAY_EVIDENCE,
                        )
                        started = time.monotonic()
                        answered, failure = await capture_error(
                            partial(respond_with_deadline, responder, event, settings)
                        )
                        error: str | None = None
                        reply = answered or ""
                        if failure is not None:
                            reply = ""
                            error = f"{type(failure).__name__}: {failure}"
                        latency = time.monotonic() - started
                        calls = trajectory[seen_tools:]
                        evidence = [
                            call for call in calls if call["name"] == "recall_meeting_context"
                        ]
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
                                "spoken": cap_spoken_reply(
                                    reply,
                                    settings.conversation.max_spoken_sentences,
                                    settings.speech.language,
                                ),
                                "tool_trajectory": calls,
                                "memory_prefetch": counter.prefetch[seen_prefetch:],
                                "token_usage": counter.usage[seen_usage:],
                                "model_calls": counter.calls - seen_calls,
                                "recall_called": bool(evidence or counter.prefetch[seen_prefetch:]),
                                "recall_evidence": evidence,
                                "latency_seconds": latency,
                                "exception": error,
                                "aids": score_aids(
                                    case,
                                    reply,
                                    recalled=bool(evidence or counter.prefetch[seen_prefetch:]),
                                )
                                if rated
                                else {},
                            }
                        )
    score.candidate = finish_candidate(candidate)
    return score


def rated_pairs(score: TrustScore) -> set[tuple[str, int]]:
    return {(str(turn["case_id"]), int(turn["run_index"])) for turn in score.rated_turns()}


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


class EvalDelayedModel(ToolCallingFakeModel):
    cancelled: bool = False

    @override
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super()._agenerate(*args, **kwargs)


def judge_available() -> bool:
    return bool(os.environ.get("EVAL_JUDGE_API_KEY", "").strip())


def write_trust_artifacts(directory: Path, *, score: TrustScore, meta: ArtifactMeta) -> None:
    prepare_artifact_directory(directory)
    dumped = meta.settings.model_dump(mode="json")
    reply_raw = dumped.get("reply", {})
    fallback_raw = dumped.get("reply_fallback")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": meta.mode,
        "model": meta.model_name,
        "source_commit": score.candidate.get("source_commit", meta.source_commit),
        "dirty": score.candidate.get("dirty_at_start"),
        "candidate": score.candidate,
        "provenance_valid": score.candidate.get("unchanged") is True
        and score.runtime == runtime_metadata(meta.settings),
        "runtime": score.runtime,
        "fixture_name": meta.fixture_path.name,
        "fixture_sha256": score.candidate.get("fixture_sha256"),
        "persona": str(meta.fixture.get("persona", "")),
        "persona_sha256": score.candidate.get("persona_sha256"),
        "fixture_payload_sha256": score.candidate.get("fixture_payload_sha256"),
        "runs": len({turn["run_index"] for turn in score.turns}),
        "cases": len(meta.fixture["cases"]),
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
        "recall_calls": sum(
            len(turn.get("recall_evidence", [])) + len(turn.get("memory_prefetch", []))
            for turn in score.turns
        ),
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
    pairs: set[tuple[str, int]] = set()
    for review in reviews:
        case_id = review.get("case_id")
        run_index = review.get("run_index")
        if isinstance(case_id, str) and isinstance(run_index, int):
            pairs.add((case_id, run_index))
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
