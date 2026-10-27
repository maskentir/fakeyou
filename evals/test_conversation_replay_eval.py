import json
import os
from pathlib import Path
from typing import Any, override

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult

import fakeyou.orchestration.assembly as assembly
from evals._conversation_replay import (
    FAMILY_TOLERANCE,
    MIN_JUDGE_AGREEMENT,
    Expectation,
    ReplayBaseline,
    ReplayReport,
    Scenario,
    ScenarioFixture,
    Step,
    StepOutcome,
    Verdict,
    grade,
    judge_disagreements,
    load_baseline,
    load_judge_labels,
    load_scenarios,
    model_judge,
    replay_conversations,
    run_scenario,
    scenario_profile,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
from fakeyou.orchestration.providers import create_chat_model, create_debrief_chat_model
from tests._conversation_fakes import ScriptedJudge, ToolCallingFakeModel, judged

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}


def _offline_settings() -> Settings:
    return Settings(**NO_ENV_FILE)


def _judge_answers_unless_for(other: str) -> ScriptedJudge:
    def verdict(window: JudgeWindow) -> TurnJudgement:
        if other.casefold() in window.latest.text.casefold():
            return judged("stay_silent")
        return judged("reply")

    return ScriptedJudge(verdict)


class UnreachableModel(ToolCallingFakeModel):
    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise ConnectionError("provider unreachable")


async def _always_met(context: str, reply: str, criterion: str) -> Verdict:
    del context, reply
    return Verdict(met=True, reason=criterion)


def test_the_judge_labels_are_balanced_enough_to_catch_a_lenient_judge() -> None:
    labels = load_judge_labels()

    assert len(labels) >= 30
    assert sum(1 for label in labels if not label.met) >= 10
    assert len({label.case for label in labels}) == len(labels)


def test_the_fixture_loads_with_every_family_and_unique_ids() -> None:
    fixture = load_scenarios()

    assert len({scenario.id for scenario in fixture.scenarios}) == len(fixture.scenarios)
    assert {scenario.family for scenario in fixture.scenarios} == {
        "small_talk",
        "misheard",
        "control",
        "recall",
        "honesty",
        "floor",
        "language",
        "chat",
        "attention",
    }
    assert scenario_profile(fixture).display_name == "Rani"


def test_the_baseline_covers_every_scenario_family() -> None:
    baseline = load_baseline()

    assert set(baseline.families) == {scenario.family for scenario in load_scenarios().scenarios}


def test_a_family_regresses_only_when_it_falls_past_the_tolerance() -> None:
    def outcome(family: str, *, passed: bool) -> StepOutcome:
        return StepOutcome("s", family, 0, "line", [], checks={"replied": passed})

    report = ReplayReport(
        [
            outcome("recall", passed=True),
            outcome("recall", passed=False),
            outcome("floor", passed=True),
            outcome("floor", passed=False),
        ]
    )
    baseline = ReplayBaseline(
        reply_model="m",
        runs=1,
        families={"recall": 0.5 + FAMILY_TOLERANCE, "floor": 1.0, "honesty": 1.0},
    )

    assert report.regressions(baseline) == {"floor": 0.5, "honesty": 0.0}


async def test_replay_grades_replies_silence_and_cut_offs_through_the_real_turn_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content="Beta launch is planned for 14 October. Anything else?"),
            AIMessage(content="First part. Second part. Third part."),
            AIMessage(content="I was explaining the beta plan."),
        ]
    )
    monkeypatch.setattr(assembly, "create_chat_model", lambda _settings: model)
    monkeypatch.setattr(assembly, "create_fallback_chat_model", lambda _settings: None)
    monkeypatch.setattr(assembly, "create_strong_chat_model", lambda _settings: None)
    monkeypatch.setattr(
        assembly, "build_turn_judge", lambda *_args: _judge_answers_unless_for("Budi")
    )
    scenario = Scenario(
        id="c1",
        family="recall",
        title="contract",
        steps=(
            Step(
                say="Rani, is the beta launch still on 14 October?",
                speaker="speaker-a",
                expect=Expectation(),
            ),
            Step(say="Budi, have you eaten?", speaker="speaker-b", expect=Expectation(reply=False)),
            Step(say="Rani, explain the plan.", speaker="speaker-a", cut_after=1),
            Step(
                say="What did you just say?",
                speaker="speaker-a",
                expect=Expectation(max_sentences=1, means="recaps"),
            ),
        ),
    )
    profile = scenario_profile(load_scenarios())

    played = await run_scenario(scenario, profile, _offline_settings())
    graded = await grade(scenario, played, _always_met, profile.language)

    assert [step.checks for step in graded] == [
        {"replied": True, "no_closing_question": False},
        {"silent": True},
        {},
        {"replied": True, "sentences": True, "no_closing_question": True, "means": True},
    ]
    assert [(line.text, line.interrupted) for line in graded[2].spoken] == [
        ("First part.", False),
        ("Second part.", True),
    ]
    history = [message.text for message in model.seen[-1][1:]]
    assert "First part. Second part. —" in history


async def test_an_interim_for_the_twin_cuts_the_reply_but_one_for_others_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content="First part. Second part.")])
    monkeypatch.setattr(assembly, "create_chat_model", lambda _settings: model)
    monkeypatch.setattr(assembly, "create_fallback_chat_model", lambda _settings: None)
    monkeypatch.setattr(assembly, "create_strong_chat_model", lambda _settings: None)
    monkeypatch.setattr(
        assembly, "build_turn_judge", lambda *_args: _judge_answers_unless_for("Budi")
    )
    profile = scenario_profile(load_scenarios())

    for_twin = Scenario(
        id="c3",
        family="floor",
        title="interim for the twin",
        steps=(
            Step(say="Rani, what about the beta?", speaker="speaker-a", expect=Expectation()),
            Step(
                say="Rani, wait a moment.",
                speaker="speaker-b",
                kind="interim",
                expect=Expectation(reply=False, barge_in=True),
            ),
        ),
    )
    for_others = Scenario(
        id="c4",
        family="floor",
        title="interim for somebody else",
        steps=(
            Step(say="Rani, what about the beta?", speaker="speaker-a", expect=Expectation()),
            Step(
                say="Budi, can you send me the deck?",
                speaker="speaker-b",
                kind="interim",
                expect=Expectation(reply=False, barge_in=False),
            ),
        ),
    )

    cut = await run_scenario(for_twin, profile, _offline_settings())
    kept = await run_scenario(for_others, profile, _offline_settings())

    assert [line.text for line in cut[0].spoken] == ["First part."]
    assert cut[1].barge_in is True
    assert [line.text for line in kept[0].spoken] == ["First part.", "Second part."]
    assert kept[1].barge_in is False


async def test_a_failed_turn_fails_its_step_even_without_an_expectation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = UnreachableModel(responses=[AIMessage(content="unused")])
    monkeypatch.setattr(assembly, "create_chat_model", lambda _settings: model)
    monkeypatch.setattr(assembly, "create_fallback_chat_model", lambda _settings: None)
    monkeypatch.setattr(assembly, "create_strong_chat_model", lambda _settings: None)
    monkeypatch.setattr(
        assembly, "build_turn_judge", lambda *_args: _judge_answers_unless_for("Budi")
    )
    scenario = Scenario(
        id="c2",
        family="recall",
        title="provider down",
        steps=(Step(say="Rani, when is the beta launch?", speaker="speaker-a"),),
    )
    profile = scenario_profile(load_scenarios())

    played = await run_scenario(scenario, profile, _offline_settings())
    graded = await grade(scenario, played, _always_met, profile.language)

    assert graded[0].failures == ["ProviderFault"]
    assert graded[0].checks == {"turn_completed": False}


@pytest.mark.live
async def test_live_conversation_replay_has_no_severe_failures(tmp_path: Path) -> None:
    settings = Settings()
    try:
        create_chat_model(settings)
        judge = model_judge(create_debrief_chat_model(settings))
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    labels = load_judge_labels()
    missed = await judge_disagreements(judge, labels)
    assert 1 - len(missed) / len(labels) >= MIN_JUDGE_AGREEMENT, missed
    fixture: ScenarioFixture = load_scenarios()
    baseline = load_baseline()
    runs = int(os.environ.get("CONVERSATION_RUNS", "3"))

    report = await replay_conversations(fixture, settings, judge, runs=runs)

    raw_target = os.environ.get("CONVERSATION_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    target.mkdir(parents=True, exist_ok=True)
    (target / "conversation-replay.json").write_text(
        json.dumps(
            {
                "reply_model": settings.reply.model,
                "runs": runs,
                "judge_disagreements": missed,
                "baseline": baseline.families,
                "regressions": report.regressions(baseline),
                **report.as_json(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert report.severe_failures() == []
    assert report.regressions(baseline) == {}
