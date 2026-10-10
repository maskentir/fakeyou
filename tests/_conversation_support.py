from datetime import UTC, datetime

from fakeyou.foundation.transcript import (
    TranscriptEvent,
)


def event(id, text="hello", finalized=True, participant_id="unknown", speaker_evidence_id=None):
    return TranscriptEvent(
        id,
        datetime.now(UTC).isoformat(),
        "meeting_audio",
        text,
        finalized,
        participant_id=participant_id,
        speaker_evidence_id=speaker_evidence_id,
    )
