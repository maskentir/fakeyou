import hashlib
import json
import statistics
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from evals._shared import sanitized_role
from fakeyou.config.settings import Settings
from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import LangGraphResponder, cap_spoken_reply
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.memory import RECALL_EMPTY_RESULT
from fakeyou.memory.persona import PersonaProfile, build_system_prompt
from fakeyou.observability.faults import capture_error

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "acc-0930-replay.json"
REPLAY_RUNS = 3
REQUIRED_PASS_RATE = 0.9
MAX_TRAILING_QUESTION_RATE = 0.2
REPLY_LATENCY_BUDGET_SECONDS = 4.0
REQUIRED_CHECKS = (
    "answered",
    "recall_called",
    "must_mention",
    "must_mention_any",
    "must_not_mention",
    "within_two_sentences",
)


@dataclass
class ReplayScore:
    checks: dict[str, list[bool]] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, *, passed: bool) -> None:
        self.checks.setdefault(name, []).append(passed)

    def rate(self, name: str) -> float:
        values = self.checks.get(name, [])
        return sum(values) / len(values) if values else 1.0

    def summary(self) -> dict[str, Any]:
        latencies = sorted(self.latencies)
        p95 = (
            statistics.quantiles(latencies, n=20, method="inclusive")[18]
            if len(latencies) > 1
            else (latencies or [0.0])[0]
        )
        return {
            "rates": {name: round(self.rate(name), 3) for name in sorted(self.checks)},
            "latency_p50_s": round(statistics.median(latencies), 3) if latencies else None,
            "latency_p95_s": round(p95, 3),
            "turns": len(self.latencies),
        }


def recall_tool(facts: list[str]) -> BaseTool:
    @tool(description="Search long-term meeting memory for relevant context.")
    def recall_meeting_context(query: str) -> str:
        del query
        return "\n".join(facts) if facts else RECALL_EMPTY_RESULT

    return recall_meeting_context


def score_turn(
    score: ReplayScore, turn: dict[str, Any], reply: str, *, recalled: bool
) -> dict[str, bool]:
    spoken = cap_spoken_reply(reply).lower()
    outcome: dict[str, bool] = {}
    outcome["answered"] = bool(spoken.strip())
    score.add("answered", passed=outcome["answered"])
    outcome["within_two_sentences"] = cap_spoken_reply(reply) == reply.strip()
    score.add("within_two_sentences", passed=outcome["within_two_sentences"])
    outcome["no_trailing_question"] = not spoken.rstrip().endswith("?")
    score.add("no_trailing_question", passed=outcome["no_trailing_question"])
    if turn.get("expects_recall"):
        outcome["recall_called"] = recalled
        score.add("recall_called", passed=recalled)
    required = turn.get("must_mention", [])
    if required:
        outcome["must_mention"] = all(phrase in spoken for phrase in required)
        for phrase in required:
            score.add("must_mention", passed=phrase in spoken)
    optional = turn.get("must_mention_any", [])
    if optional:
        outcome["must_mention_any"] = any(phrase in spoken for phrase in optional)
        score.add("must_mention_any", passed=outcome["must_mention_any"])
    forbidden = turn.get("must_not_mention", [])
    if forbidden:
        outcome["must_not_mention"] = all(phrase not in spoken for phrase in forbidden)
        for phrase in forbidden:
            score.add("must_not_mention", passed=phrase not in spoken)
    return outcome


async def replay(
    model_factory: Callable[[], BaseChatModel], fixture: dict[str, Any], runs: int
) -> ReplayScore:
    profile = PersonaProfile.model_validate_json((ROOT / fixture["persona"]).read_bytes())
    score = ReplayScore()
    for run in range(runs):
        for session in fixture["sessions"]:
            agent = create_meeting_agent(
                model_factory(),
                tools=[recall_tool(session["memory"])],
                persona=build_system_prompt(profile),
                checkpointer=InMemorySaver(),
            )
            responder = LangGraphResponder(agent, f"replay-{run}-{session['meeting_id']}")
            for index, turn in enumerate(session["turns"]):
                calls: list[None] = []
                event = TranscriptEvent(
                    id=f"{session['meeting_id']}-{run}-{index}",
                    timestamp=datetime.now(UTC).isoformat(),
                    source="meeting_audio",
                    text=turn["say"],
                    finalized=True,
                    participant_id="unknown",
                    speaker_evidence_id=None,
                )
                started = time.monotonic()

                def _note_tool_start(calls: list[None] = calls) -> None:
                    calls.append(None)

                answered, _ = await capture_error(
                    partial(responder.respond, [event], _note_tool_start)
                )
                reply = answered or ""
                latency = time.monotonic() - started
                score.latencies.append(latency)
                score.replies.append(reply)
                outcome = score_turn(score, turn, reply, recalled=bool(calls))
                score.turns.append(
                    {
                        "meeting_id": session["meeting_id"],
                        "run_index": run,
                        "turn_index": index,
                        "latency_seconds": latency,
                        "recall_called": bool(calls),
                        "checks": outcome,
                    }
                )
    return score


def load_fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


def source_commit() -> str:
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
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "unknown"


def write_replay_artifacts(
    directory: Path,
    *,
    score: ReplayScore,
    settings: Settings,
    fixture_path: Path,
    source_commit: str,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fixture_sha256 = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    dumped = settings.model_dump(mode="json")
    reply_raw = dumped.get("reply", {})
    fallback_raw = dumped.get("reply_fallback")
    reply_manifest = sanitized_role(reply_raw) if isinstance(reply_raw, dict) else {}
    if isinstance(fallback_raw, dict):
        fallback_manifest: dict[str, Any] | None = sanitized_role(fallback_raw)
    else:
        fallback_manifest = None
    language = dumped.get("speech", {}).get("language", "")
    run_indices = [
        turn.get("run_index", 0) for turn in score.turns if isinstance(turn.get("run_index"), int)
    ]
    runs = (max(run_indices) + 1) if run_indices else 0
    summary = score.summary()
    names = set(score.checks) | set(REQUIRED_CHECKS) | {"no_trailing_question"}
    samples = {name: len(score.checks.get(name, [])) for name in sorted(names)}
    thresholds = {
        "required_pass_rate": REQUIRED_PASS_RATE,
        "max_trailing_question_rate": MAX_TRAILING_QUESTION_RATE,
        "reply_latency_budget_seconds": REPLY_LATENCY_BUDGET_SECONDS,
    }
    gates: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_CHECKS:
        rate = score.rate(name)
        gates[name] = {
            "rate": round(rate, 3),
            "samples": len(score.checks.get(name, [])),
            "passed": rate >= REQUIRED_PASS_RATE,
        }
    trailing_rate = 1 - score.rate("no_trailing_question")
    gates["trailing_question_rate"] = {
        "rate": round(trailing_rate, 3),
        "samples": len(score.checks.get("no_trailing_question", [])),
        "passed": trailing_rate <= MAX_TRAILING_QUESTION_RATE,
    }
    latency_p95 = summary["latency_p95_s"]
    gates["latency_p95_s"] = {
        "value": latency_p95,
        "samples": len(score.latencies),
        "passed": latency_p95 <= REPLY_LATENCY_BUDGET_SECONDS,
    }
    passed = all(item["passed"] for item in gates.values())
    payload = {
        "source_commit": source_commit,
        "fixture_name": fixture_path.name,
        "fixture_sha256": fixture_sha256,
        "runs": runs,
        "turns": len(score.latencies),
        "reply": reply_manifest,
        "reply_fallback": fallback_manifest,
        "language": language,
        "response_timeout_seconds": dumped.get("conversation", {}).get("response_timeout_seconds"),
        "recall_timeout_seconds": dumped.get("memory", {}).get("recall", {}).get("timeout_seconds"),
        "summary": summary,
        "samples": samples,
        "thresholds": thresholds,
        "gates": gates,
        "passed": passed,
    }
    (directory / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "turns.json").write_text(
        json.dumps(score.turns, indent=2, sort_keys=True), encoding="utf-8"
    )
