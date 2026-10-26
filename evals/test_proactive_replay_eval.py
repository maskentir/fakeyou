import json
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import fakeyou.orchestration.assembly as assembly
from evals._proactive_replay import (
    load_proactive_fixture,
    proactive_profile,
    replay_proactive,
    run_proactive_scenario,
)
from fakeyou.config.settings import Settings
from fakeyou.conversation.contribution import source_registry
from fakeyou.conversation.grounding import GroundingVerdict
from fakeyou.conversation.moments import MomentCandidate, MomentDetector, MomentJudgement
from tests._conversation_fakes import ScriptedJudge, ToolCallingFakeModel, judged

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}
FACT = "The Orchid beta launch is planned for 14 October 2026"


class _Verifier:
    async def verify(
        self, *, text: str, source_text: str, trigger_text: str, kind: str | None
    ) -> GroundingVerdict:
        del text, source_text, trigger_text, kind
        return GroundingVerdict(grounded=True, adds_information=True, confidence=0.95)


class _MomentJudge:
    async def judge(
        self,
        *,
        lines: Sequence[tuple[str, str]],
        candidates: Sequence[MomentCandidate],
        owner_names: Sequence[str],
        twin_names: Sequence[str],
    ) -> MomentJudgement:
        del lines, owner_names, twin_names
        return MomentJudgement(
            moment="unanswered_question", source_ref=candidates[0].ref, confidence=0.95
        )


async def _embed(texts: Sequence[str]) -> list[list[float]]:
    return [[1.0, 0.0] if "beta" in text.casefold() else [0.0, 1.0] for text in texts]


def test_the_proactive_fixture_covers_positive_and_control_scenarios() -> None:
    fixture = load_proactive_fixture()

    assert len({scenario.id for scenario in fixture.scenarios}) == len(fixture.scenarios)
    assert sum(1 for scenario in fixture.scenarios if scenario.expect.speak) >= 5
    assert sum(1 for scenario in fixture.scenarios if scenario.kind == "control") >= 3
    assert sum(scenario.duration_seconds for scenario in fixture.scenarios) >= 3600


async def test_the_replay_runs_detector_to_speech_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = json.dumps(
        {
            "understood": "question",
            "language": "en",
            "intent": "answer",
            "text": "The beta launches on 14 October.",
            "contribution_kind": "fact",
            "contribution_source": "profile",
            "source_ref": "fact-0",
        }
    )
    model = ToolCallingFakeModel(responses=[AIMessage(content=plan)])
    monkeypatch.setattr(assembly, "create_chat_model", lambda _settings: model)
    monkeypatch.setattr(assembly, "create_fallback_chat_model", lambda _settings: None)
    monkeypatch.setattr(assembly, "create_strong_chat_model", lambda _settings: None)
    monkeypatch.setattr(
        assembly, "build_turn_judge", lambda *_args: ScriptedJudge(lambda _w: judged("stay_silent"))
    )
    monkeypatch.setattr(assembly, "build_grounding_verifier", lambda _settings: _Verifier())
    fixture = load_proactive_fixture()
    profile = proactive_profile(fixture)
    detector = MomentDetector(
        registry=source_registry(profile_facts=[FACT]),
        judge=_MomentJudge(),
        embed=_embed,
        twin_names=("Rani",),
        similarity_threshold=0.5,
        unanswered_seconds=15.0,
    )
    scenario = next(item for item in fixture.scenarios if item.id == "p01")

    outcome = await run_proactive_scenario(
        scenario, fixture, profile, Settings(**NO_ENV_FILE), detector=detector
    )

    assert outcome.offers == 1
    assert outcome.spoken == ["The beta launches on 14 October."]
    assert outcome.wrong == 0
    assert not outcome.missed


@pytest.mark.live
async def test_the_proactive_lane_stays_within_the_wrong_per_hour_gate() -> None:
    report = await replay_proactive(Settings())

    assert report.control_wrong == 0
    assert report.wrong_per_hour <= 1.0
    assert report.spoken >= 3
