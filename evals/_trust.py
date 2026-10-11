import asyncio
import contextlib
import hashlib
import json
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

import fakeyou.memory.memory as memory_module
from evals._shared import token_present
from evals.runtime_evidence import (
    ModelCallCounter,
    capture_candidate,
    finish_candidate,
    native_responder,
    respond_with_deadline,
    runtime_metadata,
)
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import cap_spoken_reply
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.persona import (
    PersonaProfile,
    build_system_prompt,
    meeting_tool_names,
    memory_wiring,
    persona_digest,
)
from tests.fakes import ToolCallingFakeModel, make_graphiti, recall_call

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trust-scenarios.json"
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
        verdict = review.get("verdict")
        if review.get("skipped", False) is not False or (
            isinstance(verdict, str) and verdict.strip().lower() in {"skip", "skipped"}
        ):
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
        if not isinstance(mode, str) or mode not in REVIEW_MODES:
            return False
        if mode == "live":
            source_ref = review.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref.strip():
                return False
            checked = review.get("checked")
            if not isinstance(checked, list) or "all_turns" not in checked:
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


def contract_model(case: dict[str, Any], *, recall_tool: bool = True) -> ToolCallingFakeModel:
    responses: list[AIMessage] = []
    for turn in case["turns"]:
        if recall_tool and case.get("needs_recall", False):
            responses.append(recall_call({"query": str(turn["say"])}))
        responses.append(AIMessage(content=str(turn["contract_reply"])))
    return ToolCallingFakeModel(responses=responses)


async def replay_trust(
    model_factory: Callable[[dict[str, Any]], BaseChatModel],
    fixture: dict[str, Any],
    runs: int,
    *,
    settings: Settings | None = None,
    fallback: BaseChatModel | None = None,
    fixture_path: Path = FIXTURE,
) -> TrustScore:
    candidate = capture_candidate(fixture_path)
    settings = settings if settings is not None else Settings()
    profile = PersonaProfile.model_validate_json((ROOT / fixture["persona"]).read_bytes())
    candidate["persona_sha256"] = persona_digest(profile)
    candidate["fixture_payload_sha256"] = hashlib.sha256(
        json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    names = meeting_tool_names(profile, None, memory_requested=True)
    strategy = settings.memory.recall.strategy
    bound, prefetch = memory_wiring(names, strategy)
    persona = build_system_prompt(
        profile.model_copy(update={"allowed_tools": names}), recall_strategy=strategy
    )
    group_id = profile.memory_group
    score = TrustScore(runtime=runtime_metadata(settings))
    for case in fixture["cases"]:
        memory, stored, states = build_case_memory(case, group_id)
        with case_storage(memory, stored, states):
            score.prompts[case["id"]] = {
                "system_prompt": persona,
                "prompt_sha256": hashlib.sha256(persona.encode("utf-8")).hexdigest(),
                "tools_bound": sorted(bound),
                "recall_strategy": strategy,
                "memory_prefetch": prefetch,
            }
            for run in range(runs):
                trajectory: list[dict[str, Any]] = []
                counter = ModelCallCounter()
                async with native_responder(
                    model_factory(case),
                    settings=settings,
                    profile=profile,
                    memory=memory,
                    brief=None,
                    fallback=fallback,
                    counter=counter,
                    trajectory=trajectory,
                    meeting_id=f"trust-eval-{case['id']}-{run}",
                ) as responder:
                    for index, turn in enumerate(case["turns"]):
                        seen_tools = len(trajectory)
                        seen_results = len(counter.tool_results)
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
                        )
                        started = time.monotonic()
                        try:
                            reply = await respond_with_deadline(responder, event, settings)
                            error: str | None = None
                        except Exception as exc:
                            reply = ""
                            error = f"{type(exc).__name__}: {exc}"
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
                                "tools_bound": sorted(bound),
                                "tool_trajectory": calls,
                                "tool_results": counter.tool_results[seen_results:],
                                "memory_prefetch": counter.prefetch[seen_prefetch:],
                                "token_usage": counter.usage[seen_usage:],
                                "model_calls": counter.calls - seen_calls,
                                "recall_called": bool(evidence or counter.prefetch[seen_prefetch:]),
                                "recall_evidence": evidence,
                                "latency_seconds": latency,
                                "exception": error,
                                "aids": score_aids(
                                    case, reply, bool(evidence or counter.prefetch[seen_prefetch:])
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

    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super()._agenerate(*args, **kwargs)


def strategy_settings(strategy: str) -> Settings:
    settings = Settings()
    recall = settings.memory.recall.model_copy(update={"strategy": strategy})
    return settings.model_copy(
        update={"memory": settings.memory.model_copy(update={"recall": recall})}
    )
