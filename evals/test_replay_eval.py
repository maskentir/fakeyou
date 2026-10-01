import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.agent import create_meeting_agent
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


@dataclass
class ReplayScore:
    checks: dict[str, list[bool]] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

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


def score_turn(score: ReplayScore, turn: dict[str, Any], reply: str, recalled: bool) -> None:
    spoken = cap_spoken_reply(reply).lower()
    score.add("answered", bool(spoken.strip()))
    score.add("within_two_sentences", cap_spoken_reply(reply) == reply.strip())
    score.add("no_trailing_question", not spoken.rstrip().endswith("?"))
    if turn.get("expects_recall"):
        score.add("recall_called", recalled)
    for phrase in turn.get("must_mention", []):
        score.add("must_mention", phrase in spoken)
    if turn.get("must_mention_any"):
        score.add("must_mention_any", any(phrase in spoken for phrase in turn["must_mention_any"]))
    for phrase in turn.get("must_not_mention", []):
        score.add("must_not_mention", phrase not in spoken)


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
                )
                started = time.monotonic()
                reply = await responder.respond([event], lambda calls=calls: calls.append(None))
                score.latencies.append(time.monotonic() - started)
                score.replies.append(reply)
                score_turn(score, turn, reply, bool(calls))
    return score


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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
async def test_live_replay_meets_quality_and_latency_budgets() -> None:
    settings = Settings()
    try:
        create_chat_model(settings)
    except ValueError as error:
        pytest.skip(str(error))

    score = await replay(lambda: create_chat_model(settings), load_fixture(), REPLAY_RUNS)

    summary = score.summary()
    print("\nreplay summary:", json.dumps({"model": settings.reply.model, **summary}))
    for reply in score.replies:
        print("  reply:", reply)
    for name in REQUIRED_CHECKS:
        assert score.rate(name) >= REQUIRED_PASS_RATE, (name, summary)
    assert 1 - score.rate("no_trailing_question") <= MAX_TRAILING_QUESTION_RATE, summary
    assert summary["latency_p95_s"] <= REPLY_LATENCY_BUDGET_SECONDS, summary
