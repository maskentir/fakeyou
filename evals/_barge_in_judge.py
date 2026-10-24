import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from fakeyou.conversation.participation_graph import JudgeThresholds
from fakeyou.conversation.turn_ingress import judge_yields_the_floor
from fakeyou.conversation.turn_judge import JudgeLine, JudgeWindow, TurnJudge, TurnJudgement
from fakeyou.observability.faults import capture_error

FIXTURE = Path(__file__).parent / "fixtures" / "turn-judge-barge-in.json"


class BargeInLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    speaker: str
    text: str
    by_twin: bool = False


class BargeInCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    earlier: tuple[BargeInLine, ...]
    latest: BargeInLine
    barge_in: bool


class BargeInFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    twin_names: tuple[str, ...]
    cases: tuple[BargeInCase, ...]


def load_barge_in_fixture(path: Path = FIXTURE) -> BargeInFixture:
    return BargeInFixture.model_validate_json(path.read_text(encoding="utf-8"))


def barge_in_window(case: BargeInCase) -> JudgeWindow:
    return JudgeWindow(
        earlier=tuple(
            JudgeLine(speaker=line.speaker, text=line.text, twin=line.by_twin)
            for line in case.earlier
        ),
        latest=JudgeLine(speaker=case.latest.speaker, text=case.latest.text),
        twin_speaking=True,
    )


@dataclass(frozen=True, slots=True)
class BargeInOutcome:
    case: BargeInCase
    run: int
    judgement: TurnJudgement | None
    yields: bool
    latency_ms: float
    error: str | None = None


def _share(hits: int, total: int) -> float:
    return round(hits / total, 3) if total else 0.0


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * share))], 1) if ordered else 0.0


@dataclass(slots=True)
class BargeInReport:
    outcomes: list[BargeInOutcome] = field(default_factory=list)

    def _where(self, *, expected: bool) -> list[BargeInOutcome]:
        return [outcome for outcome in self.outcomes if outcome.case.barge_in is expected]

    def missed_barge_in_rate(self) -> float:
        expected = self._where(expected=True)
        return _share(sum(not outcome.yields for outcome in expected), len(expected))

    def false_barge_in_rate(self) -> float:
        expected = self._where(expected=False)
        return _share(sum(outcome.yields for outcome in expected), len(expected))

    def failures(self) -> list[str]:
        return [f"{o.case.id}/r{o.run}:{o.error}" for o in self.outcomes if o.error is not None]

    def latency(self) -> dict[str, float]:
        values = [outcome.latency_ms for outcome in self.outcomes if outcome.error is None]
        return {"p50_ms": _percentile(values, 0.5), "p90_ms": _percentile(values, 0.9)}

    def as_json(self) -> dict[str, object]:
        return {
            "cases": len({outcome.case.id for outcome in self.outcomes}),
            "judgements": len(self.outcomes),
            "missed_barge_in_rate": self.missed_barge_in_rate(),
            "false_barge_in_rate": self.false_barge_in_rate(),
            "failures": self.failures(),
            "latency": self.latency(),
            "outcomes": [
                {
                    "case": outcome.case.id,
                    "kind": outcome.case.kind,
                    "run": outcome.run,
                    "expected_barge_in": outcome.case.barge_in,
                    "yields": outcome.yields,
                    "addressee": outcome.judgement.addressee if outcome.judgement else None,
                    "action": outcome.judgement.action if outcome.judgement else None,
                    "backchannel": outcome.judgement.backchannel if outcome.judgement else None,
                    "confidence": outcome.judgement.confidence if outcome.judgement else None,
                    "latency_ms": round(outcome.latency_ms, 1),
                    "error": outcome.error,
                }
                for outcome in self.outcomes
            ],
        }


async def judge_barge_in_cases(
    judge: TurnJudge, fixture: BargeInFixture, *, runs: int, thresholds: JudgeThresholds
) -> BargeInReport:
    report = BargeInReport()
    for run in range(runs):
        for case in fixture.cases:
            started = time.monotonic()
            judgement, error = await capture_error(partial(judge.judge, barge_in_window(case)))
            if judgement is None:
                report.outcomes.append(
                    BargeInOutcome(
                        case,
                        run,
                        judgement=None,
                        yields=False,
                        latency_ms=0.0,
                        error=type(error).__name__,
                    )
                )
                continue
            yields = judge_yields_the_floor(judgement, thresholds)
            latency_ms = (time.monotonic() - started) * 1000
            report.outcomes.append(BargeInOutcome(case, run, judgement, yields, latency_ms))
    return report
