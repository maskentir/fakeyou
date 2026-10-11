from datetime import UTC, datetime

from fakeyou.foundation.transcript import (
    TranscriptEvent,
)


def event(
    id: str,
    text: str = "hello",
    finalized: bool = True,
    participant_id: str = "unknown",
    speaker_evidence_id: str | None = None,
) -> TranscriptEvent:
    return TranscriptEvent(
        id,
        datetime.now(UTC).isoformat(),
        "meeting_audio",
        text,
        finalized,
        participant_id=participant_id,
        speaker_evidence_id=speaker_evidence_id,
    )
