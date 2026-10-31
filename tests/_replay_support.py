import asyncio
import warnings
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from fakeyou.foundation.transcript import TranscriptUpdate, transcript_event
from fakeyou.persistence.record import RecordEntry, TranscriptEntry, TwinSpeechEntry
from fakeyou.replay.virtual_time import VirtualTimeLoop

START = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
QUESTION = "Harun, can you see my screen?"
ANSWER = "Yes, I can see it."


def run_virtual[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
            return runner.run(main())


def at(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def row(event: str, seconds: float, **fields: Any) -> dict[str, Any]:
    return {"event": event, "meeting_id": "m-1", "at": at(seconds), **fields}


def recorded_conversation() -> tuple[list[dict[str, Any]], list[RecordEntry]]:
    spoken_at = at(2.0)
    asked = transcript_event(
        TranscriptUpdate(text=QUESTION, final=True, timestamp=spoken_at), "unknown", None
    ).id
    rows = [
        row("speech_started", 0.0),
        row("speech_stopped", 1.0),
        row("stt_turn_recorded", 2.0, interims=2, first_interim_to_final_ms=600.0),
        row("transcript_finalized", 2.0, transcript_id=asked),
        row(
            "policy_decided",
            2.1,
            transcript_id=asked,
            action="reply",
            reason="judge_reply",
            confidence=0.95,
            heard=QUESTION,
            addressee="twin",
            finished=True,
            backchannel=False,
        ),
        row(
            "request_started",
            2.2,
            source="llm",
            operation="chat",
            request_id="r1",
            transcript_id=asked,
        ),
        row("reply_plan_observed", 3.0, transcript_id=asked, language="en", intent="answer"),
        row(
            "answer_sentence_emitted", 3.0, transcript_id=asked, sentence_index=0, chars=len(ANSWER)
        ),
        row(
            "request_finished",
            3.1,
            source="llm",
            operation="chat",
            request_id="r1",
            transcript_id=asked,
            duration_ms=900.0,
        ),
        row(
            "synthesis_started",
            3.0,
            request_id="s1",
            transcript_id=asked,
            sentence_index=0,
            chars=len(ANSWER),
        ),
        row(
            "synthesis_pcm_emitted",
            3.4,
            request_id="s1",
            transcript_id=asked,
            sentence_index=0,
            first_chunk_ms=400.0,
        ),
        row(
            "synthesis_finished",
            3.6,
            request_id="s1",
            transcript_id=asked,
            sentence_index=0,
            first_chunk_ms=400.0,
            duration_ms=600.0,
            bytes_out=48000,
            chars=len(ANSWER),
        ),
        row("speech_started", 9.0),
        row("speech_stopped", 9.5),
    ]
    entries: list[RecordEntry] = [
        TranscriptEntry(
            at=START + timedelta(seconds=2),
            seq=1,
            id=asked,
            spoken_at=spoken_at,
            source="meeting_audio",
            text=QUESTION,
            participant_id="unknown",
            speaker_evidence_id=None,
        ),
        TwinSpeechEntry(at=START + timedelta(seconds=4), seq=2, causation_id=asked, text=ANSWER),
    ]
    return rows, entries
