import asyncio
import json
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, ConfigDict

import fakeyou.orchestration.assembly as assembly
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import AttentionSettings, Settings
from fakeyou.conversation.moments import ModelMomentJudge, MomentDetector
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.transcript import AttentionContext, TranscriptEvent, attention_line
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.providers import create_embedder, create_turn_judge_model
from fakeyou.perception.attention import AttentionPrefilter
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.persistence.record import MeetingRecord
from tests._audio_fakes import FakeReplyAudio

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "evals" / "fixtures" / "proactive-scenarios.json"


class ProactiveExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    speak: bool
    kind: str | None = None


class ProactiveScreen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    points: tuple[str, ...] = ()


class ProactiveStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    say: str | None = None
    speaker: str = "device-ayu"
    screen: ProactiveScreen | None = None
    at: float = 0.0


class ProactiveScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    duration_seconds: float
    expect: ProactiveExpectation
    steps: tuple[ProactiveStep, ...]


class ProactiveFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    persona: str
    persona_overrides: dict[str, Any] = {}
    brief: MeetingBrief
    scenarios: tuple[ProactiveScenario, ...]


@dataclass(slots=True)
class ProactiveOutcome:
    scenario: str
    kind: str
    expected_speak: bool
    offers: int = 0
    spoken: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def wrong(self) -> int:
        return len(self.spoken) if not self.expected_speak else 0

    @property
    def missed(self) -> bool:
        return self.expected_speak and not self.spoken


@dataclass(slots=True)
class ProactiveReport:
    outcomes: list[ProactiveOutcome]
    total_seconds: float

    @property
    def offers(self) -> int:
        return sum(outcome.offers for outcome in self.outcomes)

    @property
    def spoken(self) -> int:
        return sum(len(outcome.spoken) for outcome in self.outcomes)

    @property
    def wrong(self) -> int:
        return sum(outcome.wrong for outcome in self.outcomes)

    @property
    def missed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.missed)

    @property
    def wrong_per_hour(self) -> float:
        hours = self.total_seconds / 3600.0
        return self.wrong / hours if hours else 0.0

    @property
    def control_wrong(self) -> int:
        return sum(outcome.wrong for outcome in self.outcomes if outcome.kind == "control")

    def as_dict(self) -> dict[str, Any]:
        return {
            "offers": self.offers,
            "spoken": self.spoken,
            "wrong": self.wrong,
            "missed": self.missed,
            "wrong_per_hour": round(self.wrong_per_hour, 3),
            "control_wrong": self.control_wrong,
            "outcomes": [
                {
                    "scenario": outcome.scenario,
                    "kind": outcome.kind,
                    "expected_speak": outcome.expected_speak,
                    "offers": outcome.offers,
                    "spoken": outcome.spoken,
                    "failures": outcome.failures,
                }
                for outcome in self.outcomes
            ],
        }


def load_proactive_fixture(path: Path = FIXTURE) -> ProactiveFixture:
    return ProactiveFixture.model_validate_json(path.read_text(encoding="utf-8"))


def proactive_profile(fixture: ProactiveFixture) -> PersonaProfile:
    base = json.loads((ROOT / fixture.persona).read_text(encoding="utf-8"))
    return PersonaProfile.model_validate({**base, **fixture.persona_overrides})


def build_detector(
    fixture: ProactiveFixture,
    profile: PersonaProfile,
    settings: Settings,
    journal: Journal,
) -> MomentDetector:
    attention = settings.perception.attention
    embedder = create_embedder(settings)

    async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
        assert embedder is not None
        return await embedder.aembed_documents(list(texts))

    return MomentDetector(
        registry=assembly.build_source_registry(profile, fixture.brief),
        judge=ModelMomentJudge(
            create_turn_judge_model(settings),
            timeout_seconds=settings.turn_judge.timeout_seconds,
        ),
        embed=embed_texts if embedder is not None else None,
        twin_names=(profile.display_name,),
        owner_names=attention.owner_names or ("Rani",),
        similarity_threshold=attention.similarity_threshold,
        moment_confidence=attention.moment_confidence,
        unanswered_seconds=attention.unanswered_seconds,
        journal=journal,
    )


async def _settle(turns: Any) -> None:
    for _ in range(200):
        await turns.lifecycle.wait_idle()
        if not turns.lifecycle.deferred.pending and not turns.lifecycle.is_busy:
            return
        await asyncio.sleep(0.02)


def _attention_event(scenario: ProactiveScenario, context: AttentionContext) -> TranscriptEvent:
    return TranscriptEvent(
        id=f"{scenario.id}-attention-{context.trigger_event_id or 'screen'}",
        timestamp=datetime.now(UTC).isoformat(),
        source="meeting_attention",
        text=attention_line(context),
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
        attention=context,
    )


async def run_proactive_scenario(
    scenario: ProactiveScenario,
    fixture: ProactiveFixture,
    profile: PersonaProfile,
    settings: Settings,
    *,
    detector: MomentDetector | None = None,
) -> ProactiveOutcome:
    attention = AttentionSettings(
        enabled=True,
        transcript_moments=True,
        owner_names=("Rani",),
        unanswered_seconds=settings.perception.attention.unanswered_seconds,
    )
    config = SessionConfig(
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=SessionId(f"proactive-{scenario.id}"),
        link="https://meet.google.com/eval-only",
        twin_name=profile.display_name,
        attention_enabled=True,
        attention_max_interjections=attention.max_interjections_per_10min,
        attention=attention,
    )
    outcome = ProactiveOutcome(
        scenario=scenario.id, kind=scenario.kind, expected_speak=scenario.expect.speak
    )
    lines: list[str] = []
    journal = Journal(config.meeting_id, emit=lines.append, echo=False)
    moment_detector = detector or build_detector(fixture, profile, settings, journal)
    await moment_detector.prepare()
    prefilter = AttentionPrefilter(max_per_window=attention.max_interjections_per_10min)

    async def deliver(context: AttentionContext, turns: Any, now: float) -> None:
        if not prefilter.allow(now):
            return
        prefilter.take(now)
        outcome.offers += 1
        turns.ingress.receive_attention(_attention_event(scenario, context), channel="voice")
        await _settle(turns)

    with (
        TemporaryDirectory() as folder,
        closing(MeetingRecord(Path(folder) / "record.jsonl")) as record,
    ):
        audio = FakeReplyAudio()
        async with assembly.open_conversation(
            settings,
            config,
            agent=assembly.AgentWiring(profile=profile, brief=fixture.brief, history=record),
            behavior=assembly.TurnBehavior(
                on_transition=lambda *_: None,
                wake_phrases=(profile.display_name,),
                twin_names=(profile.display_name,),
            ),
            diagnostics=Diagnostics(journal=journal),
        ) as turns:
            turns.lifecycle.bind(audio)
            for index, step in enumerate(scenario.steps):
                if step.screen is not None:
                    seen = step.screen.title
                    if step.screen.points:
                        seen = f"{seen}: {'; '.join(step.screen.points[:2])}"
                    await deliver(
                        AttentionContext(
                            kind="screen",
                            trigger_text=f"The shared screen is now {seen}.",
                            trigger_event_id=f"{scenario.id}-screen",
                        ),
                        turns,
                        step.at,
                    )
                    continue
                assert step.say is not None
                event = TranscriptEvent(
                    id=f"{scenario.id}-{index}",
                    timestamp=datetime.now(UTC).isoformat(),
                    source="meeting_audio",
                    text=step.say,
                    finalized=True,
                    participant_id=step.speaker,
                    speaker_evidence_id=None,
                )
                record.add_transcript(event)
                await turns.ingress.receive_final(event)
                await moment_detector.observe(event, now=step.at)
                ready = moment_detector.take_ready(now=step.at)
                if ready is not None:
                    await deliver(ready, turns, step.at)
                await _settle(turns)
            end = max((step.at for step in scenario.steps), default=0.0) + 60.0
            due = moment_detector.take_ready(now=end)
            if due is not None:
                await deliver(due, turns, end)
            await _settle(turns)
        outcome.spoken = list(audio.spoken)
        outcome.failures = [
            json.loads(line)["event"] for line in lines if "failed" in line or "fault" in line
        ]
    return outcome


async def replay_proactive(
    settings: Settings, *, runs: int = 1, fixture: ProactiveFixture | None = None
) -> ProactiveReport:
    fixture = fixture or load_proactive_fixture()
    profile = proactive_profile(fixture)
    outcomes: list[ProactiveOutcome] = []
    total = 0.0
    for _ in range(runs):
        for scenario in fixture.scenarios:
            outcomes.append(await run_proactive_scenario(scenario, fixture, profile, settings))
            total += scenario.duration_seconds
    return ProactiveReport(outcomes=outcomes, total_seconds=total)
