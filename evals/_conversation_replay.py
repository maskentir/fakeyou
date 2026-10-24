import asyncio
import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, override

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

import fakeyou.orchestration.assembly as assembly
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.sentences import sentence_end
from fakeyou.foundation.transcript import EventSource, TranscriptEvent
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.journal import Journal
from fakeyou.persistence.record import MeetingRecord
from tests._audio_fakes import FakeReplyAudio

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "evals" / "fixtures" / "conversation-scenarios.json"
JUDGE_LABELS = ROOT / "evals" / "fixtures" / "conversation-judge-labels.json"
BASELINE = ROOT / "evals" / "fixtures" / "conversation-baseline.json"
MIN_JUDGE_AGREEMENT = 0.9
FAMILY_TOLERANCE = 0.2
STEP_TIMEOUT_SECONDS = 60.0
LANGUAGE_NAMES = {"en": "English", "id": "Indonesian"}
JUDGE_RULES = (
    "You grade one reply from a meeting participant's digital twin. You receive the meeting "
    "so far, the twin's reply to the last line, and one criterion. Participant lines come from "
    "speech recognition and may contain misheard words. Decide only whether the reply meets "
    "the criterion; ignore style. Return met=true or met=false with a one-sentence reason."
)

Severity = Literal["severe", "quality"]


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reply: bool | None = True
    max_sentences: int | None = Field(default=None, gt=0)
    allow_question: bool = False
    forbid: tuple[str, ...] = ()
    means: str | None = None
    language: Literal["en", "id"] | None = None
    severity: Severity = "quality"


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    say: str
    speaker: str
    claim: str | None = None
    cut_after: int | None = Field(default=None, ge=0)
    expect: Expectation | None = None
    source: EventSource = "meeting_audio"


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    family: str
    title: str
    steps: tuple[Step, ...]


class ScenarioFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    persona: str
    persona_overrides: dict[str, Any] = {}
    scenarios: tuple[Scenario, ...]


class JudgeLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case: str
    context: str
    reply: str
    criterion: str
    met: bool


class JudgeLabels(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    labels: tuple[JudgeLabel, ...]


class ReplayBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reply_model: str
    runs: int = Field(gt=0)
    families: dict[str, float]


class Verdict(BaseModel):
    met: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SpokenLine:
    text: str
    interrupted: bool


@dataclass(slots=True)
class StepOutcome:
    scenario: str
    family: str
    index: int
    said: str
    spoken: list[SpokenLine]
    latency_ms: float = 0.0
    failures: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    severity: Severity = "quality"

    @property
    def reply(self) -> str:
        return " ".join(line.text for line in self.spoken)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


Judge = Callable[[str, str, str], Awaitable[Verdict]]


class FakeChat:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, *, transcript_id: str) -> bool:
        del transcript_id
        self.sent.append(text)
        return True


class ScenarioAudio(FakeReplyAudio):
    def __init__(self, record: MeetingRecord) -> None:
        super().__init__()
        self._record = record
        self._cut_after: int | None = None
        self.turn: list[SpokenLine] = []

    def begin(self, cut_after: int | None) -> None:
        self._cut_after = cut_after
        self.turn = []

    @override
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        del language
        cut = self._cut_after
        if cut is not None and len(self.turn) > cut:
            return
        line = SpokenLine(text, interrupted=cut is not None and len(self.turn) == cut)
        self._record.add_twin_speech(text, interrupted=line.interrupted)
        self.turn.append(line)


def load_scenarios(path: Path = FIXTURE) -> ScenarioFixture:
    return ScenarioFixture.model_validate_json(path.read_text(encoding="utf-8"))


def scenario_profile(fixture: ScenarioFixture) -> PersonaProfile:
    base = json.loads((ROOT / fixture.persona).read_text(encoding="utf-8"))
    return PersonaProfile.model_validate({**base, **fixture.persona_overrides})


def sentence_count(text: str, language: str) -> int:
    count = 0
    remaining = text.strip()
    while remaining:
        boundary = sentence_end(remaining, language=language) or len(remaining)
        count += 1
        remaining = remaining[boundary:].strip()
    return count


def _event(scenario: Scenario, index: int, step: Step) -> TranscriptEvent:
    return TranscriptEvent(
        id=f"{scenario.id}-{index}",
        timestamp=datetime.now(UTC).isoformat(),
        source=step.source,
        text=step.say,
        finalized=True,
        participant_id=step.speaker,
        speaker_evidence_id=None,
        display_claim=step.claim,
    )


async def _settle(turns: Any) -> None:
    for _ in range(200):
        await turns.lifecycle.wait_idle()
        if not turns.lifecycle.deferred.pending and not turns.lifecycle.is_busy:
            return
        await asyncio.sleep(0.02)


async def run_scenario(
    scenario: Scenario, profile: PersonaProfile, settings: Settings
) -> list[StepOutcome]:
    config = SessionConfig(
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=SessionId(f"replay-{scenario.id}"),
        link="https://meet.google.com/eval-only",
        twin_name=profile.display_name,
    )
    outcomes: list[StepOutcome] = []
    lines: list[str] = []
    journal = Journal(config.meeting_id, emit=lines.append, echo=False)
    with (
        TemporaryDirectory() as folder,
        closing(MeetingRecord(Path(folder) / "record.jsonl")) as record,
    ):
        audio = ScenarioAudio(record)
        chat = FakeChat()
        async with assembly.open_conversation(
            settings,
            config,
            agent=assembly.AgentWiring(profile=profile, history=record),
            behavior=assembly.TurnBehavior(
                on_transition=lambda *_: None,
                wake_phrases=(profile.display_name,),
                twin_names=(profile.display_name,),
                barge_in=True,
                chat=chat,
            ),
            diagnostics=Diagnostics(journal=journal),
        ) as turns:
            turns.lifecycle.bind(audio)
            for index, step in enumerate(scenario.steps):
                event = _event(scenario, index, step)
                record.add_transcript(event)
                audio.begin(step.cut_after)
                started = time.monotonic()
                if step.source == "meeting_audio":
                    await turns.ingress.receive_final(event)
                else:
                    turns.ingress.receive_chat(event)
                await _settle(turns)
                spoken = audio.turn
                if step.source != "meeting_audio":
                    spoken = [SpokenLine(text=text, interrupted=False) for text in chat.sent]
                    chat.sent.clear()
                outcomes.append(
                    StepOutcome(
                        scenario.id,
                        scenario.family,
                        index,
                        step.say,
                        spoken,
                        (time.monotonic() - started) * 1000,
                        failures=_turn_failures(lines, event.id),
                    )
                )
    return outcomes


def _turn_failures(lines: Sequence[str], transcript_id: str) -> list[str]:
    rows = (json.loads(line) for line in lines)
    return [
        row["error"]
        for row in rows
        if row["event"] == "turn_failed" and row.get("transcript_id") == transcript_id
    ]


def _transcript(scenario: Scenario, outcomes: Sequence[StepOutcome], upto: int) -> str:
    lines: list[str] = []
    for step, outcome in zip(scenario.steps[: upto + 1], outcomes, strict=False):
        lines.append(f"{step.claim or step.speaker}: {step.say}")
        if outcome.index < upto and outcome.spoken:
            lines.append(f"Twin: {outcome.reply}")
    return "\n".join(lines)


def _shape_checks(expect: Expectation, reply: str, language: str) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    if expect.max_sentences is not None:
        checks["sentences"] = sentence_count(reply, language) <= expect.max_sentences
    if not expect.allow_question:
        checks["no_closing_question"] = not reply.rstrip().endswith("?")
    if expect.forbid:
        folded = reply.casefold()
        checks["forbidden"] = not any(term.casefold() in folded for term in expect.forbid)
    return checks


def _criteria(expect: Expectation) -> dict[str, str]:
    criteria = {"means": expect.means} if expect.means is not None else {}
    if expect.language is not None:
        criteria["language"] = f"The reply is written in {LANGUAGE_NAMES[expect.language]}."
    return criteria


async def grade(
    scenario: Scenario, outcomes: list[StepOutcome], judge: Judge, language: str
) -> list[StepOutcome]:
    for step, outcome in zip(scenario.steps, outcomes, strict=True):
        if outcome.failures:
            outcome.checks["turn_completed"] = False
        expect = step.expect
        if expect is None:
            continue
        outcome.severity = expect.severity
        reply = outcome.reply
        if expect.reply is not None:
            outcome.checks["replied" if expect.reply else "silent"] = bool(reply) == expect.reply
        if expect.reply is False or not reply:
            continue
        outcome.checks.update(_shape_checks(expect, reply, language))
        context = _transcript(scenario, outcomes, outcome.index)
        for name, criterion in _criteria(expect).items():
            verdict = await judge(context, reply, criterion)
            outcome.checks[name] = verdict.met
            outcome.reasons[name] = verdict.reason
    return outcomes


def model_judge(model: BaseChatModel) -> Judge:
    structured = model.with_structured_output(Verdict)

    async def judge(context: str, reply: str, criterion: str) -> Verdict:
        verdict = await structured.ainvoke(
            [
                SystemMessage(JUDGE_RULES),
                HumanMessage(
                    f"Meeting so far:\n{context}\n\nTwin reply:\n{reply}\n\nCriterion: {criterion}"
                ),
            ]
        )
        if not isinstance(verdict, Verdict):
            raise TypeError("judge returned no verdict")
        return verdict

    return judge


@dataclass(slots=True)
class ReplayReport:
    outcomes: list[StepOutcome]

    def graded(self) -> list[StepOutcome]:
        return [outcome for outcome in self.outcomes if outcome.checks]

    def severe_failures(self) -> list[StepOutcome]:
        return [o for o in self.graded() if o.severity == "severe" and not o.passed]

    def families(self) -> dict[str, dict[str, float | int]]:
        totals: Counter[str] = Counter()
        passed: Counter[str] = Counter()
        for outcome in self.graded():
            totals[outcome.family] += 1
            passed[outcome.family] += int(outcome.passed)
        return {
            family: {
                "steps": totals[family],
                "passed": passed[family],
                "rate": round(passed[family] / totals[family], 3),
            }
            for family in sorted(totals)
        }

    def latencies(self) -> dict[str, float]:
        by_family: dict[str, list[float]] = {}
        for outcome in self.graded():
            by_family.setdefault(outcome.family, []).append(outcome.latency_ms)
        return {
            family: round(max(values), 1) for family, values in sorted(by_family.items())
        }

    def checks(self) -> dict[str, dict[str, int]]:
        tally: dict[str, Counter[str]] = {}
        for outcome in self.graded():
            for name, met in outcome.checks.items():
                tally.setdefault(name, Counter())["met" if met else "missed"] += 1
        return {name: dict(counts) for name, counts in sorted(tally.items())}

    def regressions(self, baseline: ReplayBaseline) -> dict[str, float]:
        rates = {family: float(row["rate"]) for family, row in self.families().items()}
        return {
            family: rates.get(family, 0.0)
            for family, floor in baseline.families.items()
            if rates.get(family, 0.0) < floor - FAMILY_TOLERANCE
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "families": self.families(),
            "latency_ms": self.latencies(),
            "checks": self.checks(),
            "severe_failures": [f"{o.scenario}#{o.index}" for o in self.severe_failures()],
            "steps": [
                {
                    "scenario": o.scenario,
                    "family": o.family,
                    "index": o.index,
                    "said": o.said,
                    "latency_ms": round(o.latency_ms, 1),
                    "reply": [
                        {"text": line.text, "interrupted": line.interrupted} for line in o.spoken
                    ],
                    "failures": o.failures,
                    "checks": o.checks,
                    "reasons": o.reasons,
                    "severity": o.severity,
                }
                for o in self.outcomes
            ],
        }


async def replay_conversations(
    fixture: ScenarioFixture, settings: Settings, judge: Judge, *, runs: int = 1
) -> ReplayReport:
    profile = scenario_profile(fixture)
    outcomes: list[StepOutcome] = []
    for _ in range(runs):
        for scenario in fixture.scenarios:
            played = await run_scenario(scenario, profile, settings)
            outcomes += await grade(scenario, played, judge, profile.language)
    return ReplayReport(outcomes)


def load_baseline(path: Path = BASELINE) -> ReplayBaseline:
    return ReplayBaseline.model_validate_json(path.read_text(encoding="utf-8"))


def load_judge_labels(path: Path = JUDGE_LABELS) -> tuple[JudgeLabel, ...]:
    return JudgeLabels.model_validate_json(path.read_text(encoding="utf-8")).labels


async def judge_disagreements(judge: Judge, labels: Sequence[JudgeLabel]) -> list[str]:
    verdicts = await asyncio.gather(
        *(judge(label.context, label.reply, label.criterion) for label in labels)
    )
    return [
        label.case
        for label, verdict in zip(labels, verdicts, strict=True)
        if verdict.met != label.met
    ]
