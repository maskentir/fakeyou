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
from fakeyou.conversation.contribution import OwnerModelSource, source_registry
from fakeyou.conversation.critic import CriticClaim, CriticVerdict
from fakeyou.conversation.moments import MomentCandidate, MomentDetector, MomentJudgement
from fakeyou.session.speech import BookEntry
from tests._conversation_fakes import ScriptedJudge, ToolCallingFakeModel, judged
from tests._conversation_support import claim, plan_reply

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}
FACT = "The Orchid beta launch is planned for 14 October 2026"


class _Critic:
    async def judge(
        self,
        *,
        claims: Sequence[CriticClaim],
        sources: Sequence[BookEntry],
        trigger_text: str,
        recent_lines: Sequence[str] = (),
    ) -> CriticVerdict:
        del claims, sources, trigger_text, recent_lines
        return CriticVerdict(
            owner_would_say=True,
            worth_interrupting=True,
            guesses_hedged=True,
            grounded=True,
            adds_information=True,
            on_topic=True,
            confidence=0.95,
        )


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
    plan = plan_reply(
        claim("The beta launches on 14 October.", "om-orchid-beta-launch"), understood="question"
    )
    model = ToolCallingFakeModel(responses=[AIMessage(content=plan)])
    monkeypatch.setattr(assembly, "create_chat_model", lambda _settings: model)
    monkeypatch.setattr(assembly, "create_fallback_chat_model", lambda _settings: None)
    monkeypatch.setattr(assembly, "create_strong_chat_model", lambda _settings: None)
    monkeypatch.setattr(
        assembly, "build_turn_judge", lambda *_args: ScriptedJudge(lambda _w: judged("stay_silent"))
    )
    monkeypatch.setattr(assembly, "build_contribution_critic", lambda *_args: _Critic())
    fixture = load_proactive_fixture()
    profile = proactive_profile(fixture)
    detector = MomentDetector(
        registry=source_registry(
            owner_model={"om-orchid-beta-launch": OwnerModelSource(FACT, "room_speakable")}
        ),
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
