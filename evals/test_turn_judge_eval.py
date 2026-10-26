import json
import os
from pathlib import Path

import pytest

from evals._turn_judge import case_window, judge_cases, load_judge_fixture
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.participation_graph import JudgeThresholds
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
from fakeyou.orchestration.assembly import build_turn_judge
from tests._conversation_fakes import ScriptedJudge, judged

THRESHOLDS = JudgeThresholds(reply=0.6, clarify=0.5)
MAX_FALSE_REPLY_RATE = 0.05
MAX_MISSED_ANSWER_RATE = 0.10
MAX_PREMATURE_COMMIT_RATE = 0.02


def test_the_fixture_has_both_answers_and_silences_and_unique_ids() -> None:
    fixture = load_judge_fixture()

    assert len({case.id for case in fixture.cases}) == len(fixture.cases)
    assert any(case.answer for case in fixture.cases)
    assert any(not case.answer for case in fixture.cases)
    assert any(not case.finished for case in fixture.cases)
    assert any(case.kind == "named" for case in fixture.cases)


def test_a_case_becomes_a_window_with_the_twin_lines_marked() -> None:
    case = next(case for case in load_judge_fixture().cases if case.id == "j02")

    window = case_window(case)

    assert [line.twin for line in window.earlier] == [False, True]
    assert window.latest.speaker == "Ayu"


async def test_the_report_scores_misses_false_replies_and_early_finishes() -> None:
    fixture = load_judge_fixture()

    def always_reply(window: JudgeWindow) -> TurnJudgement:
        del window
        return judged("reply", finished=True)

    report = await judge_cases(ScriptedJudge(always_reply), fixture, runs=1, thresholds=THRESHOLDS)

    assert report.missed_named() == []
    assert report.missed_answer_rate() == 0.0
    assert report.false_reply_rate() == 1.0
    assert report.early_finish_rate() == 1.0
    assert report.premature_commit_rate() == 1.0


async def test_a_failing_judge_misses_every_answer() -> None:
    report = await judge_cases(
        ScriptedJudge(fail=TimeoutError()), load_judge_fixture(), runs=1, thresholds=THRESHOLDS
    )

    assert report.missed_answer_rate() == 1.0
    assert report.missed_named()
    assert report.failures()


@pytest.mark.live
async def test_live_turn_judge_meets_the_gates(tmp_path: Path) -> None:
    settings = Settings()
    try:
        judge = build_turn_judge(settings, load_judge_fixture().twin_names)
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    assert judge is not None
    runs = int(os.environ.get("TURN_JUDGE_RUNS", "3"))
    thresholds = JudgeThresholds(
        reply=settings.turn_judge.reply_threshold,
        clarify=settings.turn_judge.clarify_threshold,
        unnamed_reply=settings.turn_judge.unnamed_reply_threshold,
    )

    report = await judge_cases(judge, load_judge_fixture(), runs=runs, thresholds=thresholds)

    raw_target = os.environ.get("TURN_JUDGE_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    target.mkdir(parents=True, exist_ok=True)
    (target / "turn-judge.json").write_text(
        json.dumps(
            {"model": (settings.turn_judge.model or settings.reply).model, "runs": runs}
            | report.as_json(),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert report.failures() == []
    assert report.missed_named() == []
    assert report.false_reply_rate() <= MAX_FALSE_REPLY_RATE
    assert report.missed_answer_rate() <= MAX_MISSED_ANSWER_RATE
    assert report.premature_commit_rate() <= MAX_PREMATURE_COMMIT_RATE
