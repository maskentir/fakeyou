from datetime import UTC, datetime, timedelta
from typing import Any

START = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def row(seconds: float, event: str, **fields: Any) -> dict[str, Any]:
    return {
        "event": event,
        "meeting_id": "m1",
        "at": (START + timedelta(seconds=seconds)).isoformat(),
        **fields,
    }


def answered_turn(
    offset: float,
    transcript_id: str,
    reply: str,
    *,
    recall: bool = False,
    participant_id: str = "unknown",
    speaker_evidence_id: str | None = None,
) -> list[dict[str, Any]]:
    rows = [
        row(offset, "speech_stopped", source="audio"),
        row(
            offset + 0.4,
            "transcript_finalized",
            transcript_id=transcript_id,
            text="When is the demo?",
            participant_id=participant_id,
            speaker_evidence_id=speaker_evidence_id,
        ),
        row(offset + 0.4, "agent_started", request_id=f"{transcript_id}:1"),
    ]
    if recall:
        rows.append(
            row(offset + 1.0, "tool_finished", tool="recall_meeting_context", duration_ms=500)
        )
    rows += [
        row(offset + 1.6, "agent_finished", request_id=f"{transcript_id}:1", duration_ms=1200),
        row(offset + 1.6, "playback_started", source="playback", text=reply),
        row(offset + 4.6, "synthesis_finished", duration_ms=3000, first_chunk_ms=900),
        row(
            offset + 7.0,
            "playback_finished",
            source="playback",
            ends_with_question=reply.rstrip().endswith("?"),
        ),
    ]
    return rows
