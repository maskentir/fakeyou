import hashlib
import json
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.agent import create_meeting_agent
from fakeyou.catalog.errors import FakeYouError
from fakeyou.conversation import LangGraphResponder, TranscriptEvent, cap_spoken_reply
from fakeyou.memory import RECALL_EMPTY_RESULT
from fakeyou.persona import PersonaProfile, build_system_prompt
from fakeyou.providers import create_chat_model
from fakeyou.settings import Settings
from tests.fakes import ToolCallingFakeModel

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
CREDENTIAL_MARKERS = ("key", "token", "secret", "password", "passwd", "auth", "credential")


@dataclass
class ReplayScore:
    checks: dict[str, list[bool]] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, passed: bool) -> None:
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


def recall_tool(facts: list[str]):
    @tool(description="Search long-term meeting memory for relevant context.")
    def recall_meeting_context(query: str) -> str:
        return "\n".join(facts) if facts else RECALL_EMPTY_RESULT

    return recall_meeting_context


def score_turn(
    score: ReplayScore, turn: dict[str, Any], reply: str, recalled: bool
) -> dict[str, bool]:
    spoken = cap_spoken_reply(reply).lower()
    outcome: dict[str, bool] = {}
    outcome["answered"] = bool(spoken.strip())
    score.add("answered", outcome["answered"])
    outcome["within_two_sentences"] = cap_spoken_reply(reply) == reply.strip()
    score.add("within_two_sentences", outcome["within_two_sentences"])
    outcome["no_trailing_question"] = not spoken.rstrip().endswith("?")
    score.add("no_trailing_question", outcome["no_trailing_question"])
    if turn.get("expects_recall"):
        outcome["recall_called"] = recalled
        score.add("recall_called", recalled)
    required = turn.get("must_mention", [])
    if required:
        outcome["must_mention"] = all(phrase in spoken for phrase in required)
        for phrase in required:
            score.add("must_mention", phrase in spoken)
    optional = turn.get("must_mention_any", [])
    if optional:
        outcome["must_mention_any"] = any(phrase in spoken for phrase in optional)
        score.add("must_mention_any", outcome["must_mention_any"])
    forbidden = turn.get("must_not_mention", [])
    if forbidden:
        outcome["must_not_mention"] = all(phrase not in spoken for phrase in forbidden)
        for phrase in forbidden:
            score.add("must_not_mention", phrase not in spoken)
    return outcome


async def replay(model_factory, fixture: dict[str, Any], runs: int) -> ReplayScore:
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
                try:
                    reply = await responder.respond([event], lambda calls=calls: calls.append(None))
                except Exception:
                    reply = ""
                latency = time.monotonic() - started
                score.latencies.append(latency)
                score.replies.append(reply)
                outcome = score_turn(score, turn, reply, bool(calls))
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
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _sanitize_url(value: str) -> str:
    if "://" not in value:
        return value
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return value
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{host}{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        return value


def _is_credential_key(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in CREDENTIAL_MARKERS)


def _sanitize_extra_body(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_credential_key(key):
                cleaned[key] = "**********"
            else:
                cleaned[key] = _sanitize_extra_body(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_extra_body(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_extra_body(item) for item in value)
    if isinstance(value, str) and "://" in value:
        return _sanitize_url(value)
    return value


def _sanitized_role(role: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(role)
    base_url = cleaned.get("base_url")
    if isinstance(base_url, str):
        cleaned["base_url"] = _sanitize_url(base_url)
    for key, item in list(cleaned.items()):
        if key not in ("base_url", "extra_body") and isinstance(item, str) and "://" in item:
            cleaned[key] = _sanitize_url(item)
    extra = cleaned.get("extra_body")
    if extra is not None:
        cleaned["extra_body"] = _sanitize_extra_body(extra)
    return cleaned


def _source_commit() -> str:
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
    reply_manifest = _sanitized_role(reply_raw) if isinstance(reply_raw, dict) else {}
    if isinstance(fallback_raw, dict):
        fallback_manifest: dict[str, Any] | None = _sanitized_role(fallback_raw)
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


async def test_replay_scoring_counts_each_check_offline() -> None:
    fixture = {
        "persona": "examples/persona.example.json",
        "sessions": [
            {
                "meeting_id": "m1",
                "memory": [],
                "turns": [
                    {"say": "When is the beta launch?", "must_mention": ["14 october"]},
                    {"say": "Price?", "must_not_mention": ["million"], "expects_recall": True},
                ],
            }
        ],
    }
    replies = [
        AIMessage(content="The beta launch is on 14 October 2026."),
        AIMessage(content="Five million?"),
    ]

    score = await replay(lambda: ToolCallingFakeModel(responses=replies), fixture, runs=1)

    assert score.rate("must_mention") == 1.0
    assert score.rate("must_not_mention") == 0.0
    assert score.rate("no_trailing_question") == 0.5
    assert score.rate("recall_called") == 0.0
    assert score.summary()["turns"] == 2


def test_replay_fixture_uses_real_session_transcripts() -> None:
    fixture = load_fixture()

    assert [session["meeting_id"] for session in fixture["sessions"]] == [
        "acc-0930-s1",
        "acc-0930-s2",
    ]
    assert sum(len(session["turns"]) for session in fixture["sessions"]) == 8


@pytest.mark.live
async def test_live_replay_meets_quality_and_latency_budgets(tmp_path: Path) -> None:
    settings = Settings()
    try:
        create_chat_model(settings)
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))

    score = await replay(lambda: create_chat_model(settings), load_fixture(), REPLAY_RUNS)
    write_replay_artifacts(
        tmp_path,
        score=score,
        settings=settings,
        fixture_path=FIXTURE,
        source_commit=_source_commit(),
    )

    summary = score.summary()
    print("\nreplay summary:", json.dumps({"model": settings.reply.model, **summary}))
    for reply in score.replies:
        print("  reply:", reply)
    for name in REQUIRED_CHECKS:
        assert score.rate(name) >= REQUIRED_PASS_RATE, (name, summary)
    assert 1 - score.rate("no_trailing_question") <= MAX_TRAILING_QUESTION_RATE, summary
    assert summary["latency_p95_s"] <= REPLY_LATENCY_BUDGET_SECONDS, summary
