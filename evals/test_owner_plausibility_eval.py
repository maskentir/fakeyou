import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams

from evals._judge import judge_config, model_family
from fakeyou.config.settings import Settings
from fakeyou.conversation.agent import CONTRIBUTION_GUIDANCE, create_meeting_agent
from fakeyou.conversation.contribution import OwnerModelSource, source_registry
from fakeyou.conversation.conversation import MeetingAgent, MeetingResponder
from fakeyou.conversation.critic import ModelContributionCritic
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.speech_book import BookSource
from fakeyou.foundation.transcript import AttentionContext, TranscriptEvent
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.persona_prompt import build_persona_prompt
from fakeyou.orchestration.providers import create_chat_model, create_turn_judge_model
from fakeyou.persistence.owner_model import FrozenOwnerModel

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "owner-plausibility-scenarios.json").read_text(
        encoding="utf-8"
    )
)
EXAMPLE_PROFILE = PersonaProfile.model_validate_json(
    (Path(__file__).resolve().parent.parent / "examples" / "persona.example.json").read_bytes()
)
SCENARIOS: list[dict[str, str]] = FIXTURE["scenarios"]
OWNER_MODEL = FrozenOwnerModel.model_validate(FIXTURE["owner_model"])
PLAUSIBLE_THRESHOLD = 0.8
PLAUSIBLE_SHARE = 0.8
JUDGE_SAMPLES = 3


def _position(scenario: dict[str, str]) -> str:
    item = next(item for item in OWNER_MODEL.items if item.item_id == scenario["position"])

    return item.statement


def _sources() -> list[str]:
    return [f"The owner's approved source: {item.statement}" for item in OWNER_MODEL.items]


def _room_event(room: str) -> TranscriptEvent:
    return TranscriptEvent(
        id="room-1",
        timestamp=datetime.now(UTC).isoformat(),
        source="meeting_audio",
        text=room,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
    )


def _attention_event(room: str) -> TranscriptEvent:
    return TranscriptEvent(
        id="attention-1",
        timestamp=datetime.now(UTC).isoformat(),
        source="meeting_attention",
        text=room,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
        attention=AttentionContext(kind="screen", trigger_text=room, trigger_event_id="screen#1"),
    )


def _responder(settings: Settings, model: Any, critic_model: Any) -> MeetingResponder:
    persona = build_persona_prompt(EXAMPLE_PROFILE, owner_model=OWNER_MODEL)
    registry = source_registry(
        owner_model={
            item.ref: OwnerModelSource(
                item.statement,
                item.disclosure,
                kind=item.kind,
                confidence=item.confidence,
                since=item.valid_at,
            )
            for item in OWNER_MODEL.items
        }
    )
    return MeetingResponder(
        cast(MeetingAgent, create_meeting_agent(model, persona=persona)),
        NoHistory(),
        language="id",
        journal=Journal("owner-plausibility"),
        book_source=BookSource(registry=registry),
        contributor=cast(
            MeetingAgent,
            create_meeting_agent(model, persona=f"{persona}\n\n{CONTRIBUTION_GUIDANCE}"),
        ),
        critic=ModelContributionCritic(
            critic_model, timeout_seconds=settings.turn_judge.timeout_seconds
        ),
    )


def test_the_owner_plausibility_fixture_covers_positions_and_kinds() -> None:
    identifiers = [scenario["id"] for scenario in SCENARIOS]

    assert len(set(identifiers)) == len(identifiers)
    assert len(SCENARIOS) >= 4
    assert {scenario["kind"] for scenario in SCENARIOS} == {"proactive", "opinion"}
    stances = {
        item.item_id
        for item in OWNER_MODEL.items
        if item.kind in ("position", "preference", "decision_rule")
    }
    for scenario in SCENARIOS:
        assert scenario["position"] in stances
        assert scenario["room"].strip()
        assert scenario["criteria"].strip()
        assert _position(scenario).strip()


@pytest.mark.live
async def test_the_owner_judges_the_contributions_owner_plausible() -> None:
    settings = Settings()
    judge = judge_config()
    if judge is None:
        pytest.skip("set EVAL_JUDGE_API_KEY to run the owner plausibility judge")
    if judge.model == settings.reply.model:
        pytest.fail(f"the judge {judge.model} is the reply model under test")
    model = create_chat_model(settings, temperature=0.0)
    critic_model = create_turn_judge_model(settings, temperature=0.0)
    responder = _responder(settings, model, critic_model)
    scores: list[float] = []

    for scenario in SCENARIOS:
        events = (
            [_attention_event(scenario["room"])]
            if scenario["kind"] == "proactive"
            else [_room_event(scenario["room"])]
        )
        chunk = await responder.respond(events, lambda: None)
        if not chunk.text:
            scores.append(0.0)
            continue
        case = LLMTestCase(
            input=scenario["room"],
            actual_output=chunk.text,
            context=[f"The owner position: {_position(scenario)}", *_sources()],
        )
        samples = [
            float(
                GEval(
                    name=scenario["id"],
                    criteria=scenario["criteria"],
                    evaluation_params=[
                        SingleTurnParams.INPUT,
                        SingleTurnParams.ACTUAL_OUTPUT,
                        SingleTurnParams.CONTEXT,
                    ],
                    model=judge.deepeval(),
                    threshold=PLAUSIBLE_THRESHOLD,
                    async_mode=False,
                ).measure(case)
            )
            for _ in range(JUDGE_SAMPLES)
        ]
        scores.append(statistics.median(samples))

    plausible = sum(1 for score in scores if score >= PLAUSIBLE_THRESHOLD)
    family = model_family(judge.model)
    reply_family = model_family(settings.reply.model)

    assert plausible / len(scores) >= PLAUSIBLE_SHARE, (
        f"owner plausible scores: {scores} | judge {judge.model} ({family}) | "
        f"reply {settings.reply.model} ({reply_family})"
    )
