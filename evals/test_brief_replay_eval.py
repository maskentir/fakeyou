import hashlib
import json
from functools import partial
from pathlib import Path

import pytest

from evals._brief import (
    AID_CHECKS,
    BRIEF_RUNS,
    EXPECTED_CASE_IDS,
    EXPECTED_FAMILIES,
    EXPECTED_OUTCOMES,
    FIXTURE,
    MIN_USEFUL_OF_36,
    REQUIRED_REVIEW_CHECKS,
    USEFULNESS_THRESHOLD,
    brief_artifact_dir,
    case_brief,
    fixture_profile,
    load_brief_fixture,
    replay_briefs,
    score_brief_aids,
    write_brief_artifacts,
)
from evals._shared import ArtifactMeta, ReplayOptions, suspended_gc
from evals._trust import (
    contract_model,
    load_trust_fixture,
    rated_pairs,
    strategy_settings,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import cap_spoken_reply
from fakeyou.memory.evidence import RECALL_EMPTY_RESULT
from fakeyou.memory.persona import persona_digest
from fakeyou.orchestration.providers import create_chat_model, create_fallback_chat_model
from fakeyou.persistence.brief import brief_digest


def test_brief_corpus_defines_12_categories() -> None:
    fixture = load_brief_fixture()
    profile = fixture_profile(fixture)
    cases = fixture["cases"]
    assert [case["id"] for case in cases] == sorted(EXPECTED_CASE_IDS)
    assert [case["family"] for case in cases] == list(EXPECTED_FAMILIES)
    assert len(set(EXPECTED_FAMILIES)) == 12
    markers = [str(marker) for marker in fixture["persona_markers"]]
    persona_text = " ".join([*profile.goals, *profile.verified_facts])
    assert markers
    assert all(marker in persona_text for marker in markers)
    for case in cases:
        brief = case_brief(fixture, case)
        assert (brief.owner_id, brief.twin_id) == (profile.owner_id, profile.twin_id)
        assert brief.meeting_id == f"brief-eval-{case['id']}"
        brief_text = brief.model_dump_json()
        assert not any(marker.lower() in brief_text.lower() for marker in markers)
        assert case["expect"] in EXPECTED_OUTCOMES
        assert type(case["memory_requested"]) is bool
        assert type(case["needs_recall"]) is bool
        if brief.knowledge_scope == "brief_only":
            assert case["memory_requested"] is False
        if case["needs_recall"]:
            assert case["memory_requested"] is True
        assert case["rubric"].strip()
        assert case["severe_if"].strip()
        assert set(case["aids"]) == {"allow", "forbid"}
        for token in [*case["aids"]["allow"], *case["aids"]["forbid"]]:
            assert isinstance(token, str)
            assert token.strip()
        assert case["turns"]
        for turn in case["turns"]:
            assert turn["say"].strip()
            assert turn["contract_reply"].strip()
            assert cap_spoken_reply(turn["contract_reply"]) == turn["contract_reply"].strip()
        final = str(case["turns"][-1]["contract_reply"])
        aids = score_brief_aids(case, final, [], [])
        assert all(aids.values()), (case["id"], aids)
    scopes = {case_brief(fixture, case).knowledge_scope for case in cases}
    assert scopes == {"brief_only", "persona_memory"}
    assert {case["expect"] for case in cases} == EXPECTED_OUTCOMES


async def test_contract_brief_replay_captures_36_rated_pairs_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    fixture = load_brief_fixture()
    profile = fixture_profile(fixture)
    target = brief_artifact_dir(tmp_path)
    settings = strategy_settings("tool").model_copy(
        update={
            "conversation": strategy_settings("tool").conversation.model_copy(
                update={"sentence_streaming": False}
            )
        }
    )
    score = await replay_briefs(
        contract_model, fixture, BRIEF_RUNS, options=ReplayOptions(settings=settings)
    )
    assert settings.conversation.sentence_streaming is False
    write_brief_artifacts(
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
    turns_per_run = sum(len(case["turns"]) for case in fixture["cases"])
    assert len(score.turns) == turns_per_run * BRIEF_RUNS == 42
    assert len(score.rated_turns()) == 36
    assert rated_pairs(score) == {
        (case_id, run) for case_id in EXPECTED_CASE_IDS for run in range(BRIEF_RUNS)
    }
    assert all(turn["exception"] is None for turn in score.turns)
    assert all(turn["latency_seconds"] >= 0 for turn in score.turns)
    by_id = {case["id"]: case for case in fixture["cases"]}
    briefs = {case["id"]: case_brief(fixture, case) for case in fixture["cases"]}
    for turn in score.turns:
        case = by_id[turn["case_id"]]
        assert "remember_meeting_fact" not in turn["tools_bound"]
        assert turn["model_calls"] == (2 if case["needs_recall"] else 1)
        assert turn["spoken"] == turn["reply"]
        if briefs[turn["case_id"]].knowledge_scope == "brief_only":
            assert turn["tools_bound"] == []
            assert turn["tool_trajectory"] == []
        if case["needs_recall"]:
            assert turn["tools_bound"] == ["recall_meeting_context"]
            assert [call["name"] for call in turn["tool_trajectory"]] == ["recall_meeting_context"]
            assert turn["tool_trajectory"][0]["output"] == RECALL_EMPTY_RESULT
    for turn in score.rated_turns():
        case = by_id[turn["case_id"]]
        assert turn["reply"] == case["turns"][-1]["contract_reply"]
        assert turn["aids"] == score_brief_aids(
            case, turn["reply"], turn["tools_bound"], turn["tool_trajectory"]
        )
        assert all(turn["aids"][name] for name in AID_CHECKS)
    markers = [str(marker).lower() for marker in fixture["persona_markers"]]
    prompts = json.loads((target / "prompts.json").read_text(encoding="utf-8"))
    assert set(prompts) == set(EXPECTED_CASE_IDS)
    for case_id, entry in prompts.items():
        brief = briefs[case_id]
        assert entry["knowledge_scope"] == brief.knowledge_scope
        assert entry["permissions"] == sorted(brief.permissions)
        assert entry["brief_sha256"] == brief_digest(brief)
        assert (
            entry["prompt_sha256"]
            == hashlib.sha256(entry["system_prompt"].encode("utf-8")).hexdigest()
        )
        if brief.knowledge_scope == "brief_only":
            leaked = [marker for marker in markers if marker in entry["system_prompt"].lower()]
            assert leaked == [], case_id
    text = (target / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert manifest["mode"] == "contract"
    assert manifest["runs"] == BRIEF_RUNS
    assert manifest["cases"] == 12
    assert manifest["turns"] == 42
    assert manifest["rated_pairs"] == 36
    assert manifest["sentence_streaming"] is False
    assert manifest["thresholds"] == {
        "usefulness_min": USEFULNESS_THRESHOLD,
        "min_useful_of_36": MIN_USEFUL_OF_36,
        "severe_max": 0,
    }
    assert manifest["review"]["required_checks"] == list(REQUIRED_REVIEW_CHECKS)
    assert manifest["fixture_name"] == FIXTURE.name
    assert manifest["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert manifest["persona_sha256"] == persona_digest(profile)
    assert manifest["brief_sha256"] == {
        case_id: brief_digest(brief) for case_id, brief in briefs.items()
    }
    assert manifest["model_calls"] == score.model_calls() == 45
    assert manifest["recall_calls"] == score.recall_calls() == BRIEF_RUNS
    assert manifest["latency"]["n"] == 42
    secrets = [
        role.api_key.get_secret_value()
        for role in (settings.reply, settings.reply_fallback)
        if role is not None and role.api_key is not None and role.api_key.get_secret_value()
    ]
    assert not any(secret in text for secret in secrets)
    recorded = json.loads((target / "turns.json").read_text(encoding="utf-8"))
    assert len(recorded) == 42


async def test_brief_recall_settings_limit_native_evidence() -> None:
    fixture = load_brief_fixture()
    memory_case = load_trust_fixture()["cases"][0]
    case = next(case for case in fixture["cases"] if case["needs_recall"])
    case = {**case, "memory": memory_case["memory"]}
    settings = strategy_settings("tool").model_copy(
        update={
            "conversation": strategy_settings("tool").conversation.model_copy(
                update={"sentence_streaming": False}
            )
        }
    )
    settings = settings.model_copy(
        update={
            "memory": settings.memory.model_copy(
                update={"recall": settings.memory.recall.model_copy(update={"result_limit": 1})}
            )
        }
    )
    score = await replay_briefs(
        contract_model, {**fixture, "cases": [case]}, 1, options=ReplayOptions(settings=settings)
    )
    assert score.turns[0]["exception"] is None
    evidence = json.loads(score.turns[0]["tool_trajectory"][0]["output"])
    assert len(evidence["facts"]) == 1
    assert score.candidate["unchanged"] is True


async def test_brief_response_deadline_cancels_native_responder() -> None:
    from langchain_core.messages import AIMessage

    from evals._trust import EvalDelayedModel

    fixture = load_brief_fixture()
    settings = Settings()
    settings = settings.model_copy(
        update={
            "conversation": settings.conversation.model_copy(
                update={"response_timeout_seconds": 0.5}
            )
        }
    )
    model = EvalDelayedModel(responses=[AIMessage(content="Too late.")])
    with suspended_gc():
        score = await replay_briefs(
            lambda _: model,
            {**fixture, "cases": fixture["cases"][:1]},
            1,
            options=ReplayOptions(settings=settings),
        )
    assert score.turns[0]["exception"].startswith("TimeoutError:")
    assert score.turns[0]["reply"] == ""
    assert score.turns[0]["latency_seconds"] < 4.0
    assert model.cancelled


@pytest.mark.live
async def test_live_brief_replay_captures_36_rated_pairs(tmp_path: Path) -> None:
    settings = Settings()
    try:
        model = create_chat_model(settings)
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    assert settings.conversation.sentence_streaming is False
    fixture = load_brief_fixture()
    target = brief_artifact_dir(tmp_path)
    score = await replay_briefs(
        lambda _case: model,
        fixture,
        BRIEF_RUNS,
        options=ReplayOptions(settings=settings, fallback=create_fallback_chat_model(settings)),
    )
    write_brief_artifacts(
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
        (case_id, run) for case_id in EXPECTED_CASE_IDS for run in range(BRIEF_RUNS)
    }
    assert sum(1 for turn in score.turns if turn.get("exception")) == 0
    assert score.candidate["unchanged"] is True, score.candidate["changed_files"]


async def test_contract_brief_prefetch_follows_scope() -> None:
    fixture = load_brief_fixture()
    score = await replay_briefs(
        partial(contract_model, recall_tool=False),
        fixture,
        1,
        options=ReplayOptions(settings=strategy_settings("prefetch")),
    )
    by_id = {case["id"]: case for case in fixture["cases"]}
    briefs = {case["id"]: case_brief(fixture, case) for case in fixture["cases"]}
    assert all(turn["exception"] is None for turn in score.turns)
    for turn in score.turns:
        case = by_id[turn["case_id"]]
        memory = briefs[turn["case_id"]].knowledge_scope != "brief_only" and bool(
            case["memory_requested"]
        )
        assert turn["model_calls"] == 1
        assert turn["tools_bound"] == []
        assert bool(turn["memory_prefetch"]) is memory
        if not memory:
            assert turn["tools_bound"] == []
            assert turn["tool_trajectory"] == []
    for turn in score.rated_turns():
        assert all(turn["aids"][name] for name in AID_CHECKS)
