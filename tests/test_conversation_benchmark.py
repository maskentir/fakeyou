import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from evals.test_replay_eval import (
    FIXTURE,
    ReplayScore,
    load_fixture,
    replay,
    write_replay_artifacts,
)
from fakeyou.settings import CompatibleReply, Settings
from tests.fakes import ToolCallingFakeModel, recall_call


class StepClock:
    def __init__(self, start: float, step: float) -> None:
        self.now = start
        self.step = step

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value


def test_replay_artifacts_mask_role_credentials(tmp_path: Path, settings: Settings) -> None:
    score = ReplayScore()
    score.add("answered", True)
    score.latencies.append(1.0)
    score.turns.append(
        {
            "meeting_id": "fixture",
            "run_index": 0,
            "turn_index": 0,
            "latency_seconds": 1.0,
            "recall_called": False,
            "checks": {"answered": True},
        }
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"sessions": []}')
    write_replay_artifacts(
        tmp_path,
        score=score,
        settings=settings,
        fixture_path=fixture,
        source_commit="test-commit",
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["source_commit"] == "test-commit"
    assert summary["reply"]["api_key"] == "**********"
    assert settings.reply.api_key.get_secret_value() not in (tmp_path / "summary.json").read_text()
    assert (tmp_path / "turns.json").exists()


def test_replay_artifacts_sanitize_url_and_extra_body(tmp_path: Path, settings: Settings) -> None:
    marker_url = "MARKER_URL_SECRET"
    marker_body = "MARKER_BODY_SECRET"
    marked = CompatibleReply(
        base_url=f"https://user:{marker_url}@example.com:8000/v1?token={marker_url}#frag",
        model="marked-model",
        api_key=SecretStr("marked-key"),
        extra_body={
            "api_key": marker_body,
            "nested": {"auth_token": marker_body, "safe": "keep"},
            "reasoning_split": True,
        },
    )
    marked_settings = settings.model_copy(update={"reply": marked})
    score = ReplayScore()
    score.add("answered", True)
    score.latencies.append(0.5)
    score.turns.append(
        {
            "meeting_id": "fixture",
            "run_index": 0,
            "turn_index": 0,
            "latency_seconds": 0.5,
            "recall_called": False,
            "checks": {"answered": True},
        }
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"sessions": []}')
    write_replay_artifacts(
        tmp_path,
        score=score,
        settings=marked_settings,
        fixture_path=fixture,
        source_commit="test-commit",
    )
    text = (tmp_path / "summary.json").read_text()
    summary = json.loads(text)
    assert marker_url not in text
    assert marker_body not in text
    assert "marked-key" not in text
    assert summary["reply"]["base_url"] == "https://example.com:8000/v1"
    assert summary["reply"]["extra_body"]["api_key"] == "**********"
    assert summary["reply"]["extra_body"]["nested"]["auth_token"] == "**********"
    assert summary["reply"]["extra_body"]["nested"]["safe"] == "keep"
    assert summary["reply"]["extra_body"]["reasoning_split"] is True


def test_replay_artifact_records_sample_counts_and_fixture_hash(
    tmp_path: Path, settings: Settings
) -> None:
    score = ReplayScore()
    score.add("answered", True)
    score.add("answered", False)
    score.add("must_mention", True)
    score.latencies.extend([0.4, 0.6])
    score.turns.extend(
        [
            {
                "meeting_id": "fixture",
                "run_index": 0,
                "turn_index": 0,
                "latency_seconds": 0.4,
                "recall_called": False,
                "checks": {"answered": True},
            },
            {
                "meeting_id": "fixture",
                "run_index": 0,
                "turn_index": 1,
                "latency_seconds": 0.6,
                "recall_called": False,
                "checks": {"answered": False},
            },
        ]
    )
    fixture = tmp_path / "fixture.json"
    payload = '{"sessions": [{"meeting_id": "fixture"}]}'
    fixture.write_text(payload)
    write_replay_artifacts(
        tmp_path,
        score=score,
        settings=settings,
        fixture_path=fixture,
        source_commit="test-commit",
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["fixture_sha256"] == hashlib.sha256(payload.encode()).hexdigest()
    assert summary["samples"]["answered"] == 2
    assert summary["samples"]["must_mention"] == 1
    assert summary["samples"]["recall_called"] == 0
    assert summary["gates"]["answered"]["samples"] == 2
    assert summary["runs"] == 1
    assert summary["turns"] == 2
    turns = json.loads((tmp_path / "turns.json").read_text())
    assert len(turns) == 2


async def test_replay_records_per_turn_results_with_controlled_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evals.test_replay_eval as replay_module

    fixture = load_fixture()
    expected_turns = sum(len(session["turns"]) for session in fixture["sessions"])
    monkeypatch.setattr(replay_module, "time", StepClock(1000.0, 0.5))
    replies = [AIMessage(content=f"Plain reply number {index}.") for index in range(8)]
    score = await replay(lambda: ToolCallingFakeModel(responses=list(replies)), fixture, runs=1)
    assert len(score.latencies) == expected_turns
    assert len(score.turns) == expected_turns
    assert all(latency == 0.5 for latency in score.latencies)
    first = score.turns[0]
    assert first["meeting_id"] == fixture["sessions"][0]["meeting_id"]
    assert first["run_index"] == 0
    assert first["turn_index"] == 0
    assert first["latency_seconds"] == 0.5
    assert first["recall_called"] is False
    assert first["checks"]["answered"] is True
    assert score.summary()["turns"] == expected_turns


async def test_replay_counts_stable_when_reply_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import evals.test_replay_eval as replay_module

    monkeypatch.setattr(replay_module, "time", StepClock(2000.0, 0.25))
    fixture: dict[str, Any] = {
        "persona": "examples/persona.example.json",
        "sessions": [
            {
                "meeting_id": "stable",
                "memory": [],
                "turns": [{"say": "Alpha prompt"}, {"say": "Beta prompt"}],
            }
        ],
    }
    score = await replay(
        lambda: ToolCallingFakeModel(
            responses=[AIMessage(content=""), AIMessage(content="Valid reply here.")]
        ),
        fixture,
        runs=1,
    )
    assert len(score.latencies) == 2
    assert len(score.turns) == 2
    assert len(score.replies) == 2
    assert score.turns[0]["checks"]["answered"] is False
    assert score.turns[1]["checks"]["answered"] is True
    assert score.rate("answered") == 0.5


async def test_replay_counts_stable_when_recall_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_core.tools import tool

    import evals.test_replay_eval as replay_module

    def failing_tool(facts: list[str]) -> Any:
        @tool(description="Search long-term meeting memory for relevant context.")
        def recall_meeting_context(query: str) -> str:
            raise RuntimeError("recall unavailable")

        return recall_meeting_context

    monkeypatch.setattr(replay_module, "recall_tool", failing_tool)
    monkeypatch.setattr(replay_module, "time", StepClock(3000.0, 0.25))
    fixture: dict[str, Any] = {
        "persona": "examples/persona.example.json",
        "sessions": [
            {
                "meeting_id": "stable",
                "memory": ["Some prior fact."],
                "turns": [
                    {"say": "Alpha prompt", "expects_recall": True},
                    {"say": "Beta prompt"},
                ],
            }
        ],
    }
    score = await replay(
        lambda: ToolCallingFakeModel(
            responses=[
                recall_call({"query": "alpha"}),
                AIMessage(content="Fallback reply here."),
                AIMessage(content="Second reply here."),
            ]
        ),
        fixture,
        runs=1,
    )
    assert len(score.latencies) == 2
    assert len(score.turns) == 2
    assert len(score.replies) == 2
    assert score.turns[0]["recall_called"] is True
    assert FIXTURE.exists()
