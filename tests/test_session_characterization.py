from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, get_args

import pytest
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.conversation.contribution import SourceRegistry
from fakeyou.conversation.participation_graph import JudgeThresholds, decision_of
from fakeyou.conversation.speech_book import BookSource, fact_book
from fakeyou.conversation.turn_judge import TurnJudgement
from fakeyou.perception.meet_protocol import MeetUser
from fakeyou.perception.roster import MeetRoster
from fakeyou.session.contracts import (
    CallStateObserved,
    Endpoint,
    Observation,
    RosterObserved,
    SessionEvent,
    TranscriptObserved,
)
from fakeyou.session.identity import PresenceFact, presence
from fakeyou.session.intent import Answer, Compound, Intent, MeetAction, Reading, intent_of
from fakeyou.session.planner import TurnPlan, plan_turn
from fakeyou.session.policy import UNATTRIBUTED, Actor, Grant, Permission, authorize
from fakeyou.session.speech import ContributionKind, SpeechAct, check_act, check_sentence
from fakeyou.session.world import SessionState, reduce

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
NOW = datetime(2026, 10, 27, tzinfo=UTC)

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
    proof: str | None = None


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


def expected_outcome(scenario: Scenario) -> object:
    source = scenario.current if scenario.classification == "preserve" else scenario.target
    return source["outcome"]


@pytest.mark.parametrize("scenario", executable("participation"), ids=lambda item: item.id)
def test_participation_reaches_the_kernel_outcome(scenario: Scenario) -> None:
    given = scenario.given
    judgement = TurnJudgement.model_validate(given["judgement"])
    presence_complete = bool(given.get("target_presence_complete", True))
    decision = decision_of(
        judgement,
        THRESHOLDS,
        named=bool(given.get("named", False)),
        sole_human_responder=bool(given.get("sole_human_participant", False)) and presence_complete,
    )

    assert decision.decision == expected_outcome(scenario)
    if scenario.classification == "preserve":
        assert decision.reason_code == scenario.current["reason_code"]
        assert decision.meet_action == scenario.current.get("meet_action", "none")


def _batches(scenario: Scenario) -> list[list[MeetUser]]:
    batches = scenario.given["user_batches"]
    assert isinstance(batches, list)
    return [[MeetUser.model_validate(user) for user in batch] for batch in batches]


@pytest.mark.parametrize("scenario", executable("roster"), ids=lambda item: item.id)
def test_the_protocol_roster_adapter_keeps_its_baseline(scenario: Scenario) -> None:
    roster = MeetRoster()
    for batch in _batches(scenario):
        roster.apply_users(batch)

    assert roster.people() == scenario.current["people"]


def _kernel_state(scenario: Scenario) -> SessionState:
    observations: list[Observation] = [CallStateObserved(state="in_call", browser_epoch=1)]
    for index, batch in enumerate(_batches(scenario)):
        endpoints = tuple(
            Endpoint(
                device_id=user.device_id,
                display_name=user.name,
                present=user.present,
                current_user=user.current_user,
            )
            for user in batch
        )
        observations.append(RosterObserved(endpoints=endpoints, synced=index == 0))
    if scenario.given.get("target_unmapped_voice_track_active"):
        observations.append(
            TranscriptObserved(transcript_id="t1", text="hello", final=True, track_id="voice-x")
        )
    state = SessionState()
    for index, observation in enumerate(observations):
        state = reduce(
            state,
            SessionEvent(
                event_id=f"e{index}",
                session_id="corpus",
                attempt_id="corpus-1",
                source="meet_protocol",
                received_at=NOW,
                observation=observation,
            ),
        )
    return state


def _count_matches(fact: PresenceFact, count: object) -> bool:
    assert isinstance(count, dict)
    return (fact.confirmed_min, fact.plausible_max, fact.complete) == (
        count["min"],
        count["max"],
        count["complete"],
    )


@pytest.mark.parametrize("scenario", executable("roster"), ids=lambda item: item.id)
def test_the_kernel_presence_reaches_the_target(scenario: Scenario) -> None:
    state = _kernel_state(scenario)
    fact = presence(state)
    devices = [user.device_id for user in _batches(scenario)[0] if not user.current_user]
    outcomes = {
        "exact": lambda: _count_matches(fact, scenario.target["count"]),
        "unknown_range": lambda: _count_matches(fact, scenario.target["count"]),
        "still_present": lambda: all(
            state.world.endpoints[device].presence == "present" for device in devices
        ),
        "two_endpoints_claim_conflict": lambda: (
            len(fact.participants) == 2
            and all(participant.claim_conflict for participant in fact.participants)
        ),
        "left": lambda: all(
            state.world.endpoints[device].presence == "absent" for device in devices
        ),
    }

    assert outcomes[str(scenario.target["outcome"])]()


def _string_map(value: object) -> dict[str, str]:
    assert isinstance(value, dict)
    return {str(key): str(item) for key, item in value.items()}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


@pytest.mark.parametrize("scenario", executable("contribution_gate"), ids=lambda item: item.id)
def test_contributions_pass_the_speech_gate_as_the_target_says(scenario: Scenario) -> None:
    registry_data = scenario.given["registry"]
    assert isinstance(registry_data, dict)
    questions = registry_data.get("brief_questions", [])
    assert isinstance(questions, list)
    brief = _string_map(registry_data.get("brief", {}))
    registry = SourceRegistry(
        facts={ref: text for ref, text in brief.items() if ref not in questions},
        questions={ref: text for ref, text in brief.items() if ref in questions},
        profile=_string_map(registry_data.get("profile", {})),
    )
    book = fact_book(
        BookSource(registry, frozenset({"explain_facts", "ask_brief_questions"})),
        heard="trigger",
        observed="",
        memory={},
    )
    claim = scenario.given["claim"]
    assert isinstance(claim, dict)
    act = SpeechAct(
        cites=tuple(ref for ref in (_optional_string(claim.get("source_ref")),) if ref),
        contribution=cast(ContributionKind, claim["kind"]),
    )
    verdict = check_act(act, book)
    if verdict == "pass":
        verdict = check_sentence(str(claim["text"]), act, book)

    assert ("pass" if verdict == "pass" else "drop") == scenario.target["outcome"]


def _planned(intent: Intent, actor: Actor, permissions: frozenset[Permission]) -> TurnPlan:
    authorization = authorize(intent, actor=actor, channel="speech", grant=Grant(permissions))
    return plan_turn(authorization, channel="speech", plan_id="corpus")


EVERY_PERMISSION: frozenset[Permission] = frozenset(get_args(Permission))
FINISHED_PRS = frozenset({"PR3", "PR5", "PR6"})


def _fallback_plan() -> TurnPlan:
    authorization = authorize(
        intent_of(Reading("speak", "fallback_named")),
        actor=Actor("participant", "dev-ayu"),
        channel="speech",
        grant=Grant(EVERY_PERMISSION),
    )
    return plan_turn(authorization, channel="speech", plan_id="corpus")


CATALOG_PLANS: dict[str, Callable[[], bool]] = {
    "k-l1": lambda: (
        _fallback_plan().action is None
        and not _fallback_plan().leave
        and _fallback_plan().evidence == ()
    ),
    "k-a1": lambda: (
        not _planned(Compound(MeetAction("leave"), Answer()), UNATTRIBUTED, EVERY_PERMISSION).leave
    ),
    "k-a2": lambda: (
        not _planned(MeetAction("leave"), UNATTRIBUTED, frozenset({"meet_act", "meet_chat"})).leave
    ),
    "k-pl1": lambda: (
        _planned(
            Compound(MeetAction("react", "\U0001f44d"), Answer()),
            Actor("participant", "dev-ayu"),
            EVERY_PERMISSION,
        ).template
        == "action_then_speech"
    ),
    "k-pl2": lambda: (
        _planned(
            Compound(MeetAction("leave"), Answer(("screen", "memory"))),
            Actor("participant", "dev-ayu"),
            EVERY_PERMISSION,
        ).evidence
        == ()
    ),
}


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in executable("catalog") if scenario.executable_in in FINISHED_PRS],
    ids=lambda item: item.id,
)
def test_catalog_scenarios_of_finished_prs_are_executable(scenario: Scenario) -> None:
    if scenario.proof is not None:
        path, name = scenario.proof.split("::")
        source = (FIXTURE.parent.parent.parent / path).read_text(encoding="utf-8")
        assert f"def {name}(" in source
        return
    assert CATALOG_PLANS[scenario.id]()
