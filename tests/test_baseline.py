from datetime import UTC, datetime, timedelta
from typing import Any

from fakeyou.observability.baseline import baseline_metrics

START = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def row(seconds: float, event: str, **fields: Any) -> dict[str, Any]:
    return {"event": event, "at": (START + timedelta(seconds=seconds)).isoformat(), **fields}


TURN = [
    row(0.0, "speech_started"),
    row(2.0, "speech_stopped"),
    row(3.2, "transcript_finalized", transcript_id="t1", speaker_bound=True),
    row(3.3, "transcript_finalized", transcript_id="t0", speaker_bound=False),
    row(4.0, "reply_plan_observed", transcript_id="t1", contribution="none"),
    row(4.4, "synthesis_pcm_emitted", transcript_id="t1", sentence_index=0),
    row(4.5, "playback_started", transcript_id="t1", sentence_index=0),
    row(6.0, "playback_finished", transcript_id="t1", sentence_index=0),
    row(6.1, "playback_started", transcript_id="t1", sentence_index=1),
    row(6.5, "speech_started"),
    row(8.0, "barge_in_detected", trigger="interim", interrupted_transcript_id="t1"),
    row(8.3, "playback_dropped", transcript_id="t1", sentence_index=1, detail="interrupted"),
    row(9.0, "request_started", source="memory", operation="prefetch", transcript_id="t1"),
    row(9.5, "speech_rejected", reason="invented_number"),
    row(10.0, "session_finished", terminal_reason="left_by_participant", detail="asked to leave"),
]


def test_turn_latencies_follow_the_turn_keys() -> None:
    metrics = baseline_metrics(TURN)

    assert metrics["vad_stop_to_final_ms"]["p50"] == 1200.0
    assert metrics["final_to_first_pcm_ms"]["p50"] == 1200.0
    assert metrics["final_to_first_audio_ms"]["p50"] == 1300.0
    assert metrics["sentence_gap_ms"]["p50"] == 100.0
    assert metrics["barge_in_to_silence_ms"]["p50"] == 1800.0


def test_counts_cover_identity_contribution_and_the_session_end() -> None:
    metrics = baseline_metrics(TURN)

    assert metrics["speaker_bound"] == {"count": 1, "of": 2, "ratio": 0.5}
    assert metrics["contributions"] == {"count": 0, "of": 1, "ratio": 0.0}
    assert metrics["speech_rejected"] == {"invented_number": 1}
    assert metrics["memory_prefetches"] == 1
    assert (metrics["terminal_reason"], metrics["terminal_detail"]) == (
        "left_by_participant",
        "asked to leave",
    )


def test_audio_without_a_turn_key_is_not_paired_by_time() -> None:
    rows = [
        row(0.0, "transcript_finalized", transcript_id="t1"),
        row(1.0, "playback_started"),
    ]

    assert baseline_metrics(rows)["final_to_first_audio_ms"]["count"] == 0
