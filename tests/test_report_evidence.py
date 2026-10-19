from fakeyou.observability.report import (
    build_session_report,
)
from tests._report_support import answered_turn, row


def test_report_should_count_trailing_questions_without_retaining_reply_text() -> None:
    report = build_session_report(
        [
            row(1, "playback_finished", ends_with_question=True),
            row(2, "playback_finished", ends_with_question=False),
            row(3, "playback_finished"),
            row(4, "playback_dropped", ends_with_question=True),
        ]
    )

    assert report["replies"] == {"spoken": 3, "interrupted": 1, "trailing_questions": 1}


def test_interrupted_stream_with_pcm_still_reports_first_pcm_latency() -> None:
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(
            1.7,
            "synthesis_started",
            source="tts",
            request_id="s1",
            chars=6,
            streaming=True,
        ),
        row(
            2.5,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=800,
            bytes_out=2,
        ),
        row(2.6, "speech_started", source="audio"),
        row(
            3.0,
            "playback_dropped",
            source="playback",
            detail="interrupted",
            ends_with_question=False,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["outcome"] == "interrupted"
    assert turn["reply_to_first_pcm_seconds"] == 0.9
    assert turn["speech_stopped_to_first_pcm_seconds"] == 2.5
    assert report["evidence"] == {"excluded": {}}


def test_late_synthesis_finish_uses_its_request_mapping() -> None:
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(1.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(10, "speech_stopped", source="audio"),
        row(10.4, "transcript_finalized", transcript_id="t2", text="Second"),
        row(10.4, "agent_started", request_id="t2:1"),
        row(11.6, "agent_finished", request_id="t2:1", duration_ms=1200),
        row(
            12.0,
            "synthesis_finished",
            source="tts",
            request_id="s1",
            duration_ms=3000,
            first_chunk_ms=900,
            bytes_out=4,
            streaming=True,
        ),
    ]

    report = build_session_report(rows)
    first, second = report["turns"]

    assert first["reply_to_first_pcm_seconds"] == 8.3
    assert first["speech_stopped_to_first_pcm_seconds"] == 9.9
    assert second["reply_to_first_pcm_seconds"] is None
    assert second["speech_stopped_to_first_pcm_seconds"] is None
    assert report["evidence"] == {"excluded": {"missing_timestamp": 2}}


def test_synthesis_finished_without_chunk_proof_yields_no_pcm_latency() -> None:
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows[5] = row(4.6, "synthesis_finished", duration_ms=3000, first_chunk_ms=None)

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["final_to_reply_seconds"] == 1.2
    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] is None


def test_clean_turn_reports_no_excluded_evidence() -> None:
    report = build_session_report(answered_turn(0, "t1", "The demo is on Friday."))

    assert report["evidence"] == {"excluded": {}}
    assert build_session_report([])["evidence"] == {"excluded": {}}


def test_missing_timestamps_are_excluded_with_reasons() -> None:
    report = build_session_report(
        [row(0.4, "transcript_finalized", transcript_id="t1", text="Hello")]
    )
    [turn] = report["turns"]

    assert turn["speech_stopped_to_final_seconds"] is None
    assert turn["final_to_agent_seconds"] is None
    assert turn["agent_to_reply_seconds"] is None
    assert turn["final_to_reply_seconds"] is None
    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] is None
    assert report["evidence"] == {"excluded": {"missing_timestamp": 6}}


def test_negative_latency_is_excluded_with_reasons() -> None:
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(1.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(
            1.0,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=100,
            bytes_out=2,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["speech_stopped_to_final_seconds"] == 0.4
    assert turn["final_to_reply_seconds"] == 1.2
    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] == 1.0
    assert report["evidence"] == {"excluded": {"negative_latency": 1}}


def test_nonfinite_durations_are_excluded_with_reasons() -> None:
    rows = []
    for offset, transcript_id, first_chunk_ms, duration_ms in [
        (0, "t1", float("inf"), 3000),
        (10, "t2", 900, float("nan")),
    ]:
        rows += [
            row(offset, "speech_stopped", source="audio"),
            row(offset + 0.4, "transcript_finalized", transcript_id=transcript_id, text="Hi"),
            row(offset + 0.4, "agent_started", request_id=f"{transcript_id}:1"),
            row(offset + 1.6, "agent_finished", request_id=f"{transcript_id}:1"),
            row(offset + 1.6, "playback_started", source="playback", ends_with_question=False),
            row(offset + 1.7, "synthesis_started", source="tts", request_id=f"s-{transcript_id}"),
            row(
                offset + 4.6,
                "synthesis_finished",
                source="tts",
                request_id=f"s-{transcript_id}",
                duration_ms=duration_ms,
                first_chunk_ms=first_chunk_ms,
            ),
        ]

    report = build_session_report(rows)

    assert [turn["reply_to_first_pcm_seconds"] for turn in report["turns"]] == [None, None]
    assert [turn["final_to_reply_seconds"] for turn in report["turns"]] == [1.2, 1.2]
    assert report["evidence"] == {"excluded": {"nonfinite_duration": 2, "missing_timestamp": 4}}


def test_invalid_durations_are_excluded_with_reasons() -> None:
    cases = [(-1, 3000), (5000, 3000), ("soon", 3000), (900, -5), (True, 3000)]
    for index, (first_chunk_ms, duration_ms) in enumerate(cases):
        transcript_id = f"t{index}"
        rows = [
            row(0, "speech_stopped", source="audio"),
            row(0.4, "transcript_finalized", transcript_id=transcript_id, text="Hi"),
            row(0.4, "agent_started", request_id=f"{transcript_id}:1"),
            row(1.6, "agent_finished", request_id=f"{transcript_id}:1"),
            row(1.6, "playback_started", source="playback", ends_with_question=False),
            row(1.7, "synthesis_started", source="tts", request_id="s1"),
            row(
                4.6,
                "synthesis_finished",
                source="tts",
                request_id="s1",
                duration_ms=duration_ms,
                first_chunk_ms=first_chunk_ms,
            ),
        ]

        report = build_session_report(rows)
        [turn] = report["turns"]

        assert turn["reply_to_first_pcm_seconds"] is None
        assert turn["speech_stopped_to_first_pcm_seconds"] is None
        assert report["evidence"] == {"excluded": {"invalid_duration": 1, "missing_timestamp": 2}}


def test_malformed_timestamps_do_not_break_the_report() -> None:
    rows = [
        {"event": "speech_stopped", "meeting_id": "m1", "at": "not-a-time", "source": "audio"},
        row(0.4, "transcript_finalized", transcript_id="t1", text="Hi"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1"),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["speech_stopped_to_final_seconds"] is None
    assert turn["final_to_reply_seconds"] == 1.2
    assert report["evidence"]["excluded"]["missing_timestamp"] == 3


def test_synthesis_events_without_a_recorded_start_are_excluded() -> None:
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(
            2.5,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="lost",
            first_chunk_ms=800,
            bytes_out=2,
        ),
        row(
            4.6,
            "synthesis_finished",
            source="tts",
            request_id="lost",
            duration_ms=3000,
            first_chunk_ms=900,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] is None
    assert report["evidence"]["excluded"]["missing_synthesis_start"] == 2


def test_synthesis_start_without_an_answering_turn_is_ambiguous() -> None:
    rows = [
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.5, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(
            1.5,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=800,
            bytes_out=2,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["reply_to_first_pcm_seconds"] is None
    assert report["evidence"]["excluded"]["ambiguous_synthesis_request"] == 1


def test_duplicate_synthesis_start_marks_the_request_ambiguous() -> None:
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(1.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(10, "speech_stopped", source="audio"),
        row(10.4, "transcript_finalized", transcript_id="t2", text="Second"),
        row(10.4, "agent_started", request_id="t2:1"),
        row(11.6, "agent_finished", request_id="t2:1", duration_ms=1200),
        row(11.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(
            12.0,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=300,
            bytes_out=2,
        ),
    ]

    report = build_session_report(rows)
    first, second = report["turns"]

    assert first["reply_to_first_pcm_seconds"] is None
    assert second["reply_to_first_pcm_seconds"] is None
    assert report["evidence"]["excluded"]["ambiguous_synthesis_request"] == 1


def test_report_correlates_model_time_to_its_transcript() -> None:
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows += [
        row(0.5, "request_started", source="llm", request_id="llm-1", transcript_id="t1"),
        row(
            1.5,
            "request_finished",
            source="llm",
            request_id="llm-1",
            transcript_id="t1",
            duration_ms=1000,
        ),
    ]
    report = build_session_report(sorted(rows, key=lambda entry: entry["at"]))
    turn = report["turns"][0]
    assert turn["llm_count"] == 1
    assert turn["llm_duration_seconds"] == 1.0
    assert turn["final_to_agent_seconds"] == 0.0
    assert turn["agent_to_reply_seconds"] == 1.2
