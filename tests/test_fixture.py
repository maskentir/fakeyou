from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fakeyou.persistence.record import ChatEntry, RecordEntry, TranscriptEntry, TwinSpeechEntry
from fakeyou.replay.fixture import (
    SynthesisTiming,
    VoiceEdge,
    build_fixture,
    load_fixture,
)

START = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
RUNTIME = Path(".runtime/audio")


def at(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def row(event: str, seconds: float, **fields: Any) -> dict[str, Any]:
    return {"event": event, "meeting_id": "m-1", "at": at(seconds), **fields}


def heard(transcript_id: str, text: str, seconds: float, seq: int) -> TranscriptEntry:
    return TranscriptEntry(
        at=START + timedelta(seconds=seconds),
        seq=seq,
        id=transcript_id,
        spoken_at=at(seconds),
        source="meeting_audio",
        text=text,
        participant_id="unknown",
        speaker_evidence_id=None,
    )


def session() -> tuple[list[dict[str, Any]], list[RecordEntry]]:
    rows = [
        row("speech_started", 0.0),
        row("speech_stopped", 1.0),
        row("stt_turn_recorded", 1.9, interims=3, first_interim_to_final_ms=800.0),
        row("transcript_finalized", 2.0, transcript_id="t1"),
        row("policy_decided", 2.4, transcript_id="t1", action="reply", confidence=0.9),
        row("policy_decided", 2.6, transcript_id="t1", action="silent"),
        row(
            "request_started",
            2.5,
            source="llm",
            operation="chat",
            request_id="r1",
            transcript_id="t1",
        ),
        row("answer_sentence_emitted", 3.0, transcript_id="t1", sentence_index=0, chars=11),
        row("answer_sentence_emitted", 3.5, transcript_id="t1", sentence_index=1, chars=20),
        row(
            "request_finished",
            3.6,
            source="llm",
            operation="chat",
            request_id="r1",
            transcript_id="t1",
            duration_ms=1100.0,
        ),
        row(
            "synthesis_started",
            3.8,
            request_id="s1",
            transcript_id="t1",
            sentence_index=0,
            chars=11,
        ),
        row(
            "synthesis_finished",
            3.9,
            request_id="s1",
            transcript_id="t1",
            sentence_index=0,
            first_chunk_ms=300.0,
            duration_ms=450.0,
            bytes_out=9600,
            chars=11,
        ),
        row("synthesis_finished", 4.0, first_chunk_ms=200.0, duration_ms=300.0),
        row("transcript_finalized", 6.0, transcript_id="t2"),
        row(
            "request_started",
            6.2,
            source="llm",
            operation="chat",
            request_id="r2",
            transcript_id="t2",
        ),
        row("transcript_finalized", 7.0, transcript_id="missing"),
    ]
    entries: list[RecordEntry] = [
        heard("t1", "can you see my screen", 2.0, 1),
        TwinSpeechEntry(
            at=START + timedelta(seconds=4), seq=2, causation_id="t1", text="Yes, I can."
        ),
        ChatEntry(
            at=START + timedelta(seconds=5),
            seq=3,
            ref="chat#1",
            message_id="c1",
            sender_id="d1",
            sender="Ayu",
            text="hi",
            sent_at=START + timedelta(seconds=5),
            own=False,
            backlog=False,
        ),
        heard("t2", "never mind", 6.0, 4),
    ]
    return rows, entries


def test_the_input_timeline_pairs_each_final_with_its_text_and_stt_turn() -> None:
    fixture = build_fixture(*session())

    assert fixture.edges == (VoiceEdge(0.0, active=True), VoiceEdge(1.0, active=False))
    assert [(final.offset, final.entry.text) for final in fixture.finals] == [
        (2.0, "can you see my screen"),
        (6.0, "never mind"),
    ]
    assert (fixture.finals[0].interims, fixture.finals[0].first_interim_to_final_ms) == (3, 800.0)
    assert (fixture.finals[1].interims, fixture.finals[1].first_interim_to_final_ms) == (0, None)
    assert [(percept.offset, percept.entry.ref) for percept in fixture.percepts] == [
        (5.0, "chat#1")
    ]


def test_the_first_verdict_for_a_final_keeps_its_delay_and_fields() -> None:
    fixture = build_fixture(*session())

    verdict = fixture.verdicts["t1"]
    assert verdict.delay_ms == pytest.approx(400)
    assert verdict.fields["action"] == "reply"
    assert verdict.fields["confidence"] == 0.9


def test_replies_keep_request_timing_sentences_and_spoken_text() -> None:
    fixture = build_fixture(*session())

    answered = fixture.replies["t1"]
    assert answered.duration_ms == 1100.0
    assert [(s.index, s.chars) for s in answered.sentences] == [(0, 11)]
    assert answered.sentences[0].offset_ms == pytest.approx(500)
    assert answered.spoken == ("Yes, I can.",)
    cancelled = fixture.replies["t2"]
    assert (cancelled.duration_ms, cancelled.sentences, cancelled.spoken) == (None, (), ())


def test_only_keyed_syntheses_are_kept() -> None:
    fixture = build_fixture(*session())

    assert fixture.syntheses == {("t1", 0): SynthesisTiming(300.0, 450.0, 9600, 11)}
    assert fixture.synthesized == {("t1", 0): 11}


def test_a_sentence_takes_the_text_that_was_actually_spoken() -> None:
    fixture = build_fixture(*session())

    answered = fixture.replies["t1"]
    assert [(sentence.index, sentence.text) for sentence in answered.sentences] == [
        (0, "Yes, I can.")
    ]
    assert answered.spoken == ("Yes, I can.",)


def test_a_sentence_that_never_reached_the_synthesizer_is_not_replayed() -> None:
    fixture = build_fixture(*session())

    assert [sentence.index for sentence in fixture.replies["t1"].sentences] == [0]


def test_an_empty_journal_cannot_be_replayed() -> None:
    with pytest.raises(ValueError, match="no rows"):
        build_fixture([], [])


@pytest.mark.skipif(not (RUNTIME / "journals/live-oue-4.jsonl").exists(), reason="no oue-4 data")
def test_the_oue_4_session_loads_as_a_replay_fixture() -> None:
    fixture = load_fixture(
        RUNTIME / "journals/live-oue-4.jsonl", RUNTIME / "meetings/live-oue-4/record.jsonl"
    )

    assert len(fixture.finals) == 236
    assert len(fixture.verdicts) >= 230
    assert sum(1 for reply in fixture.replies.values() if reply.sentences) >= 20
    assert len(fixture.syntheses) >= 15


def test_an_early_turn_is_keyed_by_the_final_it_was_verified_as() -> None:
    rows = [
        row("transcript_finalized", 2.0, transcript_id="final"),
        row("policy_decided", 1.8, transcript_id="early", action="reply", reason="judge_reply"),
        row("early_turn_verified", 2.0, early_transcript_id="early", transcript_id="final"),
        row(
            "request_started",
            1.9,
            source="llm",
            operation="chat",
            request_id="r1",
            transcript_id="early",
        ),
        row("answer_sentence_emitted", 2.5, transcript_id="early", sentence_index=0, chars=3),
    ]

    fixture = build_fixture(rows, [heard("final", "are you there", 2.0, 1)])

    assert fixture.verdicts["final"].fields["reason"] == "judge_reply"
    assert "final" in fixture.replies
    assert fixture.rows[1]["transcript_id"] == "early"
