from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.conversation.contribution import (
    ContributionClaim,
    ContributionGate,
    SourceRegistry,
    validate_contribution,
)
from fakeyou.conversation.participation_graph import JudgeThresholds, decision_of
from fakeyou.conversation.turn_judge import TurnJudgement
from fakeyou.perception.meet_protocol import MeetUser
from fakeyou.perception.roster import MeetRoster

FIXTURE = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / "kernel-scenarios.json"
AREAS = frozenset(
    {
        "participation",
        "identity",
        "authorization",
        "planner",
        "browser_command",
        "speech",
        "latency",
        "recovery",
        "long_session",
        "live_meet",
    }
)
THRESHOLDS = JudgeThresholds(reply=0.6, clarify=0.5)

Classification = Literal["preserve", "intentional_change", "known_bug_not_parity"]
Kind = Literal["participation", "roster", "contribution_gate", "catalog"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RollbackThresholds(Frozen):
    false_reply_rate_max: float = Field(ge=0.0, le=1.0)
    missed_answer_rate_max: float = Field(ge=0.0, le=1.0)
    missed_named_max: int = Field(ge=0)
    premature_commit_rate_max: float = Field(ge=0.0, le=1.0)
    severe_invariant_violations_max: int = Field(ge=0)
    first_audio_p50_seconds_max: float = Field(gt=0)
    first_audio_p95_seconds_max: float = Field(gt=0)
    meet_action_p95_seconds_max: float = Field(gt=0)
    meet_action_success_rate_min: float = Field(ge=0.0, le=1.0)
    stop_to_silence_p95_seconds_max: float = Field(gt=0)
    judge_p90_ms_max: float = Field(gt=0)


class Scenario(Frozen):
    id: str = Field(min_length=1)
    area: str
    kind: Kind
    classification: Classification
    given: dict[str, object] = Field(default_factory=dict)
    current: dict[str, object] = Field(default_factory=dict)
    target: dict[str, object] = Field(default_factory=dict)
    executable_in: str | None = None


class KernelScenarios(Frozen):
    version: Literal[1]
    rollback_thresholds: RollbackThresholds
    scenarios: tuple[Scenario, ...] = Field(min_length=1)


def load_scenarios() -> KernelScenarios:
    return KernelScenarios.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def executable(kind: Kind) -> list[Scenario]:
    return [scenario for scenario in load_scenarios().scenarios if scenario.kind == kind]


def test_the_corpus_covers_every_kernel_area_with_unique_ids() -> None:
    corpus = load_scenarios()

    assert len({scenario.id for scenario in corpus.scenarios}) == len(corpus.scenarios)
    assert {scenario.area for scenario in corpus.scenarios} == AREAS
    assert corpus.rollback_thresholds.missed_named_max == 0
    assert corpus.rollback_thresholds.severe_invariant_violations_max == 0


def test_preserve_scenarios_keep_the_same_outcome_class() -> None:
    for scenario in load_scenarios().scenarios:
        if scenario.classification == "preserve":
            assert scenario.target.get("outcome") == scenario.current.get("outcome"), scenario.id
        else:
            assert scenario.target.get("outcome") != scenario.current.get("outcome"), scenario.id


def test_catalog_scenarios_name_the_pr_that_makes_them_executable() -> None:
    for scenario in load_scenarios().scenarios:
        if scenario.kind == "catalog":
            assert scenario.executable_in is not None, scenario.id
            assert scenario.target.get("outcome"), scenario.id


@pytest.mark.parametrize("scenario", executable("participation"), ids=lambda item: item.id)
def test_participation_characterization(scenario: Scenario) -> None:
    given = scenario.given
    judgement = TurnJudgement.model_validate(given["judgement"])
    decision = decision_of(
        judgement,
        THRESHOLDS,
        named=bool(given.get("named", False)),
        sole_human_participant=bool(given.get("sole_human_participant", False)),
    )

    assert decision.decision == scenario.current["decision"]
    assert decision.reason_code == scenario.current["reason_code"]
    assert decision.meet_action == scenario.current.get("meet_action", "none")
    assert scenario.current["outcome"] == decision.decision


@pytest.mark.parametrize("scenario", executable("roster"), ids=lambda item: item.id)
def test_roster_characterization(scenario: Scenario) -> None:
    roster = MeetRoster()
    batches = scenario.given["user_batches"]
    assert isinstance(batches, list)
    for batch in batches:
        assert isinstance(batch, list)
        roster.apply_users([MeetUser.model_validate(user) for user in batch])

    assert roster.people() == scenario.current["people"]


def _string_map(value: object) -> dict[str, str]:
    assert isinstance(value, dict)
    return {str(key): str(item) for key, item in value.items()}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


@pytest.mark.parametrize("scenario", executable("contribution_gate"), ids=lambda item: item.id)
def test_contribution_gate_characterization(scenario: Scenario) -> None:
    registry_data = scenario.given["registry"]
    assert isinstance(registry_data, dict)
    questions = registry_data.get("brief_questions", [])
    assert isinstance(questions, list)
    registry = SourceRegistry(
        brief=_string_map(registry_data.get("brief", {})),
        brief_questions=frozenset(str(item) for item in questions),
        profile=_string_map(registry_data.get("profile", {})),
    )
    gate = ContributionGate(registry=registry, trigger_text="trigger")
    claim_data = scenario.given["claim"]
    assert isinstance(claim_data, dict)
    claim = ContributionClaim(
        structured=bool(claim_data["structured"]),
        intent=str(claim_data["intent"]),
        text=str(claim_data["text"]),
        kind=_optional_string(claim_data.get("kind")),
        source=_optional_string(claim_data.get("source")),
        source_ref=_optional_string(claim_data.get("source_ref")),
    )
    verdict = validate_contribution(claim, gate)

    assert verdict.passed == scenario.current["passed"]
    assert verdict.reason == scenario.current["reason"]
    assert scenario.current["outcome"] == ("pass" if verdict.passed else "drop")
