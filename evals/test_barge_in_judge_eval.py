import json
import os
from pathlib import Path

import pytest

from evals._barge_in_judge import (
    barge_in_window,
    judge_barge_in_cases,
    load_barge_in_fixture,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.participation_graph import JudgeThresholds
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
from fakeyou.orchestration.assembly import build_turn_judge
from tests._conversation_fakes import ScriptedJudge, judged

THRESHOLDS = JudgeThresholds(reply=0.6, clarify=0.5, barge_in=0.6)
MAX_MISSED_BARGE_IN_RATE = 0.10
MAX_FALSE_BARGE_IN_RATE = 0.05


def test_the_fixture_has_both_yields_and_holds_and_unique_ids() -> None:
    fixture = load_barge_in_fixture()

    assert len({case.id for case in fixture.cases}) == len(fixture.cases)
    assert any(case.barge_in for case in fixture.cases)
    assert any(not case.barge_in for case in fixture.cases)
    assert any(case.kind == "named" for case in fixture.cases)
    assert any(case.kind == "other_person" for case in fixture.cases)


def test_a_case_becomes_a_window_with_the_twin_speaking() -> None:
    case = next(case for case in load_barge_in_fixture().cases if case.id == "b04")

    window = barge_in_window(case)

    assert window.twin_speaking is True
    assert [line.twin for line in window.earlier] == [False, True]
    assert window.latest.speaker == "Ayu"


async def test_the_report_counts_missed_and_false_yields() -> None:
    fixture = load_barge_in_fixture()

    def always_yields(window: JudgeWindow) -> TurnJudgement:
        del window
        return judged("reply", addressee="twin")

    report = await judge_barge_in_cases(
        ScriptedJudge(always_yields), fixture, runs=1, thresholds=THRESHOLDS
    )

    assert report.missed_barge_in_rate() == 0.0
    assert report.false_barge_in_rate() == 1.0


async def test_a_failing_judge_misses_every_yield() -> None:
    report = await judge_barge_in_cases(
        ScriptedJudge(fail=TimeoutError()),
        load_barge_in_fixture(),
        runs=1,
        thresholds=THRESHOLDS,
    )

    assert report.missed_barge_in_rate() == 1.0
    assert report.failures()


async def test_a_quiet_judge_never_takes_the_floor() -> None:
    def stays_quiet(window: JudgeWindow) -> TurnJudgement:
        del window
        return judged("stay_silent", addressee="someone_else")

    report = await judge_barge_in_cases(
        ScriptedJudge(stays_quiet), load_barge_in_fixture(), runs=1, thresholds=THRESHOLDS
    )

    assert report.missed_barge_in_rate() == 1.0
    assert report.false_barge_in_rate() == 0.0


@pytest.mark.live
async def test_live_barge_in_judge_meets_the_gates(tmp_path: Path) -> None:
    settings = Settings()
    try:
        judge = build_turn_judge(settings, load_barge_in_fixture().twin_names)
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    assert judge is not None
    runs = int(os.environ.get("BARGE_IN_JUDGE_RUNS", "3"))
    thresholds = JudgeThresholds(
        reply=settings.turn_judge.reply_threshold,
        clarify=settings.turn_judge.clarify_threshold,
        barge_in=settings.turn_judge.barge_in_threshold,
    )

    report = await judge_barge_in_cases(
        judge, load_barge_in_fixture(), runs=runs, thresholds=thresholds
    )

    raw_target = os.environ.get("BARGE_IN_JUDGE_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    target.mkdir(parents=True, exist_ok=True)
    (target / "turn-judge-barge-in.json").write_text(
        json.dumps(
            {"model": (settings.turn_judge.model or settings.reply).model, "runs": runs}
            | report.as_json(),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert report.failures() == []
    assert report.missed_barge_in_rate() <= MAX_MISSED_BARGE_IN_RATE
    assert report.false_barge_in_rate() <= MAX_FALSE_BARGE_IN_RATE
