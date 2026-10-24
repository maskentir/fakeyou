from typing import Any

import pytest
from pydantic import ValidationError

from fakeyou.observability.report import build_session_report
from fakeyou.observability.scorecard import SESSION_TARGETS, SessionTargets, score_session
from tests._report_support import answered_turn, row


def _session(*, ending_question: bool = False, watchdog_failures: int = 0) -> list[dict[str, Any]]:
    closing = "Anything else?" if ending_question else "Sure."
    failures = [row(40.0 + index, "watchdog_failed") for index in range(watchdog_failures)]
    return [
        row(0.0, "join_finished", source="runtime", status="admitted"),
        row(1.0, "conversation_bound", source="runtime"),
        *answered_turn(10.0, "t1", "The demo is on Friday.", participant_id="speaker-a"),
        *answered_turn(30.0, "t2", closing, participant_id="speaker-a"),
        *failures,
        row(3600.0, "session_finished", source="cli", phase="ended", exit_code=0),
    ]


def test_a_healthy_session_passes_every_target() -> None:
    card = score_session(build_session_report(_session()))

    assert card["passed"] is True
    assert card["failed"] == card["unscored"] == []
    assert card["metrics"]["first_audio_p50_seconds"]["value"] == 2.5
    assert card["metrics"]["watchdog_failures_per_hour"]["value"] == 0.0


def test_misses_are_named_and_rates_are_per_reply_and_per_hour() -> None:
    card = score_session(build_session_report(_session(ending_question=True, watchdog_failures=40)))

    assert card["passed"] is False
    assert card["failed"] == ["trailing_question_rate", "watchdog_failures_per_hour"]
    assert card["metrics"]["trailing_question_rate"]["value"] == 0.5
    assert card["metrics"]["watchdog_failures_per_hour"] == {
        "max": 20.0,
        "value": 40.0,
        "passed": False,
    }


def test_a_session_without_evidence_is_unscored_not_passed() -> None:
    card = score_session(build_session_report([row(0.0, "join_requested", source="runtime")]))

    assert card["passed"] is False
    assert "session_completed" in card["unscored"]
    assert "first_audio_p50_seconds" in card["unscored"]


def test_targets_only_name_metrics_the_scorecard_can_compute() -> None:
    assert SESSION_TARGETS.version >= 1
    with pytest.raises(ValidationError, match="unscored metrics: smiles_per_hour"):
        SessionTargets.model_validate({"version": 1, "metrics": {"smiles_per_hour": {"min": 1}}})
