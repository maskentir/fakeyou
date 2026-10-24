from fakeyou.observability.report import build_session_report
from tests._report_support import answered_turn, row


def test_report_summarizes_participation_and_policy() -> None:
    rows = (
        answered_turn(
            0,
            "t1",
            "The demo is on Friday.",
            participant_id="spaces/x/devices/1",
            speaker_evidence_id="e1",
        )
        + [
            row(
                1.2,
                "request_finished",
                source="llm",
                request_id="t1:1",
                transcript_id="t1",
                duration_ms=800,
            ),
        ]
        + [
            row(
                1.0,
                "policy_decided",
                transcript_id="t1",
                action="reply",
                reason="judge_reply",
                decision_source="judge",
            ),
            row(
                2.0,
                "transcript_finalized",
                transcript_id="t2",
                text="Budi, when is the demo?",
                participant_id="unknown",
                speaker_evidence_id="e9",
            ),
            row(
                2.0,
                "policy_decided",
                transcript_id="t2",
                action="silent",
                reason="judge_stay_silent",
                decision_source="judge",
            ),
            row(
                3.0,
                "transcript_finalized",
                transcript_id="t3",
                text="I agree with Budi.",
                participant_id="unknown",
                speaker_evidence_id=None,
            ),
            row(
                3.0,
                "policy_decided",
                transcript_id="t3",
                action="silent",
                reason="fallback_unnamed",
                decision_source="fallback",
            ),
            row(0.5, "participant_observer_started", source="runtime", method="dom_mining"),
        ]
    )

    report = build_session_report(rows)

    assert report["participation"]["utterances_recorded"] == 3
    assert report["participation"]["attributed_utterances"] == 1
    assert report["participation"]["attribution_coverage"] == 0.333
    assert report["participation"]["unknown_reasons"] == {
        "evidence_unmatched": 1,
        "no_evidence": 1,
    }
    assert report["participation"]["answers"] == 1
    assert report["participation"]["policy"] == {"reply": 1, "silent": 2}
    assert report["participation"]["policy_reasons"] == {
        "judge_reply": 1,
        "judge_stay_silent": 1,
        "fallback_unnamed": 1,
    }
    assert report["participation"]["policy_sources"] == {"judge": 2, "fallback": 1}
    assert report["participation"]["observer_methods"] == {"dom_mining": 1}


def test_report_counts_memory_prefetch_as_recall_for_its_transcript() -> None:
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(
            0.5,
            "request_started",
            source="memory",
            request_id="m1",
            operation="prefetch",
            transcript_id="t1",
        ),
        row(
            0.8,
            "request_finished",
            source="memory",
            request_id="m1",
            operation="prefetch",
            transcript_id="t1",
            outcome="found",
            duration_ms=300,
        ),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
    ]

    turn = build_session_report(rows)["turns"][0]

    assert (turn["recall_count"], turn["recall_duration_seconds"]) == (1, 0.3)
    assert turn["llm_count"] == 0


def test_report_summarizes_stt_quality_signals() -> None:
    rows = [
        row(0, "order_inversion_detected", source="stt", transcript_id="t2"),
        row(1, "order_inversion_detected", source="stt", transcript_id="t3"),
        row(2, "stt_turn_recorded", source="stt", interims=2, first_interim_to_final_ms=120.0),
        row(3, "stt_turn_recorded", source="stt", interims=0, first_interim_to_final_ms=None),
        row(
            4,
            "capture_polled",
            source="browser",
            slots={"0": {"engine": "decode"}, "1": {"engine": "mstp"}},
        ),
    ]

    quality = build_session_report(rows)["stt_quality"]

    assert quality["order_inversions"] == 2
    assert quality["turn_interims"] == {"count": 2, "p50": 1.0, "p95": 1.9, "max": 2}
    assert quality["first_interim_to_final_ms"] == {
        "count": 1,
        "p50": 120.0,
        "p95": 120.0,
        "max": 120.0,
    }
    assert quality["capture_engines"] == {"decode": 1, "mstp": 1}


def test_session_report_counts_drain_gaps() -> None:
    rows = [
        {"event": "drain_gap_detected", "slot": 0, "kind": "gap", "missed": 2},
        {"event": "drain_gap_detected", "slot": 1, "kind": "resync", "missed": 0},
    ]
    assert build_session_report(rows)["stt_quality"]["drain_gaps"] == 2


def test_session_report_counts_tap_slot_rescues() -> None:
    rows = [
        {"event": "tap_slot_rescued", "slot": 0, "rescued": 1},
        {"event": "tap_slot_rescued", "slot": 0, "rescued": 2},
    ]
    assert build_session_report(rows)["stt_quality"]["tap_slot_rescues"] == 2


def test_session_report_counts_tap_slot_drops() -> None:
    rows = [
        {"event": "tap_slot_dropped", "slot": 0},
        {"event": "tap_slot_dropped", "slot": 2},
    ]
    assert build_session_report(rows)["stt_quality"]["tap_slot_drops"] == 2


def test_session_report_summarizes_pregeneration() -> None:
    rows = [
        {"event": "pregeneration_started", "request_id": "a", "words": 4},
        {
            "event": "pregeneration_finished",
            "request_id": "a",
            "outcome": "discarded",
            "reason": "superseded",
            "dist": 1,
            "words_pre": 4,
            "words_final": 5,
            "norm_dist_bucket": 0.2,
            "diff_class": "prefix_extend",
        },
        {"event": "pregeneration_started", "request_id": "b", "words": 5},
        {
            "event": "pregeneration_finished",
            "request_id": "b",
            "outcome": "adopted",
            "duration_ms": 3000.0,
            "dist": 0,
            "words_pre": 5,
            "words_final": 5,
            "norm_dist_bucket": 0.0,
            "diff_class": "exact",
        },
    ]
    section = build_session_report(rows)["pregeneration"]
    assert section["starts"] == 2
    assert section["outcomes"] == {"discarded": 1, "adopted": 1}
    assert section["reasons"] == {"superseded": 1}
    assert section["diff_classes"] == {"prefix_extend": 1, "exact": 1}
    assert section["norm_dist_buckets"] == {"0.2": 1, "0.0": 1}
    assert section["adopted_overlap_seconds"] == {
        "count": 1,
        "p50": 3.0,
        "p95": 3.0,
        "max": 3.0,
    }


def test_report_carries_journal_corrupt_lines() -> None:
    assert build_session_report([])["journal"] == {"corrupt_lines": 0}
    assert build_session_report([], corrupt_lines=2)["journal"] == {"corrupt_lines": 2}
