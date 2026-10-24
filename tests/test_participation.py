import pytest

from fakeyou.conversation.participation_graph import (
    JudgeThresholds,
    decision_of,
    fallback_decision,
    judge_failure_code,
)
from fakeyou.conversation.turn_judge import TurnJudgement
from fakeyou.foundation.participation import (
    ParticipationContext,
    is_vocative,
    mentions_whole_phrase,
)

THRESHOLDS = JudgeThresholds(reply=0.6, clarify=0.5)


def judgement(**changes: object) -> TurnJudgement:
    values: dict[str, object] = {
        "heard": "When is the demo?",
        "finished": True,
        "backchannel": False,
        "addressee": "twin",
        "action": "reply",
        "confidence": 0.9,
    }
    return TurnJudgement.model_validate(values | changes)


def test_a_confident_reply_is_spoken() -> None:
    decision = decision_of(judgement(), THRESHOLDS)

    assert (decision.decision, decision.reason_code) == ("speak", "judge_reply")


def test_a_reply_below_the_threshold_stays_silent() -> None:
    decision = decision_of(judgement(confidence=0.4), THRESHOLDS)

    assert (decision.decision, decision.reason_code) == ("silent", "judge_below_threshold")


def test_a_clarify_uses_its_own_threshold() -> None:
    assert decision_of(judgement(action="clarify", confidence=0.55), THRESHOLDS).decision == (
        "clarify"
    )
    assert decision_of(judgement(action="clarify", confidence=0.45), THRESHOLDS).decision == (
        "silent"
    )


def test_staying_silent_needs_no_confidence() -> None:
    decision = decision_of(
        judgement(action="stay_silent", addressee="someone_else", confidence=0.1), THRESHOLDS
    )

    assert (decision.decision, decision.reason_code) == ("silent", "judge_stay_silent")


def test_a_backchannel_is_carried_to_the_decision() -> None:
    decision = decision_of(judgement(action="stay_silent", backchannel=True), THRESHOLDS)

    assert decision.backchannel is True


def test_without_a_judge_only_a_named_line_gets_a_reply() -> None:
    assert fallback_decision(ParticipationContext(addressed=True)).decision == "speak"
    assert fallback_decision(ParticipationContext(addressed=False)).decision == "silent"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TimeoutError(), "judge_timeout"),
        (ConnectionError(), "judge_unavailable"),
        (None, "judge_unavailable"),
    ],
)
def test_judge_failures_have_codes(error: BaseException | None, code: str) -> None:
    assert judge_failure_code(error) == code


def test_vocative_matches_name_at_utterance_start() -> None:
    assert is_vocative("Rani Twin, when is the demo?", ["Rani Twin"])
    assert is_vocative("rani twin when is the demo?", ["Rani Twin"])
    assert is_vocative("Twin: hello", ["Twin"])
    assert is_vocative("Rani Twin", ["Rani Twin"])
    assert not is_vocative("About Rani Twin yesterday", ["Rani Twin"])
    assert not is_vocative("He said Rani Twin agrees", ["Rani Twin"])
    assert not is_vocative("Rani Twinagrees yes?", ["Rani Twin"])
    assert not is_vocative("Hello", ["Rani Twin"])
    assert not is_vocative("Rani Twin, hello", ["", "  "])


def test_vocative_tries_every_alias() -> None:
    assert is_vocative("Twin, hello", ["Rani Twin", "Twin"])
    assert is_vocative("Rani Twin, hello", ["Rani Twin", "Twin"])
    assert not is_vocative("Twinx, hello", ["Rani Twin", "Twin"])


def test_a_name_counts_only_as_a_whole_phrase() -> None:
    assert mentions_whole_phrase("so Rani, what now?", "Rani")
    assert not mentions_whole_phrase("Ranita said so", "Rani")
    assert not mentions_whole_phrase("anything", " ")
