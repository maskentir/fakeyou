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
        row(
            0,
            "join_finished",
            source="runtime",
            status="admitted",
            answer_boundary=True,
            evidence_paths=True,
        ),
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

    assert card["version"] == 3
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


def test_journals_without_the_answer_boundary_leave_the_new_gates_unscored() -> None:
    rows = [
        {key: value for key, value in entry.items() if key != "answer_boundary"}
        for entry in session()
    ]
    report = build_session_report(rows)
    card = score_session(report)

    assert report["replies"]["unnecessary_questions"] is None
    assert report["replies"]["answers_with_audio"] is None
    assert card["metrics"]["answer_audio_coverage"]["value"] is None
    assert card["metrics"]["unnecessary_question_rate"]["value"] is None
    assert card["metrics"]["first_answer_audio_p50_seconds"]["value"] is None
    assert card["failed"] == []


def test_a_journal_without_evidence_paths_keeps_the_earlier_scorecard() -> None:
    rows = [
        {
            key: value
            for key, value in entry.items()
            if key not in {"evidence_paths", "evidence_resolution_v2"}
        }
        for entry in session()
    ]
    card = score_session(build_session_report(rows))

    assert card["version"] == 2
    assert "first_answer_audio_p50_visual_cached_seconds" not in card["metrics"]


def test_evidence_paths_are_scored_and_reported_per_path() -> None:
    rows = session()
    rows.append(
        row(
            12,
            "perception_evidence_resolved",
            source="perception",
            transcript_id="t1",
            evidence_kind="participant",
            resolution="cache_hit",
            age_ms=4300,
            vision_latency_ms=0,
        )
    )
    report = build_session_report(rows)
    card = score_session(report)

    assert report["evidence_paths"] is True
    assert report["evidence_cache"]["hits"] == 1
    assert report["evidence_cache"]["hit_rate"] == 1.0
    assert report["latency_by_evidence_path"]["visual_cached"]["expected_answers"] == 1
    assert card["version"] == 3
    assert card["metrics"]["first_answer_audio_p50_visual_cached_seconds"]["value"] == 8.0
    assert card["metrics"]["first_answer_audio_p50_visual_cached_seconds"]["status"] == "failed"


def fast_session(*, answer_at: float = 2.0, evidence: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    rows = [
        row(
            0,
            "join_finished",
            source="runtime",
            status="admitted",
            answer_boundary=True,
            evidence_paths=True,
        ),
        row(1, "conversation_bound", source="runtime"),
        row(10, "speech_stopped"),
        row(10.4, "transcript_finalized", transcript_id="t1", participant_id="p1"),
        row(10.5, "agent_started", request_id="t1:1"),
        row(
            10.6,
            "plan_selected",
            transcript_id="t1",
            output="speech",
            template="answer",
            evidence=list(evidence),
        ),
        row(11.0, "thinking_cue_audio_started", transcript_id="t1"),
        row(11.5, "reply_plan_observed", transcript_id="t1", intent="answer"),
        row(11.5, "agent_finished", request_id="t1:1"),
        row(10 + answer_at, "playback_audio_started", transcript_id="t1", sentence_index=0),
        row(20, "playback_finished", transcript_id="t1", sentence_index=0),
        row(3600, "session_finished", source="cli", exit_code=0),
    ]
    return rows


def test_a_no_visual_session_passes_without_visual_cached_answers() -> None:
    card = score_session(build_session_report(fast_session()))

    assert card["passed"] is True
    assert card["metrics"]["first_answer_audio_p50_no_visual_seconds"]["status"] == "passed"
    assert card["metrics"]["first_answer_audio_p95_no_visual_seconds"]["status"] == "passed"
    assert (
        card["metrics"]["first_answer_audio_p50_visual_cached_seconds"]["status"]
        == "not_applicable"
    )
    assert (
        card["metrics"]["first_answer_audio_p95_visual_cached_seconds"]["status"]
        == "not_applicable"
    )
    assert card["metrics"]["first_answer_audio_p50_visual_cached_seconds"]["passed"] is None
    assert "first_answer_audio_p50_visual_cached_seconds" not in card["unscored"]
    assert "first_answer_audio_p50_visual_cached_seconds" not in card["failed"]


def test_a_visual_cached_session_passes_without_no_visual_answers() -> None:
    rows = fast_session(evidence=("screen",))
    card = score_session(build_session_report(rows))

    assert card["passed"] is True
    assert card["metrics"]["first_answer_audio_p50_visual_cached_seconds"]["status"] == "passed"
    assert card["metrics"]["first_answer_audio_p50_no_visual_seconds"]["status"] == "not_applicable"
    assert "first_answer_audio_p50_no_visual_seconds" not in card["unscored"]
    assert "first_answer_audio_p50_no_visual_seconds" not in card["failed"]


def test_an_expected_answer_without_measured_audio_cannot_pass() -> None:
    rows = fast_session()
    rows = [
        entry
        for entry in rows
        if not (
            entry.get("event") == "playback_audio_started" and entry.get("transcript_id") == "t1"
        )
    ]
    rows.insert(-1, row(12, "agent_failed", request_id="t1:1"))
    card = score_session(build_session_report(rows))

    assert card["metrics"]["first_answer_audio_p50_no_visual_seconds"]["status"] == "unscored"
    assert card["metrics"]["first_answer_audio_p50_no_visual_seconds"]["value"] is None
    assert "first_answer_audio_p50_no_visual_seconds" in card["unscored"]
    assert card["passed"] is False


def test_a_session_without_expected_answers_in_gated_paths_follows_global_gates() -> None:
    rows = fast_session(evidence=("participant",))
    rows.append(
        row(
            12,
            "perception_evidence_resolved",
            source="perception",
            transcript_id="t1",
            evidence_kind="participant",
            resolution="fresh",
            age_ms=0,
            vision_latency_ms=3000,
        )
    )
    card = score_session(build_session_report(rows))

    assert card["metrics"]["first_answer_audio_p50_no_visual_seconds"]["status"] == "not_applicable"
    assert (
        card["metrics"]["first_answer_audio_p50_visual_cached_seconds"]["status"]
        == "not_applicable"
    )
    assert card["metrics"]["first_answer_audio_p50_seconds"]["status"] == "passed"
    assert card["passed"] is True


def test_historical_journals_without_evidence_paths_keep_their_behaviour() -> None:
    rows = fast_session()
    rows = [
        {
            key: value
            for key, value in entry.items()
            if key not in {"evidence_paths", "evidence_resolution_v2"}
        }
        for entry in rows
    ]
    card = score_session(build_session_report(rows))

    assert card["version"] == 2
    assert "first_answer_audio_p50_visual_cached_seconds" not in card["metrics"]
    assert "first_answer_audio_p50_no_visual_seconds" not in card["metrics"]
    assert card["passed"] is True
    assert card["metrics"]["first_answer_audio_p50_seconds"]["status"] == "passed"


def test_new_journal_rows_declare_the_answer_boundary() -> None:
    lines: list[str] = []
    Journal("m1", emit=lines.append).record("session_finished", source="cli", exit_code=0)
    assert json.loads(lines[0])["answer_boundary"] is True


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
        {key: value for key, value in entry.items() if key != "answer_boundary"}
        for entry in session()
    ]
    rows.append(row(3700, "memory_episode_ingested", source="memory_ingest"))
    report = build_session_report(rows)

    assert report["replies"]["answers_with_audio"] is None


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


def v2_session(
    *,
    evidence: tuple[str, ...] = (),
    resolutions: tuple[tuple[str, str, str], ...] = (),
    answer_at: float | None = 2.0,
) -> list[dict[str, Any]]:
    rows = [
        row(
            0,
            "join_finished",
            source="runtime",
            status="admitted",
            answer_boundary=True,
            evidence_paths=True,
            evidence_resolution_v2=True,
        ),
        row(1, "conversation_bound", source="runtime"),
        row(10, "speech_stopped"),
        row(10.4, "transcript_finalized", transcript_id="t1", participant_id="p1"),
        row(10.5, "agent_started", request_id="t1:1"),
        row(
            10.6,
            "plan_selected",
            transcript_id="t1",
            output="speech",
            template="answer",
            evidence=list(evidence),
        ),
        row(11.0, "thinking_cue_audio_started", transcript_id="t1"),
        row(11.5, "reply_plan_observed", transcript_id="t1", intent="answer"),
        row(11.5, "agent_finished", request_id="t1:1"),
    ]
    for index, (kind, resolution, _at_offset) in enumerate(resolutions):
        rows.append(
            row(
                12 + index * 0.1,
                "perception_evidence_resolved",
                source="perception",
                transcript_id="t1",
                evidence_kind=kind,
                resolution=resolution,
                age_ms=0,
                vision_latency_ms=100 if resolution == "fresh" else 0,
            )
        )
    if answer_at is not None:
        rows += [
            row(10 + answer_at, "playback_audio_started", transcript_id="t1", sentence_index=0),
            row(20, "playback_finished", transcript_id="t1", sentence_index=0),
        ]
    rows.append(row(3600, "session_finished", source="cli", exit_code=0))
    return rows


def path_of(report: dict[str, Any]) -> str | None:
    paths = report["latency_by_evidence_path"]
    used = [name for name, group in paths.items() if group["expected_answers"] > 0]
    assert len(used) == 1
    return str(used[0])


def test_v2_no_visual_request_is_no_visual() -> None:
    report = build_session_report(v2_session())
    assert path_of(report) == "no_visual"
    assert score_session(report)["passed"] is True


def test_v2_fresh_screen_look_is_visual_fresh() -> None:
    report = build_session_report(
        v2_session(evidence=("screen",), resolutions=(("screen", "fresh", "12"),))
    )
    assert path_of(report) == "visual_fresh"


def test_v2_cached_screen_description_is_visual_cached() -> None:
    report = build_session_report(
        v2_session(evidence=("screen",), resolutions=(("screen", "cache_hit", "12"),))
    )
    assert path_of(report) == "visual_cached"
    assert score_session(report)["passed"] is True


def test_v2_camera_cache_hit_is_visual_cached() -> None:
    report = build_session_report(
        v2_session(evidence=("participant",), resolutions=(("participant", "cache_hit", "12"),))
    )
    assert path_of(report) == "visual_cached"


def test_v2_camera_miss_is_visual_fresh() -> None:
    report = build_session_report(
        v2_session(evidence=("participant",), resolutions=(("participant", "fresh", "12"),))
    )
    assert path_of(report) == "visual_fresh"


def test_v2_fresh_dominates_cached_regardless_of_order() -> None:
    fresh_first = build_session_report(
        v2_session(
            evidence=("screen", "participant"),
            resolutions=(("screen", "fresh", "12"), ("participant", "cache_hit", "12.1")),
        )
    )
    cached_first = build_session_report(
        v2_session(
            evidence=("screen", "participant"),
            resolutions=(("participant", "cache_hit", "12"), ("screen", "fresh", "12.1")),
        )
    )
    other_pair = build_session_report(
        v2_session(
            evidence=("screen", "participant"),
            resolutions=(("screen", "cache_hit", "12"), ("participant", "fresh", "12.1")),
        )
    )
    assert path_of(fresh_first) == "visual_fresh"
    assert path_of(cached_first) == "visual_fresh"
    assert path_of(other_pair) == "visual_fresh"


def test_v2_all_visual_evidence_unavailable_is_visual_unavailable() -> None:
    report = build_session_report(
        v2_session(
            evidence=("screen", "participant"),
            resolutions=(("screen", "unavailable", "12"), ("participant", "unavailable", "12.1")),
        )
    )
    assert path_of(report) == "visual_unavailable"
    assert report["latency_by_evidence_path"]["visual_cached"]["expected_answers"] == 0


def test_v2_visual_requested_without_telemetry_is_unclassified() -> None:
    report = build_session_report(v2_session(evidence=("screen",)))
    card = score_session(report)
    assert report["evidence_unclassified"] == 1
    assert "evidence_classification" in card["unscored"]
    assert card["passed"] is False


def test_v2_historical_evidence_paths_journal_keeps_old_classification() -> None:
    rows = v2_session(evidence=("screen",))
    rows = [
        {key: value for key, value in entry.items() if key != "evidence_resolution_v2"}
        for entry in rows
    ]
    report = build_session_report(rows)
    card = score_session(report)
    assert report["evidence_resolution_v2"] is False
    assert report["latency_by_evidence_path"]["visual_cached"]["expected_answers"] == 1
    assert report["latency_by_evidence_path"].get("visual_unavailable") is None
    assert card["passed"] is True


def test_v2_unclassified_turn_still_counts_in_global_coverage() -> None:
    rows = v2_session(evidence=("screen",), answer_at=None)
    rows.insert(-1, row(12, "agent_failed", request_id="t1:1"))
    report = build_session_report(rows)
    assert report["replies"]["expected_answers"] == 1
    assert report["replies"]["answers_with_audio"] == 0
