import hashlib
import json
from typing import Any

import pytest

from fakeyou.observability.faults import Fault, FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.observability.report import build_session_report
from fakeyou.observability.scorecard import score_session
from tests._report_support import row


def session(*, answer_at: float | None = 8.0) -> list[dict[str, Any]]:
    rows = [
        row(0, "join_finished", source="runtime", status="admitted", scorecard_version=2),
        row(1, "conversation_bound", source="runtime"),
        row(10, "speech_stopped"),
        row(10.4, "transcript_finalized", transcript_id="t1", participant_id="p1"),
        row(10.5, "agent_started", request_id="t1:1"),
        row(11.5, "thinking_cue_audio_started", transcript_id="t1"),
        row(17, "reply_plan_observed", transcript_id="t1", intent="answer"),
        row(17, "agent_finished", request_id="t1:1"),
    ]
    if answer_at is not None:
        rows += [
            row(10 + answer_at, "playback_audio_started", transcript_id="t1", sentence_index=0),
            row(20, "playback_finished", transcript_id="t1", sentence_index=0),
        ]
    rows.append(row(3600, "session_finished", source="cli", exit_code=0))
    return rows


def test_cue_and_answer_have_independent_gates() -> None:
    report = build_session_report(session())
    card = score_session(report)

    assert card["version"] == 2
    assert card["metrics"]["first_audio_p50_seconds"]["value"] == 1.5
    assert card["metrics"]["first_answer_audio_p50_seconds"]["value"] == 8.0
    assert card["metrics"]["first_answer_audio_p50_seconds"]["passed"] is False
    assert card["passed"] is False
    assert report["turns"][0]["speech_stopped_to_first_answer_audio_seconds"] == 8.0


def test_cue_without_answer_does_not_disappear_from_coverage() -> None:
    rows = session(answer_at=None)
    rows.insert(-1, row(21, "agent_failed", request_id="t1:1"))
    card = score_session(build_session_report(rows))

    assert card["metrics"]["answer_audio_coverage"]["value"] == 0
    assert card["metrics"]["answer_audio_coverage"]["passed"] is False
    assert "first_answer_audio_p50_seconds" in card["unscored"]


@pytest.mark.parametrize("transcript_id", [None, "other-turn"])
def test_output_is_never_assigned_to_a_different_turn(transcript_id: str | None) -> None:
    rows = session(answer_at=None)
    rows.insert(-1, row(18, "playback_audio_started", transcript_id=transcript_id))
    report = build_session_report(rows)

    assert report["latency"]["speech_stopped_to_first_answer_audio_seconds"]["count"] == 0
    assert score_session(report)["metrics"]["answer_audio_coverage"]["value"] == 0


def test_missing_answer_fails_coverage_even_with_other_fast_answers() -> None:
    rows = session(answer_at=2)
    rows[-1:-1] = [
        row(30, "speech_stopped"),
        row(30.4, "transcript_finalized", transcript_id="t2", participant_id="p1"),
        row(31, "agent_started", request_id="t2:1"),
        row(31.5, "thinking_cue_audio_started", transcript_id="t2"),
        row(40, "agent_failed", request_id="t2:1"),
    ]
    card = score_session(build_session_report(rows))

    assert card["metrics"]["first_answer_audio_p50_seconds"]["passed"] is True
    assert card["metrics"]["answer_audio_coverage"]["value"] == 0.5
    assert card["passed"] is False


@pytest.mark.parametrize("authorized", [True, False])
def test_question_denominator_is_turns_and_exemption_is_sentence_specific(
    *, authorized: bool
) -> None:
    digest = hashlib.sha256(b"What does the vendor need?").hexdigest()
    rows = session(answer_at=2)
    rows[-2:-1] = [
        row(19, "playback_finished", transcript_id="t1", sentence_index=0),
        row(
            20,
            "answer_sentence_accepted",
            transcript_id="t1",
            sentence_index=1,
            sentence_sha256=digest,
            authorized_brief_question=authorized,
        ),
        row(
            21,
            "playback_finished",
            transcript_id="t1",
            sentence_index=1,
            sentence_sha256=digest,
            ends_with_question=True,
        ),
    ]
    report = build_session_report(rows)

    assert report["replies"]["spoken"] == 1
    assert report["replies"]["trailing_questions"] == 1
    assert report["replies"]["trailing_question_rate"] == 1
    assert report["replies"]["unnecessary_questions"] == (0 if authorized else 1)


def test_extra_question_cannot_reuse_a_validated_brief_question() -> None:
    rows = session(answer_at=2)
    rows[-2:-1] = [
        row(
            19,
            "answer_sentence_accepted",
            transcript_id="t1",
            sentence_index=0,
            sentence_sha256="brief",
            authorized_brief_question=True,
        ),
        row(
            20,
            "playback_finished",
            transcript_id="t1",
            sentence_index=0,
            sentence_sha256="extra",
            ends_with_question=True,
        ),
    ]
    assert build_session_report(rows)["replies"]["unnecessary_questions"] == 1


def test_fault_journal_counts_occurrences_and_retains_operation() -> None:
    lines: list[str] = []
    reporter = FaultReporter(journal=Journal("m1", emit=lines.append))
    for _ in range(7):
        reporter.record(Fault(source="browser", kind="timeout", operation="perception_drain"))

    report = build_session_report([json.loads(line) for line in lines])
    assert len(lines) == 7
    assert reporter.summaries()[0].count == 7
    assert report["faults"] == [
        {"source": "browser", "kind": "timeout", "operation": "perception_drain", "count": 7}
    ]


def test_old_journals_keep_the_original_scorecard() -> None:
    rows = [
        {key: value for key, value in entry.items() if key != "scorecard_version"}
        for entry in session()
    ]
    card = score_session(build_session_report(rows))

    assert card["version"] == 1
    assert "first_answer_audio_p50_seconds" not in card["metrics"]


def test_new_journal_rows_declare_the_metric_contract() -> None:
    lines: list[str] = []
    Journal("m1", emit=lines.append).record("session_finished", source="cli", exit_code=0)
    assert json.loads(lines[0])["scorecard_version"] == 2


@pytest.mark.parametrize(
    ("template", "output"), [("silent", None), ("action_only", None), ("speech_only", "chat")]
)
def test_non_voice_plans_and_their_speculative_drafts_do_not_require_audio(
    template: str, output: str | None
) -> None:
    rows = session(answer_at=2)
    rows[-1:-1] = [
        row(30, "speech_stopped"),
        row(30.4, "transcript_finalized", transcript_id="t2"),
        row(31, "agent_started", request_id="t2:1"),
        row(
            31.1, "reply_plan_observed", transcript_id="t2", intent="answer", request_id="t2:draft"
        ),
        row(31.2, "answer_sentence_emitted", transcript_id="t2", request_id="t2:draft"),
        row(32, "plan_selected", transcript_id="t2", template=template, output=output),
    ]
    assert build_session_report(rows)["replies"]["expected_answers"] == 1


def test_a_cancelled_slow_answer_remains_in_the_latency_distribution() -> None:
    rows = session()
    rows.insert(-1, row(21, "agent_cancelled", request_id="t1:1", reason="barge_in"))
    report = build_session_report(rows)
    assert report["latency"]["speech_stopped_to_first_answer_audio_seconds"]["p50"] == 8


def test_background_work_does_not_upgrade_a_historical_session() -> None:
    rows = [
        {key: value for key, value in entry.items() if key != "scorecard_version"}
        for entry in session()
    ]
    rows.append(row(3700, "memory_episode_ingested", source="memory_ingest", scorecard_version=2))
    assert score_session(build_session_report(rows))["version"] == 1


def test_pausing_whole_reply_generation_is_an_intentional_cancellation() -> None:
    rows = session(answer_at=2)
    rows[-1:-1] = [
        row(30, "speech_stopped"),
        row(30.4, "transcript_finalized", transcript_id="t2"),
        row(31, "agent_started", request_id="t2:1"),
        row(32, "turn_suppressed", request_id="t2:1", reason="paused"),
    ]
    assert build_session_report(rows)["replies"]["expected_answers"] == 1


def test_accepted_silent_draft_is_not_a_missing_answer() -> None:
    rows = session(answer_at=2)
    rows[-1:-1] = [
        row(30, "speech_stopped"),
        row(30.4, "transcript_finalized", transcript_id="t2"),
        row(31, "agent_started", request_id="t2:1"),
        row(
            31.1, "reply_plan_observed", transcript_id="t2", request_id="t2:draft", intent="silent"
        ),
        row(32, "plan_selected", transcript_id="t2", output="speech", template="speech_only"),
        row(33, "reply_draft_accepted", transcript_id="t2", request_id="t2:draft"),
    ]
    assert build_session_report(rows)["replies"]["expected_answers"] == 1


def test_coverage_cannot_round_a_missing_answer_up_to_a_pass() -> None:
    report = build_session_report(session(answer_at=2))
    report["replies"].update(expected_answers=10000, answers_with_audio=9999)
    assert score_session(report)["metrics"]["answer_audio_coverage"]["passed"] is False


def test_cancellation_cannot_hide_a_failed_answer() -> None:
    rows = session(answer_at=None)
    rows[-1:-1] = [
        row(21, "agent_failed", request_id="t1:1"),
        row(22, "agent_cancelled", request_id="t1:1", reason="close"),
    ]
    assert (
        score_session(build_session_report(rows))["metrics"]["answer_audio_coverage"]["value"] == 0
    )


def test_evidence_latencies_are_reported_separately_from_ordinary_answers() -> None:
    rows = session()
    rows.insert(
        5,
        row(
            10.6,
            "plan_selected",
            transcript_id="t1",
            output="speech",
            template="speech_only",
            evidence=["participant"],
        ),
    )
    report = build_session_report(rows)
    assert report["latency_by_evidence"]["participant"]["first_answer_audio_seconds"]["p50"] == 8
    assert report["latency_by_evidence"]["participant"]["expected_answers"] == 1


def test_a_question_followed_by_a_statement_is_still_an_unnecessary_question() -> None:
    rows = session(answer_at=2)
    rows[-2] = row(
        20,
        "playback_finished",
        transcript_id="t1",
        sentence_index=0,
        ends_with_question=False,
        contains_question=True,
    )
    replies = build_session_report(rows)["replies"]
    assert replies["trailing_questions"] == 0
    assert replies["unnecessary_questions"] == 1
