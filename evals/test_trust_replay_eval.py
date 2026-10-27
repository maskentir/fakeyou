import hashlib
import json
import os
from pathlib import Path

import pytest

from evals._shared import ArtifactMeta, ReplayOptions
from evals._trust import (
    AID_CHECKS,
    EXPECTED_CASE_IDS,
    FIXTURE,
    MIN_USEFUL_PAIRS,
    TRUST_RUNS,
    USEFULNESS_THRESHOLD,
    contract_model,
    judge_available,
    load_trust_fixture,
    rated_pairs,
    replay_trust,
    score_aids,
    write_trust_artifacts,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.orchestration.providers import create_chat_model, create_fallback_chat_model


async def test_contract_replay_captures_87_rated_pairs_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    fixture = load_trust_fixture()
    raw_target = os.environ.get("TRUST_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    _base_settings = Settings()
    settings = _base_settings.model_copy(
        update={
            "conversation": _base_settings.conversation.model_copy(
                update={"sentence_streaming": False}
            )
        }
    )
    score = await replay_trust(
        contract_model, fixture, TRUST_RUNS, options=ReplayOptions(settings=settings)
    )
    assert settings.conversation.sentence_streaming is False
    write_trust_artifacts(
        target,
        score=score,
        meta=ArtifactMeta(
            settings=settings,
            fixture=fixture,
            fixture_path=FIXTURE,
            source_commit=score.candidate["source_commit"],
            mode="contract",
            model_name="ToolCallingFakeModel(contract-scripted)",
        ),
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
        assert turn["recall_called"] is True
        assert turn["aids"] == score_aids(case, turn["reply"], recalled=turn["recall_called"])
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
        options=ReplayOptions(settings=settings, fallback=create_fallback_chat_model(settings)),
    )
    write_trust_artifacts(
        target,
        score=score,
        meta=ArtifactMeta(
            settings=settings,
            fixture=fixture,
            fixture_path=FIXTURE,
            source_commit=score.candidate["source_commit"],
            mode="live",
            model_name=settings.reply.model,
        ),
    )
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
        options=ReplayOptions(settings=Settings()),
    )
    assert len(score.turns[0]["token_usage"]) == score.turns[0]["model_calls"]


async def test_contract_replay_brings_prefetched_memory_to_every_trust_turn() -> None:
    fixture = load_trust_fixture()
    score = await replay_trust(
        contract_model,
        fixture,
        1,
        options=ReplayOptions(settings=Settings()),
    )
    assert all(turn["exception"] is None for turn in score.turns)
    by_id = {case["id"]: case for case in fixture["cases"]}
    for turn in score.turns:
        assert turn["model_calls"] == 1
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
    assert {prompt["memory_prefetch"] for prompt in score.prompts.values()} == {True}
