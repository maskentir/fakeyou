import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from fakeyou.conversation.participation_graph import JudgeThresholds, decision_of
from fakeyou.conversation.turn_judge import JudgeLine, JudgeWindow, TurnJudge, TurnJudgement
from fakeyou.observability.faults import capture_error

FIXTURE = Path(__file__).parent / "fixtures" / "turn-judge.json"


class FixtureLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    speaker: str
    text: str
    by_twin: bool = False


class JudgeCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    earlier: tuple[FixtureLine, ...]
    latest: FixtureLine
    answer: bool
    finished: bool


class JudgeFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    twin_names: tuple[str, ...]
    cases: tuple[JudgeCase, ...]


def load_judge_fixture(path: Path = FIXTURE) -> JudgeFixture:
    return JudgeFixture.model_validate_json(path.read_text(encoding="utf-8"))


def case_window(case: JudgeCase) -> JudgeWindow:
    return JudgeWindow(
        earlier=tuple(
            JudgeLine(speaker=line.speaker, text=line.text, twin=line.by_twin)
            for line in case.earlier
        ),
        latest=JudgeLine(speaker=case.latest.speaker, text=case.latest.text),
    )


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case: JudgeCase
    run: int
    judgement: TurnJudgement | None
    answered: bool
    latency_ms: float
    error: str | None = None


def _share(hits: int, total: int) -> float:
    return round(hits / total, 3) if total else 0.0


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 1) if ordered else 0.0


@dataclass(slots=True)
class JudgeReport:
    outcomes: list[CaseOutcome] = field(default_factory=list)

    def _where(
        self, *, answer: bool | None = None, finished: bool | None = None
    ) -> list[CaseOutcome]:
        return [
            outcome
            for outcome in self.outcomes
            if (answer is None or outcome.case.answer is answer)
            and (finished is None or outcome.case.finished is finished)
        ]

    def missed_named(self) -> list[str]:
        return [
            f"{outcome.case.id}/r{outcome.run}"
            for outcome in self._where(answer=True)
            if outcome.case.kind == "named" and not outcome.answered
        ]

    def missed_answer_rate(self) -> float:
        expected = self._where(answer=True)
        return _share(sum(not outcome.answered for outcome in expected), len(expected))

    def false_reply_rate(self) -> float:
        silent = self._where(answer=False)
        return _share(sum(outcome.answered for outcome in silent), len(silent))

    def early_finish_rate(self) -> float:
        unfinished = [o for o in self._where(finished=False) if o.judgement is not None]
        return _share(sum(o.judgement.finished for o in unfinished if o.judgement), len(unfinished))

    def premature_commit_rate(self) -> float:
        unfinished = [o for o in self._where(finished=False) if o.judgement is not None]
        cut_off = sum(o.answered and o.judgement.finished for o in unfinished if o.judgement)
        return _share(cut_off, len(unfinished))

    def failures(self) -> list[str]:
        return [f"{o.case.id}/r{o.run}:{o.error}" for o in self.outcomes if o.error is not None]

    def latency(self) -> dict[str, float]:
        values = [outcome.latency_ms for outcome in self.outcomes if outcome.error is None]
        return {"p50_ms": _percentile(values, 0.5), "p90_ms": _percentile(values, 0.9)}

    def as_json(self) -> dict[str, object]:
        return {
            "cases": len({outcome.case.id for outcome in self.outcomes}),
            "judgements": len(self.outcomes),
            "missed_named": self.missed_named(),
            "missed_answer_rate": self.missed_answer_rate(),
            "false_reply_rate": self.false_reply_rate(),
            "early_finish_rate": self.early_finish_rate(),
            "premature_commit_rate": self.premature_commit_rate(),
            "failures": self.failures(),
            "latency": self.latency(),
            "outcomes": [
                {
                    "case": outcome.case.id,
                    "kind": outcome.case.kind,
                    "run": outcome.run,
                    "expected_answer": outcome.case.answer,
                    "answered": outcome.answered,
                    "expected_finished": outcome.case.finished,
                    "finished": outcome.judgement.finished if outcome.judgement else None,
                    "addressee": outcome.judgement.addressee if outcome.judgement else None,
                    "action": outcome.judgement.action if outcome.judgement else None,
                    "confidence": outcome.judgement.confidence if outcome.judgement else None,
                    "latency_ms": round(outcome.latency_ms, 1),
                    "error": outcome.error,
                }
                for outcome in self.outcomes
            ],
        }


async def judge_cases(
    judge: TurnJudge, fixture: JudgeFixture, *, runs: int, thresholds: JudgeThresholds
) -> JudgeReport:
    report = JudgeReport()
    for run in range(runs):
        for case in fixture.cases:
            started = time.monotonic()
            judgement, error = await capture_error(partial(judge.judge, case_window(case)))
            if judgement is None:
                report.outcomes.append(
                    CaseOutcome(
                        case,
                        run,
                        judgement=None,
                        answered=False,
                        latency_ms=0.0,
                        error=type(error).__name__,
                    )
                )
                continue
            answered = decision_of(judgement, thresholds).decision != "silent"
            latency_ms = (time.monotonic() - started) * 1000
            report.outcomes.append(CaseOutcome(case, run, judgement, answered, latency_ms))
    return report
