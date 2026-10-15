from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from evals._replay import (
    FIXTURE,
    MAX_TRAILING_QUESTION_RATE,
    REPLAY_RUNS,
    REPLY_LATENCY_BUDGET_SECONDS,
    REQUIRED_CHECKS,
    REQUIRED_PASS_RATE,
    load_fixture,
    replay,
    source_commit,
    write_replay_artifacts,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.orchestration.providers import create_chat_model
from tests._conversation_fakes import ToolCallingFakeModel


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
    replies: list[BaseMessage] = [
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
        source_commit=source_commit(),
    )

    summary = score.summary()
    for name in REQUIRED_CHECKS:
        assert score.rate(name) >= REQUIRED_PASS_RATE, (name, summary)
    assert 1 - score.rate("no_trailing_question") <= MAX_TRAILING_QUESTION_RATE, summary
    assert summary["latency_p95_s"] <= REPLY_LATENCY_BUDGET_SECONDS, summary
