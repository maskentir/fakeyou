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
from fakeyou.persistence.owner_model import FrozenOwnerModel
from tests._conversation_fakes import ToolCallingFakeModel
from tests._conversation_support import claim, plan_reply

BETA_OWNER_MODEL = {
    "version": 1,
    "items": [
        {
            "item_id": "beta",
            "kind": "fact",
            "topic": "imported fact",
            "statement": "The Orchid beta launch is planned for 14 October 2026",
            "confidence": "firm",
            "disclosure": "room_speakable",
        }
    ],
}


async def test_replay_scoring_counts_each_check_offline() -> None:
    fixture = {
        "persona": "examples/persona.example.json",
        "owner_model": BETA_OWNER_MODEL,
        "sessions": [
            {
                "meeting_id": "m1",
                "memory": [],
                "turns": [
                    {"say": "When is the beta launch?", "must_mention": ["14 october"]},
                    {"say": "Price?", "must_not_mention": ["million"]},
                ],
            }
        ],
    }
    replies: list[BaseMessage] = [
        AIMessage(content=plan_reply(claim("The beta launch is on 14 October 2026.", "om-beta"))),
        AIMessage(content="Five million?"),
    ]

    score = await replay(lambda: ToolCallingFakeModel(responses=replies), fixture, runs=1)

    assert score.rate("must_mention") == 1.0
    assert score.rate("must_not_mention") == 0.0
    assert score.rate("no_trailing_question") == 0.5
    assert score.summary()["turns"] == 2


async def test_replay_speaks_an_owner_position_and_never_an_owner_only_item() -> None:
    position = "Ship after the load test passes."
    private = "My salary floor is 90 thousand."
    fixture = {
        "persona": "examples/persona.example.json",
        "owner_model": {
            "version": 2,
            "items": [
                {
                    "item_id": "ship",
                    "kind": "position",
                    "topic": "release",
                    "statement": position,
                    "confidence": "firm",
                    "disclosure": "room_speakable",
                },
                {
                    "item_id": "salary",
                    "kind": "fact",
                    "topic": "compensation",
                    "statement": private,
                    "confidence": "firm",
                    "disclosure": "owner_only",
                },
            ],
        },
        "sessions": [
            {
                "meeting_id": "m1",
                "memory": [],
                "turns": [
                    {"say": "When should we ship?", "must_mention": ["load test"]},
                    {"say": "What is the salary floor?", "must_not_mention": ["90"]},
                ],
            }
        ],
    }
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content=plan_reply(claim("Rani wants to ship after the load test.", "om-ship"))
            ),
            AIMessage(
                content=plan_reply(claim("Rani's salary floor is 90 thousand.", "om-salary"))
            ),
        ]
    )

    score = await replay(lambda: model, fixture, runs=1)

    prompt = str(model.seen[0][0].content)
    assert '"id": "om-ship"' in prompt
    assert position in prompt
    assert private not in prompt
    assert "om-salary" not in prompt
    assert score.replies[0] == "Rani wants to ship after the load test."
    assert score.rate("must_mention") == 1.0
    assert score.rate("must_not_mention") == 1.0


async def test_streaming_replay_scores_ordered_sentences_offline() -> None:
    fixture = {
        "persona": "examples/persona.example.json",
        "owner_model": BETA_OWNER_MODEL,
        "sessions": [
            {
                "meeting_id": "m1",
                "memory": [],
                "turns": [
                    {"say": "When is the beta launch?", "must_mention": ["14 october"]},
                ],
            }
        ],
    }
    replies: list[BaseMessage] = [
        AIMessage(
            content=plan_reply(
                claim("The beta launch is on 14 October 2026. See you there.", "om-beta")
            )
        )
    ]

    score = await replay(
        lambda: ToolCallingFakeModel(responses=replies), fixture, runs=1, streaming=True
    )

    assert score.rate("must_mention") == 1.0
    assert score.rate("within_two_sentences") == 1.0
    assert len(score.first_sentence_latencies) == 1
    assert score.first_sentence_latencies[0] <= score.latencies[0]
    assert score.summary()["first_sentence_p95_s"] is not None
    assert "14 October 2026" in score.replies[0]


def test_replay_fixture_uses_real_session_transcripts() -> None:
    fixture = load_fixture()

    assert [session["meeting_id"] for session in fixture["sessions"]] == [
        "acc-0930-s1",
        "acc-0930-s2",
    ]
    assert sum(len(session["turns"]) for session in fixture["sessions"]) == 8
    owner_model = FrozenOwnerModel.model_validate(fixture["owner_model"])
    assert [item.ref for item in owner_model.items] == [
        "om-orchid-beta-launch",
        "om-orchid-checklist",
        "om-budget-approval",
        "om-weekly-sync",
        "om-open-questions",
        "om-orchid-demo",
    ]
    for session in fixture["sessions"]:
        assert all(fact.startswith("From Google Meet ") for fact in session["memory"])


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
